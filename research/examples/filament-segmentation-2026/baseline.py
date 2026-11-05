"""Small, train-from-scratch candidate for the Solar Filament research task.

Run: python main.py /input/manifest.json /output
Only training annotations are read. PQ is computed separately by the host.
"""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import json
import os
from pathlib import Path
import random
import sys

import numpy as np
from PIL import Image
from pycocotools import mask as coco_mask
from scipy import ndimage
import torch
from torch import nn
from torch.nn import functional as F


# Research workers can change these constants and the implementation below.
# They cannot change the host's dataset split or evaluation code.
TRAIN_STEPS = 200
BATCH_SIZE = 4
TRAIN_TILE = 256
INFERENCE_TILE = 512
LEARNING_RATE = 3e-4
FOREGROUND_THRESHOLD = 0.5
MIN_INSTANCE_AREA = 12
BASE_CHANNELS = 16


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Manifest path escapes input directory: {relative}")
    return path


class Block(nn.Sequential):
    def __init__(self, input_channels, output_channels):
        super().__init__(
            nn.Conv2d(input_channels, output_channels, 3, padding=1, bias=False),
            nn.GroupNorm(4, output_channels), nn.SiLU(),
            nn.Conv2d(output_channels, output_channels, 3, padding=1, bias=False),
            nn.GroupNorm(4, output_channels), nn.SiLU(),
        )


class SmallUNet(nn.Module):
    def __init__(self):
        super().__init__()
        width = BASE_CHANNELS
        self.first = Block(1, width)
        self.second = Block(width, 2 * width)
        self.middle = Block(2 * width, 4 * width)
        self.up_second = Block(6 * width, 2 * width)
        self.up_first = Block(3 * width, width)
        self.output = nn.Conv2d(width, 1, 1)

    def forward(self, x):
        a = self.first(x)
        b = self.second(F.max_pool2d(a, 2))
        c = self.middle(F.max_pool2d(b, 2))
        d = self.up_second(torch.cat([F.interpolate(c, size=b.shape[-2:], mode="bilinear", align_corners=False), b], dim=1))
        e = self.up_first(torch.cat([F.interpolate(d, size=a.shape[-2:], mode="bilinear", align_corners=False), a], dim=1))
        return self.output(e)


def decode_annotation(annotation, height, width):
    segmentation = annotation["segmentation"]
    if isinstance(segmentation, list):
        if not segmentation:
            return np.zeros((height, width), dtype=bool)
        polygons = [segmentation] if isinstance(segmentation[0], (int, float)) else segmentation
        rle = coco_mask.merge(coco_mask.frPyObjects(polygons, height, width))
    else:
        rle = dict(segmentation)
        if isinstance(rle["counts"], list):
            rle = coco_mask.frPyObjects(rle, height, width)
        elif isinstance(rle["counts"], str):
            rle["counts"] = rle["counts"].encode("ascii")
    return coco_mask.decode(rle).astype(bool)


def training_sampler(root, manifest):
    annotations_path = inside(root, manifest["train"]["annotations"])
    annotations = json.loads(annotations_path.read_text())
    by_view = defaultdict(list)
    for annotation in annotations["annotations"]:
        by_view[str(annotation["image_id"])].append(annotation)
    views = defaultdict(list)
    for image in annotations["images"]:
        views[Path(image["file_name"]).stem].append(image)
    records = manifest["train"]["images"]
    if not records:
        raise ValueError("Training partition is empty")
    records_by_id = {image["image_id"]: image for image in records}

    @lru_cache(maxsize=4)
    def load_view(image_id, view_id):
        record = records_by_id[image_id]
        with Image.open(inside(root, record["path"])) as image:
            gray = np.asarray(image.convert("L"), dtype=np.float32) / 255.0
        foreground = np.zeros(gray.shape, dtype=bool)
        for annotation in by_view[view_id]:
            foreground |= decode_annotation(annotation, *gray.shape)
        return gray, foreground

    def sample():
        record = random.choice(records)
        view = random.choice(views[record["image_id"]])
        image, target = load_view(record["image_id"], str(view["id"]))
        height, width = image.shape
        tile = TRAIN_TILE
        if height < tile or width < tile:
            padding = ((0, max(0, tile-height)), (0, max(0, tile-width)))
            image = np.pad(image, padding)
            target = np.pad(target, padding)
            height, width = image.shape
        positives = np.argwhere(target)
        if len(positives) and random.random() < 0.75:
            y, x = random.choice(positives)
            y = int(np.clip(y-random.randrange(tile), 0, height-tile))
            x = int(np.clip(x-random.randrange(tile), 0, width-tile))
        else:
            y, x = random.randrange(height-tile+1), random.randrange(width-tile+1)
        a, b = image[y:y+tile, x:x+tile], target[y:y+tile, x:x+tile]
        turns = random.randrange(4)
        a, b = np.rot90(a, turns), np.rot90(b, turns)
        if random.random() < 0.5:
            a, b = np.fliplr(a), np.fliplr(b)
        return np.ascontiguousarray(a[None], dtype=np.float32), np.ascontiguousarray(b[None], dtype=np.float32)
    return sample


def positions(length, tile, stride):
    values = list(range(0, length-tile+1, stride))
    if values[-1] != length-tile:
        values.append(length-tile)
    return values


@torch.inference_mode()
def infer(model, gray, device):
    height, width = gray.shape
    tile = min(INFERENCE_TILE, height, width)
    if tile < 4:
        raise ValueError("Images must be at least 4 x 4 pixels")
    stride = max(1, 3 * tile // 4)
    window = np.maximum(np.outer(np.hanning(tile), np.hanning(tile)), 0.05).astype(np.float32)
    probability = np.zeros_like(gray, dtype=np.float32)
    weights = np.zeros_like(gray, dtype=np.float32)
    for y in positions(height, tile, stride):
        for x in positions(width, tile, stride):
            tensor = torch.from_numpy(np.ascontiguousarray(gray[None, None, y:y+tile, x:x+tile])).to(device)
            with torch.autocast(device_type=device.type, enabled=device.type == "cuda"):
                prediction = model(tensor).sigmoid()
            probability[y:y+tile, x:x+tile] += prediction[0, 0].float().cpu().numpy() * window
            weights[y:y+tile, x:x+tile] += window
    foreground = probability / weights >= FOREGROUND_THRESHOLD
    labels, count = ndimage.label(foreground, structure=np.ones((3, 3), dtype=np.uint8))
    areas = np.bincount(labels.ravel())
    instances = []
    for instance_id in range(1, count+1):
        if areas[instance_id] >= MIN_INSTANCE_AREA:
            encoded = coco_mask.encode(np.asfortranarray(labels == instance_id, dtype=np.uint8))
            instances.append({"size": [height, width], "counts": encoded["counts"].decode("ascii")})
    return instances


def main(manifest_path, output_directory):
    manifest_path = Path(manifest_path).resolve()
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("domain") != "filament" or manifest.get("schema_version") != 1:
        raise ValueError("Expected filament manifest version 1")
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    seed = int(os.environ.get("RESEARCH_SEED", "42"))
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = SmallUNet().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE)
    scaler = torch.amp.GradScaler("cuda", enabled=device.type == "cuda")
    sample = training_sampler(root, manifest)
    model.train()
    for step in range(TRAIN_STEPS):
        pairs = [sample() for _ in range(BATCH_SIZE)]
        x = torch.from_numpy(np.stack([a for a, _ in pairs])).to(device)
        target = torch.from_numpy(np.stack([b for _, b in pairs])).to(device)
        optimizer.zero_grad(set_to_none=True)
        with torch.autocast(device_type=device.type, enabled=device.type == "cuda"):
            logits = model(x)
            probability = logits.sigmoid()
            intersection = (probability * target).sum(dim=(1, 2, 3))
            denominator = probability.sum(dim=(1, 2, 3)) + target.sum(dim=(1, 2, 3))
            dice_loss = 1 - ((2 * intersection + 1) / (denominator + 1)).mean()
            loss = 0.5 * F.binary_cross_entropy_with_logits(logits, target) + 0.5 * dice_loss
        scaler.scale(loss).backward()
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), 2.0)
        scaler.step(optimizer)
        scaler.update()
        if step == 0 or (step + 1) % 20 == 0:
            print(json.dumps({"phase": "train", "step": step+1, "loss": float(loss.detach())}), flush=True)
    torch.save({"state_dict": model.state_dict(), "seed": seed, "steps": TRAIN_STEPS}, output / "model.pt")
    model.eval()
    predictions = []
    for record in manifest["predict"]["images"]:
        with Image.open(inside(root, record["path"])) as image:
            gray = np.asarray(image.convert("L"), dtype=np.float32) / 255.0
        instances = infer(model, gray, device)
        predictions.append({"image_id": record["image_id"], "instances": instances})
        print(json.dumps({"phase": "predict", "image_id": record["image_id"], "instances": len(instances)}), flush=True)
    result = {"schema_version": 1, "processed_images": [record["image_id"] for record in predictions],
              "predictions": predictions}
    temporary = output / "predictions.json.tmp"
    temporary.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
    temporary.replace(output / "predictions.json")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python main.py INPUT_MANIFEST OUTPUT_DIRECTORY")
    main(sys.argv[1], sys.argv[2])
