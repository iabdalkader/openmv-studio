#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Top-level training dispatcher. Reads project.json for the task type
# and forwards to the matching ml/<task>/train.py implementation.
#
# Usage:
#   python train.py --project DIR --models-dir DIR --epochs N --imgsz N --model NAME

import argparse
import os
import sys

# Ensure we can `import ml.*` regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ml import common


def main():
    ap = argparse.ArgumentParser(description="Train a model on a project dataset")
    ap.add_argument("--project", required=True, help="Project directory")
    ap.add_argument("--models-dir", required=True, help="Bundled models directory")
    ap.add_argument("--epochs", type=int, default=50, help="Training epochs")
    ap.add_argument("--imgsz", type=int, default=192, help="Image size")
    ap.add_argument("--model", required=True, help="Base model file name (e.g. yolov8n.pt)")
    ap.add_argument("--batch", type=int, default=16, help="Batch size")
    args = ap.parse_args()

    project = common.load_project(args.project)
    task = project["task"]

    if task == "bbox":
        from ml.bbox import train as task_train
        task_train.main(args, project)
    elif task == "centerpoint":
        from ml.centerpoint import train as task_train
        task_train.main(args, project)
    else:
        common.emit({"error": f"Unknown task: {task}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
