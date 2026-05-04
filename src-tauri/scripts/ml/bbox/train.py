# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# YOLOv8 / YOLOv11 training via Ultralytics. Materializes the
# Ultralytics-flavored dataset (dataset.yaml + dataset/ tree) and
# runs model.train(). Per-epoch metrics emitted as JSON via
# ml.common.emit so the Rust side parses TrainProgressEvent unchanged.

import os
import time

# Disable all ultralytics network paths (PyPI version check, GA
# telemetry, attempt_download_asset). Must be set BEFORE the
# ultralytics import below, since utils/__init__.py evaluates ONLINE =
# is_online() at module load and caches it.
os.environ["YOLO_OFFLINE"] = "true"

from ml import common


def _materialize_dataset(project_dir, project, train_stems, val_stems):
    """Write the Ultralytics dataset.yaml and copy the train/val image+
    label pairs into <project>/dataset/{train,val}/{images,labels}/.
    Returns the dataset.yaml path.
    """
    dataset_dir = os.path.join(project_dir, "dataset")
    for subset in ["train", "val"]:
        for subdir in ["images", "labels"]:
            os.makedirs(os.path.join(dataset_dir, subset, subdir), exist_ok=True)

    for stem in train_stems:
        common.copy_dataset_pair(stem, project_dir, dataset_dir, "train")
    for stem in val_stems:
        common.copy_dataset_pair(stem, project_dir, dataset_dir, "val")

    classes = project["classes"]
    yaml_path = os.path.join(project_dir, "dataset.yaml")
    with open(yaml_path, "w") as f:
        f.write(f"path: {dataset_dir}\n")
        f.write("train: train/images\n")
        f.write("val: val/images\n")
        f.write(f"nc: {len(classes)}\n")
        f.write(f"names: {classes}\n")
    return yaml_path


def main(args, project):
    import sys

    model_path = os.path.join(args.models_dir, args.model)
    if not os.path.exists(model_path):
        common.emit({"error": f"Model not found: {model_path}"})
        sys.exit(1)

    try:
        from ultralytics import YOLO
    except ImportError:
        common.emit({"error": "ultralytics not installed"})
        sys.exit(1)

    device = common.select_device()

    common.emit({"status": "preparing_dataset"})
    train_stems, val_stems, _, _, _ = common.dataset_summary(args.project)
    yaml_path = _materialize_dataset(args.project, project, train_stems, val_stems)

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
        common.emit({
            "epoch": epoch,
            "epochs": args.epochs,
            "metrics": [
                {"name": "box_loss",
                 "value": round(float(trainer.loss_items[0]), 4),
                 "range": None},
                {"name": "cls_loss",
                 "value": round(float(trainer.loss_items[1]), 4),
                 "range": None},
                {"name": "mAP50",
                 "value": round(float(metrics.get("metrics/mAP50(B)", 0)), 4),
                 "range": [0.0, 1.0]},
            ],
            "epoch_secs": round(epoch_secs, 2),
            "elapsed_secs": round(elapsed_secs, 2),
            "eta_secs": round(eta_secs, 2),
        })

    common.emit({"status": "training_started"})
    model = YOLO(model_path)
    model.add_callback("on_train_start", on_train_start)
    model.add_callback("on_train_epoch_end", on_train_epoch_end)

    model.train(
        data=yaml_path,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=0 if device == "cuda" else device,
        project=os.path.join(args.project, "runs"),
        name="train",
        exist_ok=True,
        verbose=False,
    )

    best_path = os.path.join(args.project, "runs", "train", "weights", "best.pt")
    common.emit({
        "status": "done",
        "best_weights": best_path,
        "exists": os.path.exists(best_path),
    })
