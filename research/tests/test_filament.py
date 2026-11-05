from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
import random
import tempfile
import unittest

from infinity_research.domains import filament


def rle(counts, height=2, width=2):
    return {"size": [height, width], "counts": counts}


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prepared = self.root / "prepared"
        self.output = self.root / "output"
        self.output.mkdir()

    def fixture(self, views, predictions, height=2, width=2):
        image = {"image_id": "20260101120000Mh", "height": height, "width": width,
                 "views": [{"view_id": str(i), "instances": masks} for i, masks in enumerate(views)]}
        filament._write_json(self.prepared / "private" / "validation_truth.json",
                             {"images": [image]})
        filament._write_json(self.prepared / "public" / "manifest.json", {"schema_version": 1})
        paths = ["private/validation_truth.json", "public/manifest.json"]
        filament._write_json(self.prepared / "private" / "preparation.json", {
            "dataset_fingerprint": "fixture", "split_fingerprint": "fixture-split",
            "files": {path: filament._sha(self.prepared / path) for path in paths}})
        filament._write_json(self.output / "predictions.json", {
            "schema_version": 1, "processed_images": [image["image_id"]],
            "predictions": [{"image_id": image["image_id"], "instances": predictions}],
            "score": 999999})  # Self-reported scores never affect the evaluator.

    def test_perfect_and_missing_predictions(self):
        self.fixture([[rle([0, 4])]], [rle([0, 4])])
        result = filament.evaluate(self.prepared, self.output)
        self.assertEqual((result["metric"], result["score"]), ("solar_pq_v6", 1.0))
        self.fixture([[rle([0, 4])]], [])
        result = filament.evaluate(self.prepared, self.output)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["details"]["fn"], 1)

    def test_strict_threshold_not_greater_or_equal(self):
        self.fixture([[rle([0, 4])]], [rle([0, 2, 2])])
        result = filament.evaluate(self.prepared, self.output)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual((result["details"]["tp"], result["details"]["fp"], result["details"]["fn"]), (0, 1, 1))

    def test_notebook_hit_semantics_are_not_hungarian(self):
        self.fixture([[rle([0, 4]), rle([0, 4])]], [rle([0, 4])])
        result = filament.evaluate(self.prepared, self.output)
        self.assertEqual(result["details"]["tp"], 2)
        self.assertEqual(result["score"], 1.0)

    def test_annotator_passes_pool_counts(self):
        # First pass: one match. Second pass: same match and one missed instance.
        self.fixture([[rle([0, 2, 2])], [rle([0, 2, 2]), rle([2, 2])]], [rle([0, 2, 2])])
        result = filament.evaluate(self.prepared, self.output)
        self.assertEqual(result["details"]["annotator_views"], 2)
        self.assertAlmostEqual(result["score"], 2 / 2.5)

    def test_zero_gt_grouper_and_standalone_empty_counts(self):
        self.fixture([[]], [])
        self.assertEqual(filament.evaluate(self.prepared, self.output)["score"], 0.0)
        self.fixture([[]], [rle([0, 4])])
        result = filament.evaluate(self.prepared, self.output)
        self.assertEqual((result["score"], result["details"]["fp"]), (0.0, 0))
        self.assertEqual(result["details"]["unscored_empty_passes"], [
            {"image_id": "20260101120000Mh", "view_id": "0", "predicted_instances": 1}])
        self.assertEqual(filament._count_view([], [filament._runs(rle([0, 4]))])["fp"], 1)
        self.assertEqual(filament._count_view([], [])["pq"], 0.0)

    def test_coverage_is_independent_of_instance_count(self):
        self.fixture([[rle([0, 4])]], [])
        path = self.output / "predictions.json"
        value = json.loads(path.read_text())
        value["processed_images"].append("unexpected-image")
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "exact requested image set"):
            filament.evaluate(self.prepared, self.output)

    def test_missing_or_duplicate_image_record_is_rejected(self):
        self.fixture([[rle([0, 4])]], [])
        path = self.output / "predictions.json"
        value = json.loads(path.read_text())
        value["predictions"] = []
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "one predictions record"):
            filament.evaluate(self.prepared, self.output)

    def test_invalid_rle_and_mutated_truth_are_rejected(self):
        self.fixture([[rle([0, 4])]], [rle([0, 5])])
        with self.assertRaisesRegex(ValueError, "exact image size"):
            filament.evaluate(self.prepared, self.output)
        self.fixture([[rle([0, 4])]], [rle([0, 4])])
        (self.prepared / "private" / "validation_truth.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Prepared data changed"):
            filament.evaluate(self.prepared, self.output)

    def test_output_symlink_and_nonfinite_json_are_rejected(self):
        self.fixture([[rle([0, 4])]], [])
        path = self.output / "predictions.json"
        path.unlink()
        path.symlink_to(self.prepared / "private" / "validation_truth.json")
        with self.assertRaisesRegex(ValueError, "regular JSON file"):
            filament.evaluate(self.prepared, self.output)
        path.unlink()
        path.write_text('{"schema_version":1,"bogus":NaN}')
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            filament.evaluate(self.prepared, self.output)

    def test_run_intersections_and_compression(self):
        rng = random.Random(7341)
        # An independent pixel-set oracle catches run-boundary and signed-delta errors.
        for _ in range(100):
            a = [rng.randrange(2) for _ in range(35)]
            b = [rng.randrange(2) for _ in range(35)]
            def counts(values):
                result, active, length = [], 0, 0
                for value in values:
                    if value == active:
                        length += 1
                    else:
                        result.append(length)
                        active, length = value, 1
                return result + [length]
            ca, cb = counts(a), counts(b)
            encoded = filament._encode_counts(ca)
            self.assertEqual(filament._decode_counts(encoded, 35), ca)
            ra, aa = filament._runs(rle(ca, 5, 7))
            rb, ab = filament._runs(rle(cb, 5, 7))
            self.assertEqual((aa, ab), (sum(a), sum(b)))
            self.assertEqual(filament._intersection(ra, rb), sum(x and y for x, y in zip(a, b)))
        for bad in ("!", "P", [4, -1, 1], [True, 3]):
            with self.assertRaises(ValueError):
                filament._decode_counts(bad, 4)

    def test_export_requires_test_coverage_and_real_instances(self):
        images = [{"image_id": "20260201120000Mh", "height": 2048, "width": 2048},
                  {"image_id": "20260202120000Mh", "height": 2048, "width": 2048}]
        manifest_path = self.prepared / "public_test" / "manifest.json"
        filament._write_json(manifest_path, {"predict": {"images": images}})
        filament._write_json(self.prepared / "private" / "preparation.json", {
            "files": {"public_test/manifest.json": filament._sha(manifest_path)}})
        filament._write_json(self.output / "predictions.json", {
            "schema_version": 1, "processed_images": [image["image_id"] for image in images],
            "predictions": [
                {"image_id": images[0]["image_id"], "instances": [rle([0, 1, 2048*2048-1], 2048, 2048)]},
                {"image_id": images[1]["image_id"], "instances": []}]})
        destination = self.root / "submission.csv"
        result = filament.export_submission(self.prepared, self.output, destination)
        self.assertEqual((result["images"], result["filaments"], result["uploaded"]), (2, 1, False))
        with destination.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["filament_id"], "20260201120000Mh_1")
        self.assertEqual(filament._decode_counts(rows[0]["segmentation_rle"], 2048*2048), [0, 1, 2048*2048-1])
        with self.assertRaises(FileExistsError):
            filament.export_submission(self.prepared, self.output, destination)


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is an optional data-preparation dependency")
class PreparationTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.annotations = self.root / "train.json"
        records, annotations = [], []
        for index in range(12):
            stem = f"202601{index+1:02d}120000Mh"
            Image.new("L", (8, 8), color=10*index).save(self.images / f"{stem}.png")
            for annotator in ("a", "b"):
                view = f"{annotator}-{index}"
                records.append({"id": view, "file_name": f"{stem}.png", "height": 8, "width": 8})
                annotations.append({"id": f"ann-{view}", "image_id": view,
                                    "segmentation": rle([9, 2, 53], 8, 8)})
        self.raw = {"images": records, "annotations": annotations}
        filament._write_json(self.annotations, self.raw)

    def test_preparation_separates_labels_and_reuses_training_partition(self):
        from PIL import Image
        test_images = self.root / "test"
        test_images.mkdir()
        Image.new("L", (8, 8), color=251).save(test_images / "20260201120000Mh.png")
        prepared = self.root / "prepared"
        result = filament.prepare_dataset(self.images, self.annotations, prepared, test_images=test_images)
        self.assertEqual(sum(result["counts"].values()), 12)
        search = json.loads((prepared / "public" / "manifest.json").read_text())
        final = json.loads((prepared / "public_final" / "manifest.json").read_text())
        test = json.loads((prepared / "public_test" / "manifest.json").read_text())
        self.assertEqual(search["train"], final["train"])
        self.assertEqual(search["train"], test["train"])
        train_ids = {image["image_id"] for image in search["train"]["images"]}
        val_ids = {image["image_id"] for image in search["predict"]["images"]}
        holdout_ids = {image["image_id"] for image in final["predict"]["images"]}
        self.assertFalse(train_ids & val_ids or train_ids & holdout_ids or val_ids & holdout_ids)
        train_annotations = json.loads((prepared / "public" / "train" / "annotations.json").read_text())
        self.assertEqual({Path(image["file_name"]).stem for image in train_annotations["images"]}, train_ids)
        self.assertEqual(len(train_annotations["images"]), 2 * len(train_ids))
        for view in ("public", "public_final", "public_test"):
            self.assertFalse(list((prepared / view).rglob("*truth*")))
            self.assertFalse(any(path.is_symlink() for path in (prepared / view).rglob("*")))
        with self.assertRaises(FileExistsError):
            filament.prepare_dataset(self.images, self.annotations, prepared)

    def test_duplicate_content_across_dates_never_crosses_splits(self):
        files = sorted(self.images.iterdir())
        files[1].write_bytes(files[0].read_bytes())
        prepared = self.root / "prepared"
        filament.prepare_dataset(self.images, self.annotations, prepared)
        partitions = json.loads((prepared / "private" / "preparation.json").read_text())["partitions"]
        for ids in partitions.values():
            self.assertEqual(files[0].stem in ids, files[1].stem in ids)

    def test_missing_dates_and_test_label_overlap_are_rejected(self):
        missing = self.raw["images"][0]["file_name"]
        (self.images / missing).rename(self.images / "no-date.png")
        for image in self.raw["images"]:
            if image["file_name"] == missing:
                image["file_name"] = "no-date.png"
        filament._write_json(self.annotations, self.raw)
        with self.assertRaisesRegex(ValueError, "lacks a YYYYMMDD date"):
            filament.prepare_dataset(self.images, self.annotations, self.root / "bad")
        (self.images / "no-date.png").rename(self.images / missing)
        for image in self.raw["images"]:
            if image["file_name"] == "no-date.png":
                image["file_name"] = missing
        filament._write_json(self.annotations, self.raw)
        with self.assertRaisesRegex(ValueError, "Test image overlaps"):
            filament.prepare_dataset(self.images, self.annotations, self.root / "bad", test_images=self.images)


@unittest.skipUnless(importlib.util.find_spec("pycocotools") and importlib.util.find_spec("numpy"),
                     "pycocotools and numpy are optional preparation dependencies")
class CocoInteropTests(unittest.TestCase):
    def test_pinned_notebook_grouping_and_score_golden(self):
        import numpy as np
        from pycocotools import mask as coco_mask
        # Golden value obtained by running the pinned official v6 functions:
        # get_overlap_df -> get_pq_score, with real CPU torch. Only the RLE
        # decoder's image dimensions were changed to 5 x 7 for these fixtures.
        rng = np.random.default_rng(20261105)
        images, predictions = [], []
        def encode(mask):
            encoded = coco_mask.encode(np.asfortranarray(mask, dtype=np.uint8))
            return {"size": [5, 7], "counts": encoded["counts"].decode("ascii")}
        for index in range(100):
            image_id = f"fixture-{index}"
            base = (rng.random((5, 7)) > .5).astype(np.uint8)
            pred = [base] if index % 5 else []
            pred += [(rng.random((5, 7)) > .8).astype(np.uint8) for _ in range(index % 3)]
            views = []
            for annotator in range(1 + index % 3):
                gt = [] if index % 11 == 0 else [base]
                gt += [(rng.random((5, 7)) > .65).astype(np.uint8) for _ in range(annotator)]
                views.append({"view_id": str(annotator), "instances": [
                    filament._normalize_rle(encode(mask), 5, 7) for mask in gt]})
            images.append({"image_id": image_id, "height": 5, "width": 7, "views": views})
            predictions.append({"image_id": image_id, "instances": [encode(mask) for mask in pred]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            filament._write_json(root / "private/validation_truth.json", {"images": images})
            filament._write_json(root / "public/manifest.json", {"schema_version": 1})
            paths = ["private/validation_truth.json", "public/manifest.json"]
            filament._write_json(root / "private/preparation.json", {
                "dataset_fingerprint": "fixture", "split_fingerprint": "fixture",
                "files": {path: filament._sha(root / path) for path in paths}})
            filament._write_json(root / "output/predictions.json", {
                "schema_version": 1, "processed_images": [image["image_id"] for image in images],
                "predictions": predictions})
            result = filament.evaluate(root, root / "output")
        self.assertAlmostEqual(result["score"], 0.3992170194922966, places=12)
        self.assertEqual(result["details"]["annotator_views"], 189)
        self.assertEqual(len(result["details"]["unscored_empty_passes"]), 10)

    def test_compressed_rle_matches_pycocotools_on_asymmetric_masks(self):
        import numpy as np
        from pycocotools import mask as coco_mask
        rng = np.random.default_rng(438)
        masks = [np.zeros((5, 7), dtype=np.uint8), np.ones((5, 7), dtype=np.uint8)]
        masks.extend((rng.random((5, 7)) > .6).astype(np.uint8) for _ in range(100))
        for pixels in masks:
            encoded = coco_mask.encode(np.asfortranarray(pixels))
            compressed = encoded["counts"].decode("ascii")
            counts = filament._decode_counts(compressed, pixels.size)
            self.assertEqual(filament._encode_counts(counts), compressed)
            rebuilt = coco_mask.frPyObjects({"size": [5, 7], "counts": counts}, 5, 7)
            np.testing.assert_array_equal(coco_mask.decode(rebuilt), pixels)

    def test_polygon_conversion_preserves_coco_masks(self):
        import numpy as np
        from pycocotools import mask as coco_mask
        polygon = [[1, 1, 4, 1, 4, 3, 1, 3]]
        normalized = filament._normalize_rle(polygon, 5, 7)
        native = coco_mask.merge(coco_mask.frPyObjects(polygon, 5, 7))
        rebuilt = coco_mask.frPyObjects(normalized, 5, 7)
        np.testing.assert_array_equal(coco_mask.decode(native), coco_mask.decode(rebuilt))


if __name__ == "__main__":
    unittest.main()
