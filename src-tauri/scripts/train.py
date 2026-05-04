#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Train a YOLOv8n model on a labeled dataset using transfer learning.
# Reads project.json for class names, generates dataset.yaml with
# an 80/20 train/val split, and outputs JSON progress lines to stdout.
#
# Usage:
#   python train.py --project DIR --epochs 50 --imgsz 192

import argparse
import json
import os
import random
import shutil
import sys
import time

# Disable all ultralytics network paths (PyPI version check, GA telemetry,
# attempt_download_asset for missing weights). Must be set BEFORE the
# ultralytics import below, since utils/__init__.py evaluates ONLINE = is_online()
# at module load and caches the result on a module-level Events() instance.
os.environ["YOLO_OFFLINE"] = "true"


def generate_dataset(project_dir, imgsz):
    """Create dataset.yaml and split images into train/val sets.

    Only images marked 'accepted' in status.json are used for training.
    Rejected and pending images are excluded.
    """
    config_path = os.path.join(project_dir, "project.json")
    with open(config_path) as f:
        config = json.load(f)

    classes = config["classes"]
    images_dir = os.path.join(project_dir, "images")
    labels_dir = os.path.join(project_dir, "labels")

    status_path = os.path.join(project_dir, "status.json")
    status_map = {}
    if os.path.exists(status_path):
        with open(status_path) as f:
            try:
                status_map = json.load(f) or {}
            except json.JSONDecodeError:
                status_map = {}

    # Collect accepted images (with labels) and background images (empty
    # labels mark "no objects of any class"; Ultralytics treats them as
    # negative examples to reduce false positives).
    paired = []
    backgrounds = []
    skipped_unreviewed = 0
    skipped_rejected = 0
    skipped_no_labels = 0
    for img in sorted(os.listdir(images_dir)):
        if not img.endswith(".jpg"):
            continue
        stem = img.replace(".jpg", "")
        status = status_map.get(stem, "pending")
        label = os.path.join(labels_dir, f"{stem}.txt")
        if status == "rejected":
            skipped_rejected += 1
            continue
        if status == "background":
            if not os.path.exists(label):
                open(label, "a").close()
            backgrounds.append(stem)
            continue
        if status != "accepted":
            skipped_unreviewed += 1
            continue
        if not os.path.exists(label) or os.path.getsize(label) == 0:
            skipped_no_labels += 1
            continue
        paired.append(stem)

    if not paired:
        if backgrounds:
            msg = (
                "Dataset has only background images. Need at least one "
                "accepted image with labels to train a detector."
            )
        else:
            msg = (
                "No accepted images with labels found. "
                f"Skipped: {skipped_unreviewed} unreviewed, "
                f"{skipped_rejected} rejected, "
                f"{skipped_no_labels} accepted-but-empty."
            )
        print(json.dumps({"error": msg}), flush=True)
        sys.exit(1)

    total_for_ratio = len(paired) + len(backgrounds)
    if backgrounds and len(backgrounds) > 0.30 * total_for_ratio:
        pct = round(100 * len(backgrounds) / total_for_ratio)
        print(json.dumps({
            "warning": (
                f"Backgrounds are {pct}% of the dataset; Ultralytics "
                "recommends 0-10%. May reduce recall."
            ),
        }), flush=True)

    # Shuffle and split 80/20 for both positive and background sets.
    def split_80_20(stems):
        random.shuffle(stems)
        cut = max(1, int(len(stems) * 0.8))
        train = stems[:cut]
        val = stems[cut:] if cut < len(stems) else stems[-1:]
        return train, val

    train_set, val_set = split_80_20(paired)
    bg_train, bg_val = ([], [])
    if backgrounds:
        bg_train, bg_val = split_80_20(backgrounds)

    # Create dataset directories
    dataset_dir = os.path.join(project_dir, "dataset")
    for subset in ["train", "val"]:
        for subdir in ["images", "labels"]:
            os.makedirs(os.path.join(dataset_dir, subset, subdir), exist_ok=True)

    def copy_pair(stem, subset):
        shutil.copy2(
            os.path.join(images_dir, f"{stem}.jpg"),
            os.path.join(dataset_dir, subset, "images", f"{stem}.jpg"),
        )
        shutil.copy2(
            os.path.join(labels_dir, f"{stem}.txt"),
            os.path.join(dataset_dir, subset, "labels", f"{stem}.txt"),
        )

    for stem in train_set + bg_train:
        copy_pair(stem, "train")
    for stem in val_set + bg_val:
        copy_pair(stem, "val")

    # Write dataset.yaml
    yaml_path = os.path.join(project_dir, "dataset.yaml")
    with open(yaml_path, "w") as f:
        f.write(f"path: {dataset_dir}\n")
        f.write("train: train/images\n")
        f.write("val: val/images\n")
        f.write(f"nc: {len(classes)}\n")
        f.write(f"names: {classes}\n")

    return (
        yaml_path,
        len(train_set) + len(bg_train),
        len(val_set) + len(bg_val),
        skipped_unreviewed,
        skipped_rejected,
        len(paired),
        len(backgrounds),
    )


def main():
    ap = argparse.ArgumentParser(description="Train YOLOv8n on project dataset")
    ap.add_argument("--project", required=True, help="Project directory")
    ap.add_argument("--models-dir", required=True, help="Bundled models directory")
    ap.add_argument("--epochs", type=int, default=50, help="Training epochs")
    ap.add_argument("--imgsz", type=int, default=192, help="Image size")
    ap.add_argument("--model", required=True, help="Base model file name (e.g. yolov8n.pt)")
    ap.add_argument("--batch", type=int, default=16, help="Batch size")
    args = ap.parse_args()

    model_path = os.path.join(args.models_dir, args.model)
    if not os.path.exists(model_path):
        print(json.dumps({"error": f"Model not found: {model_path}"}), flush=True)
        sys.exit(1)

    try:
        from ultralytics import YOLO
    except ImportError:
        print(json.dumps({"error": "ultralytics not installed"}), flush=True)
        sys.exit(1)

    # Pick the fastest device available.
    import torch
    if torch.cuda.is_available():
        device = 0
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        device = "mps"
    else:
        device = "cpu"

    print(json.dumps({"status": "device_selected", "device": str(device)}), flush=True)

    # Generate dataset split
    print(json.dumps({"status": "preparing_dataset"}), flush=True)
    (yaml_path, n_train, n_val,
     n_skip_unreviewed, n_skip_rejected,
     n_accepted, n_background) = generate_dataset(args.project, args.imgsz)
    print(json.dumps({
        "status": "dataset_ready",
        "train_images": n_train,
        "val_images": n_val,
        "included_accepted": n_accepted,
        "included_background": n_background,
        "skipped_unreviewed": n_skip_unreviewed,
        "skipped_rejected": n_skip_rejected,
    }), flush=True)

    # Custom callback to output JSON progress
    timing = {"run_start": None, "epoch_start": None}

    def on_train_start(_trainer):
        now = time.monotonic()
        timing["run_start"] = now
        timing["epoch_start"] = now

    def on_train_epoch_end(trainer):
        metrics = trainer.metrics
        epoch = trainer.epoch + 1
        now = time.monotonic()
        epoch_secs = now - (timing["epoch_start"] or now)
        elapsed_secs = now - (timing["run_start"] or now)
        timing["epoch_start"] = now
        eta_secs = epoch_secs * max(0, args.epochs - epoch)
        result = {
            "epoch": epoch,
            "epochs": args.epochs,
            "box_loss": round(float(trainer.loss_items[0]), 4),
            "cls_loss": round(float(trainer.loss_items[1]), 4),
            "mAP50": round(float(metrics.get("metrics/mAP50(B)", 0)), 4),
            "epoch_secs": round(epoch_secs, 2),
            "elapsed_secs": round(elapsed_secs, 2),
            "eta_secs": round(eta_secs, 2),
        }
        print(json.dumps(result), flush=True)

    # Train
    print(json.dumps({"status": "training_started"}), flush=True)
    model = YOLO(model_path)
    model.add_callback("on_train_start", on_train_start)
    model.add_callback("on_train_epoch_end", on_train_epoch_end)

    results = model.train(
        data=yaml_path,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=device,
        project=os.path.join(args.project, "runs"),
        name="train",
        exist_ok=True,
        verbose=False,
    )

    # Report completion
    best_path = os.path.join(args.project, "runs", "train", "weights", "best.pt")
    print(json.dumps({
        "status": "done",
        "best_weights": best_path,
        "exists": os.path.exists(best_path),
    }), flush=True)


if __name__ == "__main__":
    main()
