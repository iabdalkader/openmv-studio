# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Export a trained YOLOv8/v11 model to a deployable artifact.
#
# Pipeline (task-specific bits only; common.finalize_export takes over
# after we produce the quantized artifact):
#   1. Load best.pt and apply Ultralytics' tf_wrapper Detect-head patch
#      (normalizes the bbox decode by stride/grid so cls and bbox share
#      a [0, 1] range before the final Concat -- without this, int8
#      quant collapses class scores).
#   2. Export the patched model to ONNX.
#   3. Build calibration data via YOLODataset's letterbox path
#      (stratified per class).
#   4. Branch on target:
#      - st-neural-art: PTQ to QDQ ONNX.
#      - cpu / ethos-u55-*: onnx2tf -> int8 TFLite.
#   5. Hand off to common.finalize_export for NPU compile + dest write.

import os
import random
import sys

import numpy as np

from ml import common
from ml.targets import onnx2tf_path


VALID_TARGETS = ("cpu", "ethos-u55-128", "ethos-u55-256", "st-neural-art")


def _build_calibration(project_dir, data_yaml, imgsz, weights_dir):
    """BHWC float32 [0,255] calibration NPY using YOLODataset's
    letterbox preprocessing. Stratified per class -- the dataset on
    disk is grouped by class and unstratified sampling drops the
    minority class entirely on the NPU.
    """
    import torch
    from ultralytics.data import YOLODataset
    from ultralytics.data.utils import check_det_dataset

    data = check_det_dataset(data_yaml)
    cal_split = "val" if "val" in data else "train"
    dataset = YOLODataset(
        data[cal_split],
        data=data,
        task="detect",
        imgsz=imgsz,
        augment=False,
        batch_size=16,
    )
    n = len(dataset)
    if n < 1:
        common.emit({"error": "No calibration images in dataset"})
        sys.exit(1)

    rng = random.Random(0)
    class_to_imgs = {}
    for i, lab in enumerate(dataset.labels):
        cls_arr = lab.get("cls")
        if cls_arr is None:
            continue
        for c in set(int(v) for v in np.asarray(cls_arr).flatten().tolist()):
            class_to_imgs.setdefault(c, []).append(i)

    if class_to_imgs:
        target_count = min(len(v) for v in class_to_imgs.values())
        selected = set()
        for imgs in class_to_imgs.values():
            rng.shuffle(imgs)
            taken = 0
            for idx in imgs:
                if idx in selected:
                    continue
                selected.add(idx)
                taken += 1
                if taken >= target_count:
                    break
        indices = list(selected)
    else:
        indices = list(range(n))
    rng.shuffle(indices)

    samples = [torch.as_tensor(dataset[i]["img"]) for i in indices]
    calib_data = (
        torch.nn.functional.interpolate(torch.stack(samples).float(), size=imgsz)
        .permute(0, 2, 3, 1)
        .numpy()
        .astype(np.float32)
    )
    calib_npy = os.path.join(weights_dir, "calib_data.npy")
    np.save(calib_npy, calib_data)
    return calib_npy, calib_data.shape[0]


def main(args, project):
    if args.target not in VALID_TARGETS:
        common.emit({"error": f"Invalid target: {args.target}"})
        sys.exit(1)

    best_pt = os.path.join(args.project, "runs", "train", "weights", "best.pt")
    if not os.path.exists(best_pt):
        common.emit({"error": "No trained model found (best.pt)"})
        sys.exit(1)

    data_yaml = os.path.join(args.project, "dataset.yaml")
    if not os.path.exists(data_yaml):
        common.emit({"error": "No dataset.yaml found - run training first"})
        sys.exit(1)

    weights_dir = os.path.dirname(best_pt)
    onnx_file = os.path.join(weights_dir, "best.onnx")

    # Step 1: Load model, apply tf_wrapper Detect-head patch, export ONNX.
    common.emit({"status": "exporting_onnx"})
    from ultralytics import YOLO
    from ultralytics.utils.export.tensorflow import tf_wrapper

    model = YOLO(best_pt)
    tf_wrapper(model.model)
    if os.path.exists(onnx_file):
        os.remove(onnx_file)
    model.export(format="onnx", imgsz=args.imgsz, simplify=True)
    if not os.path.exists(onnx_file):
        common.emit({"error": "ONNX export failed"})
        sys.exit(1)

    # Step 2: build calibration data.
    common.emit({"status": "building_calibration"})
    calib_npy, n_calib = _build_calibration(
        args.project, data_yaml, args.imgsz, weights_dir
    )

    # Step 3: produce a quantized model. For st-neural-art we do PTQ
    # in onnxruntime (QDQ ONNX) and feed that straight to stedgeai;
    # every other target goes through onnx2tf to produce an int8 TFLite
    # (Vela needs TFLite, cpu deploys the TFLite as-is).
    if args.target == "st-neural-art":
        common.emit({"status": "quantizing_onnx"})
        src = common.quantize_onnx_qdq(onnx_file, calib_npy, weights_dir)
        common.emit({"status": "quantize_done"})
    else:
        common.emit({
            "status": "converting_tflite",
            "calibration_images": int(n_calib),
        })
        saved_model_dir = os.path.join(weights_dir, "best_saved_model")
        src = onnx2tf_path.convert(onnx_file, calib_npy, saved_model_dir)
        common.emit({"status": "tflite_done"})

    # Step 4: NPU compile + dest write (target marker, labels.txt,
    # final 'done' event). Shared across tasks.
    common.finalize_export(
        project=project,
        project_dir=args.project,
        src=src,
        target=args.target,
        imgsz=args.imgsz,
        models_dir=args.models_dir,
        stedgeai_dir=args.stedgeai_dir,
    )
