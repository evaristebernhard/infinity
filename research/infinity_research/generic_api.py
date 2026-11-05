"""Host composition of the generic ledger, skill packages and capability tools.

The kernel knows work and leases. This module binds tools to that kernel without
turning a particular metric, dataset layout or proof language into a core rule.
"""

from __future__ import annotations

import json
from pathlib import Path
import uuid

def _object(properties=None, required=()):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


_STRING = {"type": "string"}
_IDS = {"type": "array", "items": _STRING, "maxItems": 128}
_LEASE = {"work_id": _STRING, "lease_token": _STRING}
_WORK_SCHEMAS = {
    "create_work": _object({"goal": _STRING, "parent_id": {"type": ["string", "null"]},
                            "depends_on": _IDS, "skill_names": _IDS}, ("goal",)),
    "delegate_work": _object({**_LEASE, "goal": _STRING, "brief": {"type": "object"},
                              "depends_on": _IDS, "skill_names": _IDS}, (*_LEASE, "goal")),
    "claim_work": _object({"work_id": _STRING}),
    "renew_work": _object(_LEASE, _LEASE),
    "wait_work": _object({**_LEASE, "depends_on": _IDS,
                           "join_mode": {"type": "string", "enum": ["all_success", "all_settled"]}},
                          (*_LEASE, "depends_on")),
    "complete_work": _object({**_LEASE, "result": {"type": "object"}, "effect_ids": _IDS},
                             (*_LEASE, "result")),
    "fail_work": _object({**_LEASE, "error": _STRING, "retry": {"type": "boolean"}},
                         (*_LEASE, "error")),
    "read_work": _object({"work_id": _STRING}, ("work_id",)),
    "list_work": _object({"state": _STRING, "after_id": _STRING,
                          "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
}
_WORK_HELP = {
    "create_work": "Persist a concrete goal with optional existing dependencies and parent provenance. Does not spawn an unbounded agent or change its parent's dependencies.",
    "delegate_work": "Delegate a concrete goal to a child logical agent. Requires your current work lease; the host derives parent and depth, enforces lifetime child limits, and schedules an independent conversation. Include a focused brief, expected deliverable, acceptance criteria and actual evidence IDs. This does not wait for the child; use wait_work when you need its result.",
    "claim_work": "Atomically claim one ready goal, or return your existing active lease. Save work_id and lease_token. If nothing is ready, end the turn and wait for the supervisor.",
    "renew_work": "Renew your current work lease before it expires. An expired or replaced token cannot be renewed.",
    "wait_work": "Release your lease while waiting for dependencies. all_success requires every dependency to succeed; all_settled waits for every dependency to end and lets you inspect failures. Cycles are rejected. In a hierarchical campaign this replaces the current wait set, parks the conversation without using a model slot, and later returns resume_claim with a new lease token.",
    "complete_work": "Finish workflow work with an agent-authored result and actual tool effect IDs. This does not establish a mathematical proof or independently validate the result.",
    "fail_work": "Record a failed attempt and optionally requeue within the host retry limit. Include the observed failure; unavailable tools or exhausted routes must remain explicit.",
    "read_work": "Read a goal, dependencies, attempts and persisted result. The lease token is visible only to its current owner.",
    "list_work": "Page through persisted work by ID and optional state. Use next_cursor as after_id; no lease tokens are disclosed.",
}


def _skills(store):
    from .skills import SkillRegistry
    return SkillRegistry.from_snapshot(store.root / "skills",
                                       expected_sha256=store.spec["skill_snapshot_sha256"])


def tool_catalog(spec):
    from .capabilities import capability_catalog
    hierarchical = "delegation" in spec
    hidden = "create_work" if hierarchical else "delegate_work"
    result = [{"name": name, "description": _WORK_HELP[name], "parameters": schema}
              for name, schema in _WORK_SCHEMAS.items() if name != hidden]
    if hierarchical:
        for definition in result:
            if definition["name"] == "claim_work":
                definition["description"] = (
                    "Read the supervisor-acquired lease of the logical agent work assigned to this conversation. "
                    "The native runtime does not let this tool acquire a new lease or claim another agent's work. "
                    "If no lease is available, end your turn for the supervisor to schedule you. It supplies "
                    "your initial claim and a fresh resume_claim after wait_work.")
    result.extend([
        {"name": "research_context", "description": "Read the current goal, work availability, progress and the pinned skill/tool catalog.",
         "parameters": _object()},
        {"name": "list_skills", "description": "Discover pinned skill names, descriptions and package hashes without loading their instructions.",
         "parameters": _object()},
        {"name": "read_skill", "description": "Load one pinned skill or named text resource on demand. Instructions do not grant tools or execute bundled scripts.",
         "parameters": _object({"name": _STRING, "resource": _STRING}, ("name",))},
        {"name": "read_effect", "description": "Read persisted host tool output, input/result hashes and evidence. Interrupted effects are unknown outcomes, never automatically replayed.",
         "parameters": _object({"effect_id": _STRING}, ("effect_id",))},
        {"name": "list_effects", "description": "Discover a work item's tool receipts across all attempts, including results committed before a launcher crash. Read a receipt by ID before repeating work; paginate with next_cursor as after_id.",
         "parameters": _object({"work_id": _STRING, "after_id": _STRING,
                                 "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ("work_id",))},
        {"name": "read_artifact", "description": "Read a bounded UTF-8 artifact from a completed host effect's exact artifact index. Hashes are rechecked; candidate text remains unverified data.",
         "parameters": _object({"effect_id": _STRING, "path": _STRING}, ("effect_id", "path"))},
    ])
    reserved = {definition["name"] for definition in result}
    for definition in capability_catalog(spec):
        if definition["name"] in reserved:
            raise ValueError("A capability cannot shadow a kernel tool")
        result.append({**definition, "parameters": _object(
            {**_LEASE, "arguments": definition["parameters"]}, (*_LEASE, "arguments"))})
        reserved.add(definition["name"])
    return result


def context(store):
    from .capabilities import validate_capabilities
    validate_capabilities(store.spec)
    result = store.context()
    result["tool_catalog"] = tool_catalog(store.spec)
    result["skill_catalog"] = _skills(store).catalog()
    result["skill_snapshot_sha256"] = store.spec["skill_snapshot_sha256"]
    return result


def initialize(task: Path, campaign: Path, *, skill_roots=(), allow_local=False):
    from .capabilities import prepare_capabilities
    from .kernel import GenericStore, validate_contract
    from .skills import BUILTIN_ROOT, SkillRegistry
    if task.stat().st_size > 1024 * 1024:
        raise ValueError("Task contract exceeds 1 MiB")
    spec = json.loads(task.read_text(encoding="utf-8"))
    spec = validate_contract(prepare_capabilities(spec, allow_local=allow_local))
    campaign = Path(campaign)
    if campaign.is_symlink() or (campaign.exists() and any(campaign.iterdir())):
        raise ValueError("Campaign directory must be new or empty")
    registry = (SkillRegistry.from_roots([BUILTIN_ROOT, *skill_roots])
                if skill_roots else SkillRegistry.builtin())
    selected = spec.get("skills")
    if selected is not None and not isinstance(selected, list):
        raise ValueError("skills must be a list of selected package names")
    snapshot = registry.snapshot(campaign / "skills", names=selected)
    spec["skill_snapshot_sha256"] = snapshot["sha256"]
    spec["skills"] = [item["name"] for item in _snapshot_catalog(campaign, snapshot["sha256"])]
    return GenericStore.create(campaign, spec)


def _snapshot_catalog(root, sha256):
    from .skills import SkillRegistry
    return SkillRegistry.from_snapshot(root / "skills", expected_sha256=sha256).catalog()


def _check_keys(arguments, schema):
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object")
    if set(arguments) - set(schema["properties"]):
        raise ValueError("Unexpected tool arguments")
    if set(schema.get("required", ())) - set(arguments):
        raise ValueError("Missing required tool arguments")


def invoke(store, envelope):
    from .capabilities import validate_capabilities
    validate_capabilities(store.spec)
    if (not isinstance(envelope, dict)
            or set(envelope) != {"tool", "arguments", "owner", "request_key"}):
        raise ValueError("Expected the native tool envelope with authoritative owner and request_key")
    name, args = envelope["tool"], envelope["arguments"]
    owner, key = envelope["owner"], envelope["request_key"]
    if (not isinstance(owner, str) or not owner or len(owner) > 256
            or not isinstance(key, str) or not key or len(key) > 512):
        raise ValueError("Invalid transport identity")
    definitions = {entry["name"]: entry for entry in tool_catalog(store.spec)}
    if not isinstance(name, str) or name not in definitions:
        raise ValueError("Tool is not enabled in the immutable campaign contract")
    _check_keys(args, definitions[name]["parameters"])
    if name == "research_context":
        return context(store)
    if name == "list_skills":
        return {"skills": _skills(store).catalog()}
    if name == "read_skill":
        return _skills(store).load(args["name"], args.get("resource", "SKILL.md"))
    if name == "read_effect":
        return store.effect(args["effect_id"])
    if name == "list_effects":
        return store.list_effects(**args)
    if name == "read_artifact":
        from .capabilities import read_artifact
        return read_artifact(store.root, store.effect(args["effect_id"]), args["path"])
    if name == "list_work":
        return store.list_work(**args)
    if name in _WORK_SCHEMAS:
        if name in {"create_work", "delegate_work"} and "skill_names" in args:
            if (not isinstance(args["skill_names"], list)
                    or any(skill not in store.spec["skills"] for skill in args["skill_names"])):
                raise ValueError("Work references an unavailable skill")
        return store.dispatch_work(name, args, owner=owner, request_key=key)

    from .capabilities import execute_capability
    work_id, token, arguments = args["work_id"], args["lease_token"], args["arguments"]
    if not isinstance(arguments, dict):
        raise ValueError("Capability arguments must be an object")
    effect = store.reserve_effect(owner, work_id, token, name, arguments, key)
    if not effect["execute"]:
        return effect

    def heartbeat():
        store.dispatch_work("renew_work", {"work_id": work_id, "lease_token": token},
                            owner=owner, request_key="heartbeat:" + uuid.uuid4().hex)

    try:
        output = execute_capability(name, arguments, root=store.root, spec=store.spec,
                                    effect_id=effect["effect_id"], heartbeat=heartbeat)
    except Exception as error:
        # If the lease was lost, finish_effect rejects the stale completion too;
        # the recovery record remains interrupted rather than fabricated success.
        return store.finish_effect(effect["effect_id"], owner, work_id, token,
                                   error=f"{type(error).__name__}: {error}"[:4096])
    # This is the host adapter envelope, not candidate-authored result_json.
    # Persist its stop requirement in the same transaction as the receipt.
    stop_reason = ("host_tool_cleanup_failed" if isinstance(output, dict)
                   and output.get("requires_campaign_stop") is True else None)
    try:
        return store.finish_effect(effect["effect_id"], owner, work_id, token,
                                   result=output, stop_reason=stop_reason)
    except (ValueError, TypeError, OverflowError) as error:
        # A malformed/oversized adapter response must not leave a running receipt.
        # Fencing and storage errors still propagate; do not erase committed data.
        return store.finish_effect(effect["effect_id"], owner, work_id, token,
                                   error=f"Invalid host tool response: {error}"[:4096],
                                   stop_reason=stop_reason)


def dispatch(store, operation, args):
    if not isinstance(args, dict):
        raise ValueError("Operation arguments must be an object")
    if operation == "context":
        return context(store)
    if operation == "invoke_tool":
        return invoke(store, args)
    if operation == "reserve_model_call":
        return store.reserve_model_call(args["request_id"])
    if operation == "record_usage":
        return store.record_usage(args["request_id"], args["input_tokens"], args["output_tokens"])
    if operation == "recover":
        return store.recover()
    if operation == "heartbeat":
        return store.renew_live_leases(args["owners"])
    if operation == "bind_worker":
        return store.bind_worker(args["owner"], args["work_id"])
    if operation == "interrupt_tool":
        return store.interrupt_effect(args["owner"], args["request_key"], args["reason"])
    raise ValueError(f"Operation {operation!r} is not supported by a generic campaign")


def campaign_version(root: Path):
    path = Path(root) / "task.json"
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Task contract exceeds 1 MiB")
    return json.loads(path.read_text(encoding="utf-8")).get("schema_version")
