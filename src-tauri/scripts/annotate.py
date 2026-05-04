#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Top-level auto-annotator dispatcher. Reads project.json for the task
# type and forwards to the matching ml/<task>/annotate.py.
#
# Usage:
#   python annotate.py --input DIR --output DIR --models-dir DIR \
#                      --conf F --classes "a,b" [--watch]

import argparse
import os
import sys

# Ensure we can `import ml.*` regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ml import common


def main():
    ap = argparse.ArgumentParser(description="Auto-annotate images")
    ap.add_argument("--input", required=True, help="Input images directory")
    ap.add_argument("--output", required=True, help="Output labels directory")
    ap.add_argument("--models-dir", required=True, help="Bundled models directory")
    ap.add_argument("--model", default=None, help="Model file name in --models-dir")
    ap.add_argument("--conf", type=float, default=0.25, help="Confidence threshold")
    ap.add_argument("--watch", action="store_true", help="Watch for new images")
    ap.add_argument("--classes", default="",
                    help="Comma-separated project class names")
    ap.add_argument("--project", default=None,
                    help="Project directory (used to read task type)")
    args = ap.parse_args()

    # Auto-annotator can be invoked without a project (general usage),
    # in which case it defaults to bbox behavior.
    if args.project:
        project = common.load_project(args.project)
        task = project["task"]
    else:
        project = None
        task = "bbox"

    # Per-task default annotator model. bbox uses the detect variant;
    # centerpoint uses the seg variant so the centroid of the mask can
    # serve as a better target point than the bbox midpoint.
    DEFAULT_MODEL_BY_TASK = {
        "bbox": "yolo11m.pt",
        "centerpoint": "yolo11m-seg.pt",
    }
    if args.model is None:
        args.model = DEFAULT_MODEL_BY_TASK.get(task, "yolo11m.pt")

    common.emit({
        "status": "annotator_dispatch",
        "task": task,
        "model": args.model,
    })

    if task == "bbox":
        from ml.bbox import annotate as task_annotate
        task_annotate.main(args, project)
    elif task == "centerpoint":
        from ml.centerpoint import annotate as task_annotate
        task_annotate.main(args, project)
    else:
        common.emit({"error": f"Unknown task: {task}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
