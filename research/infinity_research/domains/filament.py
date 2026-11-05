"""Competition data preparation and independent Solar Filament PQ evaluation.

The evaluator follows the published Self Evaluation Notebook v6's hit-matrix
semantics. It deliberately does not replace them with Hungarian matching.
The private truth files are host-owned inputs, never candidate outputs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import struct
import tempfile
from datetime import datetime


METRIC = "solar_pq_v6"
REFERENCE_URL = "https://www.kaggle.com/code/azimahmadzadeh/self-evaluation-notebook"
REFERENCE_SOURCE_SHA256 = "ae7c9445b4eee2b77b216df5c9772fdf92fda15611e98abd7ef5f02a4e380801"
MAX_JSON_BYTES = 64 * 1024 * 1024
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(value))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular JSON file: {path}")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"JSON exceeds {MAX_JSON_BYTES} bytes: {path}")
    def invalid_number(value):
        raise ValueError(f"Non-finite JSON number: {value}")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=invalid_number)


def _positive_int(value, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _decode_counts(value, pixels: int) -> list[int]:
    """Read COCO's signed, differential, five-bit compressed run lengths."""
    if isinstance(value, str):
        counts = []
        position = 0
        while position < len(value):
            number = 0
            shift = 0
            while True:
                if position == len(value) or shift > 60:
                    raise ValueError("Truncated or excessive COCO RLE integer")
                digit = ord(value[position]) - 48
                position += 1
                if not 0 <= digit < 64:
                    raise ValueError("Invalid character in compressed COCO RLE")
                number |= (digit & 31) << shift
                shift += 5
                if not digit & 32:
                    if digit & 16:
                        number -= 1 << shift
                    break
            if len(counts) >= 3:
                number += counts[-2]
            counts.append(number)
            if len(counts) > 2 * pixels + 1:
                raise ValueError("Too many COCO RLE runs")
    elif isinstance(value, list):
        counts = value
    else:
        raise ValueError("RLE counts must be a compressed string or integer list")
    if (not counts or len(counts) > 2 * pixels + 1
            or any(type(x) is not int or x < 0 for x in counts)
            or sum(counts) != pixels):
        raise ValueError("RLE runs must be nonnegative integers covering the exact image size")
    return list(counts)


def _encode_counts(counts: list[int]) -> str:
    chars = []
    for index, length in enumerate(counts):
        value = length - counts[index - 2] if index >= 3 else length
        while True:
            digit = value & 31
            value >>= 5
            more = value != (-1 if digit & 16 else 0)
            chars.append(chr(48 + digit + (32 if more else 0)))
            if not more:
                break
    return "".join(chars)


def _normalize_rle(segmentation, height: int, width: int) -> dict:
    if isinstance(segmentation, list):
        # Polygon conversion belongs to preparation, not arbitrary candidate code.
        try:
            from pycocotools import mask as coco_mask
        except ImportError as exc:
            raise RuntimeError("Polygon annotations require pycocotools; install the filament extra") from exc
        polygons = segmentation
        if polygons and isinstance(polygons[0], (int, float)):
            polygons = [polygons]
        for polygon in polygons:
            if (not isinstance(polygon, list) or len(polygon) < 6 or len(polygon) % 2
                    or any(isinstance(x, bool) or not isinstance(x, (int, float))
                           or not math.isfinite(x) for x in polygon)):
                raise ValueError("Invalid COCO polygon coordinates")
        if not polygons:
            return {"size": [height, width], "counts": [height * width]}
        converted = coco_mask.frPyObjects(polygons, height, width)
        segmentation = coco_mask.merge(converted)
        segmentation = {"size": list(segmentation["size"]),
                        "counts": segmentation["counts"].decode("ascii")}
    if (not isinstance(segmentation, dict) or segmentation.get("size") != [height, width]
            or any(type(value) is not int for value in segmentation.get("size", []))):
        raise ValueError(f"RLE size must be [{height}, {width}]")
    counts = _decode_counts(segmentation.get("counts"), height * width)
    return {"size": [height, width], "counts": counts}


def _runs(rle: dict) -> tuple[list[tuple[int, int]], int]:
    intervals = []
    offset = 0
    area = 0
    for index, length in enumerate(rle["counts"]):
        if index % 2 and length:
            intervals.append((offset, offset + length))
            area += length
        offset += length
    return intervals, area


def _intersection(left, right) -> int:
    i = j = total = 0
    while i < len(left) and j < len(right):
        a, b = left[i]
        c, d = right[j]
        total += max(0, min(b, d) - max(a, c))
        if b <= d:
            i += 1
        else:
            j += 1
    return total


def _count_view(ground_truth_runs, prediction_runs) -> dict:
    """The notebook's per-entry hit-matrix calculation, including empty entries."""
    hit_pred, hit_gt = set(), set()
    tp = 0
    iou_sum = 0.0
    for g, (gt_runs, gt_area) in enumerate(ground_truth_runs):
        for p, (pred_runs, pred_area) in enumerate(prediction_runs):
            intersection = _intersection(gt_runs, pred_runs)
            union = gt_area + pred_area - intersection
            # The notebook obtains float32 IoUs then converts hits to Python floats.
            iou = struct.unpack("f", struct.pack("f", intersection / union))[0] if union else 0.0
            if iou > .5:
                tp += 1
                iou_sum += iou
                hit_pred.add(p)
                hit_gt.add(g)
    fp, fn = len(prediction_runs) - len(hit_pred), len(ground_truth_runs) - len(hit_gt)
    denominator = tp + .5 * fp + .5 * fn
    return {"tp": tp, "fp": fp, "fn": fn, "iou_sum": iou_sum,
            "pq": iou_sum / denominator if denominator else 0.0}


def _date(stem: str) -> str:
    if not re.match(r"^\d{8}", stem):
        raise ValueError(f"Image {stem!r} lacks a YYYYMMDD date; supply date-bearing observation names")
    try:
        datetime.strptime(stem[:8], "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"Image {stem!r} has an invalid observation date") from exc
    return stem[:8]


def _image_index(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"Image directory is missing: {root}")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Images must be regular files within their root: {path}")
        if path.stem in result:
            raise ValueError(f"Ambiguous physical image stem: {path.stem}")
        result[path.stem] = path
    if not result:
        raise ValueError(f"No JPEG or PNG images found in {root}")
    return result


def _image_shape(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Data preparation requires Pillow; install the filament extra") from exc
    with Image.open(path) as image:
        width, height = image.size
        image.verify()
    if width <= 0 or height <= 0 or width * height > 64 * 1024 * 1024:
        raise ValueError(f"Unsupported image dimensions: {path}")
    return height, width


def _link_copy(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def prepare_dataset(images_root: Path, annotations_path: Path, output_root: Path, *,
                    seed=42, validation_fraction=.2, holdout_fraction=.1,
                    test_images: Path | None = None) -> dict:
    """Create public search/final/test views plus host-only validation truth.

    Dates stay together. Byte-identical observations across dates additionally
    join their date groups, so they cannot leak across validation boundaries.
    Existing output directories are refused, rather than silently re-split.
    """
    images_root, annotations_path, output_root = map(Path, (images_root, annotations_path, output_root))
    if (isinstance(seed, bool) or not isinstance(seed, int)
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                       and math.isfinite(x) and 0 < x < 1
                       for x in (validation_fraction, holdout_fraction))
            or validation_fraction + holdout_fraction >= 1):
        raise ValueError("Require an integer seed and positive validation/holdout fractions summing to < 1")
    if output_root.exists():
        raise FileExistsError(f"Prepared output already exists: {output_root}")
    raw = _read_json(annotations_path)
    if not isinstance(raw, dict) or not isinstance(raw.get("images"), list) or not isinstance(raw.get("annotations"), list):
        raise ValueError("Expected a COCO JSON object with images and annotations arrays")
    paths = _image_index(images_root)
    observations, view_to_stem, views = {}, {}, {}
    for image in raw["images"]:
        if not isinstance(image, dict) or not isinstance(image.get("file_name"), str):
            raise ValueError("Each COCO image needs a file_name")
        view_id = image.get("id")
        if isinstance(view_id, bool) or not isinstance(view_id, (str, int)):
            raise ValueError("Each COCO image needs a string or integer id")
        view_id = str(view_id)
        if view_id in views:
            raise ValueError(f"Duplicate COCO image id: {view_id}")
        file_name = image["file_name"]
        if "\\" in file_name or Path(file_name).is_absolute() or ".." in Path(file_name).parts:
            raise ValueError(f"Unsafe COCO file_name: {file_name}")
        stem = Path(file_name).stem
        if stem not in paths:
            raise FileNotFoundError(f"Annotated image {stem!r} is missing in {images_root}")
        height = _positive_int(image.get("height"), "image height")
        width = _positive_int(image.get("width"), "image width")
        if stem not in observations:
            if _image_shape(paths[stem]) != (height, width):
                raise ValueError(f"Image dimensions differ from annotations: {stem}")
            observations[stem] = {"image_id": stem, "width": width, "height": height,
                                  "date": _date(stem), "sha256": _sha(paths[stem])}
        elif (height, width) != (observations[stem]["height"], observations[stem]["width"]):
            raise ValueError(f"Annotator views disagree about dimensions: {stem}")
        view_to_stem[view_id] = stem
        views[view_id] = {"view_id": view_id, "instances": []}
    if not observations:
        raise ValueError("No annotated observations")
    annotation_ids = set()
    for annotation in raw["annotations"]:
        if not isinstance(annotation, dict):
            raise ValueError("COCO annotations must be objects")
        annotation_id = annotation.get("id")
        if isinstance(annotation_id, bool) or not isinstance(annotation_id, (str, int)):
            raise ValueError("Each COCO annotation needs a string or integer id")
        if str(annotation_id) in annotation_ids:
            raise ValueError(f"Duplicate annotation id: {annotation_id}")
        annotation_ids.add(str(annotation_id))
        view_id = str(annotation.get("image_id"))
        if view_id not in views:
            raise ValueError(f"Annotation references unknown image_id: {view_id}")
        observation = observations[view_to_stem[view_id]]
        views[view_id]["instances"].append(_normalize_rle(
            annotation.get("segmentation"), observation["height"], observation["width"]))

    # Union date groups connected by identical image bytes.
    parents = {value["date"]: value["date"] for value in observations.values()}
    def representative(date):
        while parents[date] != date:
            parents[date] = parents[parents[date]]
            date = parents[date]
        return date
    hashes = {}
    for value in observations.values():
        date, digest = value["date"], value["sha256"]
        if digest in hashes:
            parents[representative(date)] = representative(hashes[digest])
        else:
            hashes[digest] = date
    grouped = {}
    for stem, value in sorted(observations.items()):
        grouped.setdefault(representative(value["date"]), []).append(stem)
    groups = [grouped[key] for key in sorted(grouped)]
    if len(groups) < 3:
        raise ValueError("Need at least three independent date/content groups for train, validation and holdout")
    random.Random(seed).shuffle(groups)
    n_validation = max(1, round(len(groups) * validation_fraction))
    n_holdout = max(1, round(len(groups) * holdout_fraction))
    while n_validation + n_holdout >= len(groups):
        if n_validation >= n_holdout and n_validation > 1:
            n_validation -= 1
        else:
            n_holdout -= 1
    partitions = {
        "validation": sorted(x for group in groups[:n_validation] for x in group),
        "holdout": sorted(x for group in groups[n_validation:n_validation+n_holdout] for x in group),
        "train": sorted(x for group in groups[n_validation+n_holdout:] for x in group),
    }
    test = {}
    if test_images is not None:
        test_paths = _image_index(Path(test_images))
        for stem, path in test_paths.items():
            digest = _sha(path)
            if stem in observations or digest in hashes:
                raise ValueError(f"Test image overlaps labelled observations: {stem}")
            height, width = _image_shape(path)
            test[stem] = {"image_id": stem, "height": height, "width": width, "sha256": digest}
    else:
        test_paths = {}
    annotation_hash = _sha(annotations_path)
    dataset_id = hashlib.sha256(_json_bytes({"annotations": annotation_hash,
        "observations": observations, "test": test})).hexdigest()
    split_id = hashlib.sha256(_json_bytes({"dataset": dataset_id, "seed": seed,
        "partitions": partitions})).hexdigest()
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_root.name}-", dir=output_root.parent))
    try:
        public = staging / "public"
        private = staging / "private"
        public.mkdir()
        private.mkdir(mode=0o700)
        for split in ("train", "validation"):
            destination = public / split / "images"
            destination.mkdir(parents=True)
            for stem in partitions[split]:
                shutil.copy2(paths[stem], destination / paths[stem].name)
        training_views = {view for view, stem in view_to_stem.items() if stem in set(partitions["train"])}
        train_coco = {key: raw[key] for key in ("info", "licenses", "categories") if key in raw}
        train_coco["images"] = [dict(image, file_name=f"images/{paths[view_to_stem[str(image['id'])]].name}")
                                for image in raw["images"] if str(image["id"]) in training_views]
        train_coco["annotations"] = [ann for ann in raw["annotations"] if str(ann["image_id"]) in training_views]
        _write_json(public / "train" / "annotations.json", train_coco)
        def entries(split, stems, records, source_paths):
            return [dict(records[stem], path=f"{split}/images/{source_paths[stem].name}") for stem in stems]
        train_entries = entries("train", partitions["train"], observations, paths)
        def manifest(split, image_entries):
            return {"schema_version": 1, "domain": "filament", "competition": "filament-segmentation-2026",
                "dataset_fingerprint": dataset_id, "split_fingerprint": split_id,
                "train": {"images": train_entries, "annotations": "train/annotations.json"},
                "predict": {"split": split, "images": image_entries},
                "metric": {"name": METRIC, "direction": "maximize", "reference": REFERENCE_URL,
                           "reference_source_sha256": REFERENCE_SOURCE_SHA256},
                "predictions_format": {"file": "predictions.json", "schema_version": 1,
                    "coverage": "exact processed_images and one predictions record per predict image",
                    "instances": "COCO RLE objects with size and compressed-string or integer-list counts"}}
        validation_entries = entries("validation", partitions["validation"], observations, paths)
        search_manifest = manifest("validation", validation_entries)
        if test:
            destination = public / "test" / "images"
            destination.mkdir(parents=True)
            for stem, path in test_paths.items():
                shutil.copy2(path, destination / path.name)
            search_manifest["test"] = {"images": entries("test", sorted(test), test, test_paths)}
        _write_json(public / "manifest.json", search_manifest)
        for split in ("validation", "holdout"):
            truth = {"schema_version": 1, "split": split, "dataset_fingerprint": dataset_id,
                     "split_fingerprint": split_id, "images": []}
            for stem in partitions[split]:
                truth["images"].append(dict(observations[stem], views=[views[view] for view in views
                                                               if view_to_stem[view] == stem]))
            _write_json(private / f"{split}_truth.json", truth)
        final_public = staging / "public_final"
        (final_public / "holdout" / "images").mkdir(parents=True)
        shutil.copytree(public / "train", final_public / "train", copy_function=_link_copy)
        for stem in partitions["holdout"]:
            shutil.copy2(paths[stem], final_public / "holdout" / "images" / paths[stem].name)
        _write_json(final_public / "manifest.json", manifest("holdout",
                    entries("holdout", partitions["holdout"], observations, paths)))
        if test:
            test_public = staging / "public_test"
            test_public.mkdir()
            shutil.copytree(public / "train", test_public / "train", copy_function=_link_copy)
            shutil.copytree(public / "test", test_public / "test", copy_function=_link_copy)
            _write_json(test_public / "manifest.json", manifest("test", entries("test", sorted(test), test, test_paths)))
        seals = {str(path.relative_to(staging)): _sha(path)
                 for path in sorted(staging.rglob("*")) if path.is_file()}
        _write_json(private / "preparation.json", {"schema_version": 1,
            "dataset_fingerprint": dataset_id, "split_fingerprint": split_id,
            "source_annotations_sha256": annotation_hash, "seed": seed,
            "partitions": partitions, "files": seals})
        staging.rename(output_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=False)
        raise
    return {"prepared_root": str(output_root.resolve()), "dataset_fingerprint": dataset_id,
            "split_fingerprint": split_id, "counts": {key: len(value) for key, value in partitions.items()},
            "test_images": len(test), "metric": METRIC}


def _verify_file(prepared_root: Path, relative: str, seal: dict) -> Path:
    path = prepared_root / relative
    expected = seal.get("files", {}).get(relative)
    if path.is_symlink() or not path.is_file() or not expected or _sha(path) != expected:
        raise ValueError(f"Prepared data changed or is missing: {relative}; prepare a new dataset version")
    return path


def _load_predictions(output_dir: Path, images: list[dict]) -> dict[str, list[dict]]:
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        raise ValueError("Candidate output directory cannot be a symlink")
    raw = _read_json(output_dir / "predictions.json")
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("predictions.json requires schema_version=1")
    expected = {image["image_id"]: image for image in images}
    processed = raw.get("processed_images")
    if (not isinstance(processed, list) or not all(isinstance(x, str) for x in processed)
            or len(processed) != len(set(processed)) or set(processed) != set(expected)):
        raise ValueError("processed_images must cover the exact requested image set, without duplicates")
    records = raw.get("predictions")
    if not isinstance(records, list) or len(records) != len(expected):
        raise ValueError("Provide exactly one predictions record per requested image, even with no instances")
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Prediction records must be objects")
        image_id = record.get("image_id")
        if not isinstance(image_id, str) or image_id not in expected or image_id in result:
            raise ValueError(f"Duplicate or unknown prediction image_id: {image_id}")
        instances = record.get("instances")
        if not isinstance(instances, list) or len(instances) > 1000:
            raise ValueError("instances must be an array of at most 1000 RLE masks per image")
        image = expected[image_id]
        if any(not isinstance(mask, dict) for mask in instances):
            raise ValueError("Predictions must be RLE objects, not polygon arrays")
        result[image_id] = [_normalize_rle(mask, image["height"], image["width"]) for mask in instances]
    return result


def evaluate(prepared_root: Path, output_dir: Path, *, split="validation") -> dict:
    """Recompute PQ from predictions and host-owned labels; trust no reported score."""
    if split not in {"validation", "holdout"}:
        raise ValueError("Only validation and sealed holdout have evaluation labels")
    prepared_root = Path(prepared_root)
    seal = _read_json(prepared_root / "private" / "preparation.json")
    truth_path = _verify_file(prepared_root, f"private/{split}_truth.json", seal)
    public_name = "public" if split == "validation" else "public_final"
    _verify_file(prepared_root, f"{public_name}/manifest.json", seal)
    truth = _read_json(truth_path)
    predictions = _load_predictions(Path(output_dir), truth["images"])
    total_tp = total_fp = total_fn = 0
    total_iou = 0.0
    per_view = []
    unscored_empty_passes = []
    for image in truth["images"]:
        prediction_runs = [_runs(mask) for mask in predictions[image["image_id"]]]
        for view in image["views"]:
            if not view["instances"]:
                # get_overlap_df groups the GT CSV's rows. A pass without any
                # GT rows cannot enter that groupby, so it contributes no FP.
                # Preserve the observation and its predictions as diagnostics.
                unscored_empty_passes.append({"image_id": image["image_id"],
                    "view_id": view["view_id"], "predicted_instances": len(prediction_runs)})
                continue
            ground_truth_runs = [_runs(mask) for mask in view["instances"]]
            counts = _count_view(ground_truth_runs, prediction_runs)
            per_view.append({"image_id": image["image_id"], "view_id": view["view_id"], **counts})
            total_tp += counts["tp"]
            total_fp += counts["fp"]
            total_fn += counts["fn"]
            total_iou += counts["iou_sum"]
    denominator = total_tp + .5 * total_fp + .5 * total_fn
    score = total_iou / denominator if denominator else 0.0
    return {"metric": METRIC, "score": score, "details": {
        "split": split, "images": len(truth["images"]), "annotator_views": len(per_view),
        "scored_images": len({row["image_id"] for row in per_view}),
        "unscored_empty_passes": unscored_empty_passes,
        "tp": total_tp, "fp": total_fp, "fn": total_fn, "iou_sum": total_iou,
        "dataset_fingerprint": seal["dataset_fingerprint"], "split_fingerprint": seal["split_fingerprint"],
        "predictions_sha256": _sha(Path(output_dir) / "predictions.json"),
        "reference": REFERENCE_URL, "reference_source_sha256": REFERENCE_SOURCE_SHA256,
        "parity": "Published v6 GT-row grouping and hit-matrix semantics; synthetic parity tests, no live leaderboard certification",
        "per_annotator_image": per_view}}


def export_submission(prepared_root: Path, output_dir: Path, output_csv: Path) -> dict:
    """Validate exact test coverage and export COCO counts; never upload to Kaggle."""
    prepared_root, output_csv = Path(prepared_root), Path(output_csv)
    seal = _read_json(prepared_root / "private" / "preparation.json")
    manifest_path = _verify_file(prepared_root, "public_test/manifest.json", seal)
    manifest = _read_json(manifest_path)
    images = manifest["predict"]["images"]
    predictions = _load_predictions(Path(output_dir), images)
    if output_csv.is_symlink() or output_csv.exists():
        raise FileExistsError(f"Submission destination already exists: {output_csv}")
    rows = []
    for image in images:
        if [image["height"], image["width"]] != [2048, 2048]:
            raise ValueError("Kaggle submission requires 2048 x 2048 masks")
        for index, mask in enumerate(predictions[image["image_id"]], 1):
            if sum(mask["counts"][1::2]) == 0:
                raise ValueError("Empty-mask placeholders are not filaments; use an empty instances array")
            rows.append((f"{image['image_id']}_{index}", _encode_counts(mask["counts"])))
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["filament_id", "segmentation_rle"])
        writer.writerows(rows)
    return {"path": str(output_csv.resolve()), "images": len(images), "filaments": len(rows),
            "sha256": _sha(output_csv), "uploaded": False}
