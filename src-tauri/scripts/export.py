#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Top-level export dispatcher. Reads project.json for the task type and
# forwards to the matching ml/<task>/export.py implementation.
#
# TF must be imported BEFORE onnx/onnx2tf to avoid an abseil-version
# clash that deadlocks the int8 calibration loop. We import it here at
# the top so any task-specific export module that uses onnx2tf gets the
# right load order regardless of import path.
#
# Usage:
#   python export.py --project DIR --imgsz N --target {cpu|ethos-u55-*|st-neural-art}

import os
import sys

# Disable all ultralytics network paths (PyPI version check, GA
# telemetry, attempt_download_asset). Must be set BEFORE the ultralytics
# import below, since utils/__init__.py caches ONLINE at module load.
os.environ["YOLO_OFFLINE"] = "true"

import tensorflow as tf  # noqa: F401  MUST be first - see header

import argparse
import traceback

# Ensure we can `import ml.*` regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ml import common


def main():
    ap = argparse.ArgumentParser(description="Export a trained model to a deployable artifact")
    ap.add_argument("--project", required=True, help="Project directory")
    ap.add_argument("--imgsz", type=int, default=192, help="Image size")
    ap.add_argument("--target", default="cpu",
                    help="Deployment target (cpu, ethos-u55-128, ethos-u55-256, st-neural-art)")
    ap.add_argument("--models-dir", default=None,
                    help="Directory holding vela.ini and neuralart.json (non-CPU targets)")
    ap.add_argument("--stedgeai-dir", default=None,
                    help="Root of the stedgeai distribution (st-neural-art target)")
    args = ap.parse_args()

    project = common.load_project(args.project)
    task = project["task"]

    if task == "bbox":
        from ml.bbox import export as task_export
        task_export.main(args, project)
    elif task == "centerpoint":
        from ml.centerpoint import export as task_export
        task_export.main(args, project)
    else:
        common.emit({"error": f"Unknown task: {task}"})
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        os._exit(e.code if isinstance(e.code, int) else 1)
    except BaseException:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)
