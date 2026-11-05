"""On-demand Agent Skills with host-selected roots and reproducible snapshots.

Skills supply instructions, not permissions. This module never imports a bundled
script, executes a command, follows a symlink, or fetches a remote package.
"""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Iterable

import yaml

from .spec import canonical, digest


MAX_SKILLS = 128
MAX_FILES = 64
MAX_FILE_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 2 * 1024 * 1024
MAX_COLLECTION_BYTES = 16 * 1024 * 1024
MAX_LOCK_BYTES = 2 * 1024 * 1024
MAX_FRONTMATTER_BYTES = 16 * 1024
BUILTIN_ROOT = Path(__file__).parent / "builtin_skills"
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous mappings instead of silently keeping the last value."""


def _mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("Skill frontmatter keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _name(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or not _NAME.fullmatch(value):
        raise ValueError("Skill name must be a lowercase slug of at most 64 characters")
    return value


def _resource(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 240 or "\\" in value
            or any(ord(char) < 32 for char in value)
            or any(part in ("", ".", "..") for part in value.split("/"))
            or PurePosixPath(value).is_absolute()):
        raise ValueError("Skill resource must be a bounded relative POSIX path")
    return value


def _directory(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Skill directory must be a regular directory: {path}")
    return path.resolve()


def _read_regular(path: Path, limit: int) -> bytes:
    if path.is_symlink():
        raise ValueError(f"Skill symlinks are not accepted; import a reviewed copy: {path}")
    # O_NONBLOCK avoids hanging while rejecting a FIFO. O_NOFOLLOW also fences
    # replacement of the final path component between inspection and opening.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Skill resources must be regular files: {path}")
        if info.st_size > limit:
            raise ValueError(f"Skill resource exceeds {limit} bytes: {path}")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"Skill resource exceeds {limit} bytes: {path}")
    return data


def _metadata(data: bytes, directory_name: str) -> dict:
    try:
        document = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("SKILL.md must be UTF-8") from error
    lines = document.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("SKILL.md frontmatter is not closed") from error
    header = "\n".join(lines[1:end])
    if len(header.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise ValueError("Skill frontmatter exceeds 16 KiB")
    try:
        fields = yaml.load(header, Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, RecursionError) as error:
        raise ValueError(f"Invalid skill YAML: {error}") from error
    if not isinstance(fields, dict):
        raise ValueError("Skill frontmatter must be a mapping")
    name = _name(fields.get("name"))
    if name != directory_name:
        raise ValueError("Skill name must match its containing directory")
    description = fields.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("Skill description must contain 1..1024 characters")
    if not "\n".join(lines[end + 1:]).strip():
        raise ValueError("SKILL.md must contain instructions after its frontmatter")
    metadata = {}
    for field, limit in (("license", 1024), ("compatibility", 500), ("allowed-tools", 4096)):
        if field in fields:
            value = fields[field]
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"Invalid skill {field}")
            metadata[field] = value
    if "metadata" in fields:
        values = fields["metadata"]
        if (not isinstance(values, dict) or len(values) > 32
                or any(not isinstance(key, str) or not isinstance(value, str)
                       or len(key) > 128 or len(value) > 1024 for key, value in values.items())):
            raise ValueError("Skill metadata must be a bounded mapping of strings to strings")
        metadata["metadata"] = values
    # Unknown client-specific frontmatter is preserved in SKILL.md but is not
    # interpreted as runtime configuration or an expansion of tool authority.
    return {"name": name, "description": description.strip(), "metadata": metadata}


def _inspect_package(root: Path, provenance: dict) -> dict:
    root = _directory(root)
    entries = 0
    total = 0
    files = []
    main = None

    def visit(directory: Path, depth: int):
        nonlocal entries, total, main
        if depth > 6:
            raise ValueError("Skill resources exceed the maximum directory depth (6)")
        with os.scandir(directory) as iterator:
            children = []
            for entry in iterator:
                entries += 1
                if entries > MAX_FILES * 4:
                    raise ValueError("Skill package contains too many directory entries")
                children.append(entry)
        for entry in sorted(children, key=lambda item: item.name):
            path = Path(entry.path)
            if entry.is_symlink():
                raise ValueError(f"Skill symlinks are not accepted; import a reviewed copy: {path}")
            if entry.is_dir(follow_symlinks=False):
                visit(path, depth + 1)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ValueError(f"Skill resources must be regular files: {path}")
            resource = _resource(path.relative_to(root).as_posix())
            data = _read_regular(path, MAX_FILE_BYTES)
            total += len(data)
            if len(files) >= MAX_FILES or total > MAX_PACKAGE_BYTES:
                raise ValueError("Skill package exceeds 64 files or 2 MiB")
            files.append({"path": resource, "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
            if resource == "SKILL.md":
                main = data

    visit(root, 0)
    if main is None:
        raise ValueError(f"Skill package is missing SKILL.md: {root}")
    fields = _metadata(main, root.name)
    files.sort(key=lambda item: item["path"])
    return {**fields, "files": files, "package_sha256": digest(files),
            "provenance": copy.deepcopy(provenance)}


class SkillRegistry:
    """A bounded catalog backed by explicit local roots or a sealed snapshot.

    ``snapshot`` returns a manifest hash for the host's immutable task contract.
    Reopening requires that external hash; a lock file cannot approve itself.
    Host directories must stay outside candidate-writable execution mounts.
    """

    def __init__(self, records: list[dict], paths: dict[str, Path]):
        records = sorted(records, key=lambda item: item["name"])
        if len(records) > MAX_SKILLS:
            raise ValueError(f"Skill collection exceeds {MAX_SKILLS} packages")
        if len({record["name"] for record in records}) != len(records):
            raise ValueError("Duplicate skill names; choose one source explicitly")
        if sum(file["bytes"] for record in records for file in record["files"]) > MAX_COLLECTION_BYTES:
            raise ValueError("Skill collection exceeds 16 MiB")
        self._records = {record["name"]: copy.deepcopy(record) for record in records}
        self._paths = dict(paths)
        self._manifest = {"schema_version": 1, "skills": copy.deepcopy(records)}
        if len(canonical(self._manifest).encode("utf-8")) + 1 > MAX_LOCK_BYTES:
            raise ValueError("Skill snapshot manifest exceeds 2 MiB")
        self.sha256 = digest(self._manifest)

    @classmethod
    def builtin(cls) -> "SkillRegistry":
        return cls._from_roots([BUILTIN_ROOT], builtin=True)

    @classmethod
    def from_roots(cls, roots: Iterable[Path], *, names: Iterable[str] | None = None) -> "SkillRegistry":
        """Read reviewed skill directories or roots containing skill directories."""
        return cls._from_roots(roots, names=names)

    @classmethod
    def _from_roots(cls, roots, *, names=None, builtin=False):
        records, paths = [], {}
        root_count = 0
        for value in roots:
            root_count += 1
            if root_count > 16:
                raise ValueError("Select at most 16 skill roots")
            root = _directory(Path(value))
            if (root / "SKILL.md").exists():
                packages = [root]
            else:
                packages = []
                with os.scandir(root) as iterator:
                    for index, entry in enumerate(iterator):
                        if index >= MAX_SKILLS * 4:
                            raise ValueError("Skill root contains too many entries")
                        if entry.is_symlink():
                            raise ValueError("Skill roots cannot contain symlink entries; import a reviewed copy")
                        path = Path(entry.path)
                        if entry.is_dir(follow_symlinks=False) and (path / "SKILL.md").exists():
                            packages.append(path)
            for package in sorted(packages):
                provenance = {"kind": "builtin" if builtin else "local",
                              "source": f"infinity_research/builtin_skills/{package.name}"
                              if builtin else str(package)}
                record = _inspect_package(package, provenance)
                name = record["name"]
                if name in paths:
                    raise ValueError(f"Duplicate skill name {name!r}; choose one source explicitly")
                records.append(record)
                paths[name] = package
                if len(records) > MAX_SKILLS:
                    raise ValueError(f"Skill collection exceeds {MAX_SKILLS} packages")
                if sum(file["bytes"] for item in records for file in item["files"]) > MAX_COLLECTION_BYTES:
                    raise ValueError("Skill collection exceeds 16 MiB")
        registry = cls(records, paths)
        return registry._select(names) if names is not None else registry

    def _select(self, names: Iterable[str]) -> "SkillRegistry":
        selected = list(names)
        for name in selected:
            _name(name)
        if len(selected) > MAX_SKILLS or len(set(selected)) != len(selected):
            raise ValueError("Select distinct skill names within the collection limit")
        for name in selected:
            if name not in self._records:
                raise ValueError(f"Unknown skill: {name}")
        return SkillRegistry([self._records[name] for name in selected],
                             {name: self._paths[name] for name in selected})

    def catalog(self) -> list[dict]:
        """Return discovery metadata only, never full skill bodies or resources."""
        return [{key: copy.deepcopy(record[key])
                 for key in ("name", "description", "package_sha256", "provenance")}
                for record in self._records.values()]

    def load(self, name: str, resource: str = "SKILL.md") -> dict:
        """Read one UTF-8 resource after checking the package's recorded bytes."""
        _name(name)
        resource = _resource(resource)
        if name not in self._records:
            raise ValueError(f"Unknown skill: {name}")
        record = self._records[name]
        root = self._paths[name]
        if _inspect_package(root, record["provenance"]) != record:
            raise ValueError(f"Skill package changed after discovery: {name}")
        entry = next((file for file in record["files"] if file["path"] == resource), None)
        if entry is None:
            raise ValueError(f"Unknown resource for {name}: {resource}")
        data = _read_regular(root / resource, MAX_FILE_BYTES)
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"Skill resource changed during loading: {name}/{resource}")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Binary skill assets cannot be read through the text resource tool") from error
        return {"name": name, "resource": resource, "content": content,
                "sha256": entry["sha256"], "package_sha256": record["package_sha256"],
                "resources": copy.deepcopy(record["files"]),
                "metadata": copy.deepcopy(record["metadata"]),
                "provenance": copy.deepcopy(record["provenance"]),
                "grants_tools": False}

    def snapshot(self, destination: Path, *, names: Iterable[str] | None = None) -> dict:
        """Copy exact selected bytes into a new directory; write lock.json last.

        An interrupted copy has no valid lock and cannot be opened as a snapshot.
        Existing destinations, including partial snapshots, are never overwritten.
        """
        selected = self._select(names) if names is not None else self
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise ValueError("Skill snapshot destination must not exist")
        destination = destination.resolve()
        if any(destination.is_relative_to(path) for path in selected._paths.values()):
            raise ValueError("Skill snapshot must be outside its source packages")
        for name, record in selected._records.items():
            if _inspect_package(selected._paths[name], record["provenance"]) != record:
                raise ValueError(f"Skill package changed before snapshot: {name}")
        destination.mkdir(parents=True, exist_ok=False)
        for name, record in selected._records.items():
            for entry in record["files"]:
                data = _read_regular(selected._paths[name] / entry["path"], MAX_FILE_BYTES)
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise ValueError(f"Skill resource changed during snapshot: {name}/{entry['path']}")
                output = destination / "packages" / name / entry["path"]
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(data)
        (destination / "lock.json").write_text(canonical(selected._manifest) + "\n", encoding="utf-8")
        return {"path": str(destination), "sha256": selected.sha256,
                "skill_count": len(selected._records)}

    @classmethod
    def from_snapshot(cls, root: Path, *, expected_sha256: str) -> "SkillRegistry":
        """Verify an externally pinned manifest and every listed package/resource."""
        import json

        if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
            raise ValueError("An expected skill snapshot SHA-256 is required")
        root = _directory(root)
        manifest = json.loads(_read_regular(root / "lock.json", MAX_LOCK_BYTES))
        if digest(manifest) != expected_sha256:
            raise ValueError("Skill snapshot manifest changed")
        if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "skills"}
                or manifest["schema_version"] != 1 or not isinstance(manifest["skills"], list)
                or len(manifest["skills"]) > MAX_SKILLS):
            raise ValueError("Unsupported skill snapshot manifest")
        records, paths = [], {}
        # Even an empty snapshot must not contain an injected symlink root.
        packages = root / "packages"
        if packages.exists() or packages.is_symlink():
            _directory(packages)
        for record in manifest["skills"]:
            if not isinstance(record, dict):
                raise ValueError("Skill snapshot records must be objects")
            name = _name(record.get("name"))
            package = packages / name
            if _inspect_package(package, record.get("provenance", {})) != record:
                raise ValueError(f"Skill snapshot package changed: {name}")
            records.append(record)
            paths[name] = package
        registry = cls(records, paths)
        if registry.sha256 != expected_sha256:
            raise ValueError("Skill snapshot manifest is not in canonical name order")
        return registry
