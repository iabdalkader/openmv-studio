# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Export a trained centerpoint model to a deployable artifact.
#
# Pipeline (task-specific bits only; common.finalize_export takes over
# after we produce the quantized artifact):
#   1. Rebuild model architecture, load saved state dict, export to
#      ONNX with static input shape and named input "images".
#   2. Build calibration data: random sample of labeled images
#      (stratified per class), resize, stack as BHWC float32 [0..255].
#   3. Branch on target same as bbox:
#      - st-neural-art: PTQ to QDQ ONNX.
#      - cpu / ethos-u55-*: onnx2tf -> int8 TFLite.
#   4. Hand off to common.finalize_export for NPU compile + dest write.

import os
import random
import sys

import numpy as np

from ml import common
from ml.targets import onnx2tf_path


VALID_TARGETS = ("cpu", "ethos-u55-128", "ethos-u55-256", "st-neural-art")
INPUT_OP_NAME = "images"


def _build_calibration(project_dir, imgsz, weights_dir, n_samples=200):
    """Sample labeled images, resize to imgsz, stack as BHWC float32
    [0..255]. Stratified per class so int8 ranges don't collapse the
    minority class.
    """
    from PIL import Image

    images_dir = os.path.join(project_dir, "images")
    labels_dir = os.path.join(project_dir, "labels")

    items = []
    for f in sorted(os.listdir(images_dir)):
        if not f.endswith(".jpg"):
            continue
        stem = f[:-4]
        label = os.path.join(labels_dir, f"{stem}.txt")
        if not os.path.exists(label) or os.path.getsize(label) == 0:
            continue
        classes = set()
        with open(label) as fp:
            for line in fp:
                parts = line.strip().split()
                if parts:
                    try:
                        classes.add(int(parts[0]))
                    except ValueError:
                        pass
        if classes:
            items.append((stem, classes))

    if not items:
        common.emit({"error": "No labeled images for calibration"})
        sys.exit(1)

    rng = random.Random(0)
    class_to_imgs = {}
    for stem, classes in items:
        for c in classes:
            class_to_imgs.setdefault(c, []).append(stem)

    selected = []
    seen = set()
    if class_to_imgs:
        per_class = max(1, n_samples // len(class_to_imgs))
        for cls_imgs in class_to_imgs.values():
            rng.shuffle(cls_imgs)
            taken = 0
            for stem in cls_imgs:
                if stem in seen:
                    continue
                seen.add(stem)
                selected.append(stem)
                taken += 1
                if taken >= per_class:
                    break
    if not selected:
        selected = [s for s, _ in items[:n_samples]]
    rng.shuffle(selected)

    arrs = []
    for stem in selected[:n_samples]:
        path = os.path.join(images_dir, f"{stem}.jpg")
        im = Image.open(path).convert("RGB").resize(
            (imgsz, imgsz), Image.BILINEAR
        )
        arrs.append(np.asarray(im, dtype=np.float32))

    calib_data = np.stack(arrs)
    calib_npy = os.path.join(weights_dir, "calib_data.npy")
    np.save(calib_npy, calib_data)
    return calib_npy, calib_data.shape[0]


def _export_onnx(best_pt, onnx_out, imgsz):
    import torch
    from torch import nn
    from ml.networks.heatmap import build_heatmap_model

    ckpt = torch.load(best_pt, map_location="cpu", weights_only=False)
    num_classes = ckpt["num_classes"]
    model = build_heatmap_model(num_classes=num_classes, pretrained=False)
    model.load_state_dict(ckpt["state_dict"])
    model.eval()

    # Wrap so the ONNX output is NHWC; the firmware ML library reads
    # `model.output_shape` as (B, H, W, C).
    class _NHWCWrapper(nn.Module):
        def __init__(self, base):
            super().__init__()
            self.base = base

        def forward(self, x):
            return self.base(x).permute(0, 2, 3, 1).contiguous()

    export_model = _NHWCWrapper(model).eval()

    dummy = torch.randn(1, 3, imgsz, imgsz)
    if os.path.exists(onnx_out):
        os.remove(onnx_out)
    torch.onnx.export(
        export_model,
        dummy,
        onnx_out,
        input_names=[INPUT_OP_NAME],
        output_names=["heatmap"],
        opset_version=18,
        do_constant_folding=True,
    )
    if not os.path.exists(onnx_out):
        common.emit({"error": "ONNX export failed"})
        sys.exit(1)


def main(args, project):
    if args.target not in VALID_TARGETS:
        common.emit({"error": f"Invalid target: {args.target}"})
        sys.exit(1)

    best_pt = os.path.join(args.project, "runs", "train", "weights", "best.pt")
    if not os.path.exists(best_pt):
        common.emit({"error": "No trained model found (best.pt)"})
        sys.exit(1)

    weights_dir = os.path.dirname(best_pt)
    onnx_file = os.path.join(weights_dir, "best.onnx")

    # Step 1: rebuild the model architecture, load the trained state
    # dict, export to ONNX. Static input shape for max NPU compiler
    # compatibility; do_constant_folding lets onnx2tf and the QDQ
    # quantizer see fewer dynamic shapes.
    common.emit({"status": "exporting_onnx"})
    _export_onnx(best_pt, onnx_file, args.imgsz)

    # Step 2: build calibration data (stratified per class).
    common.emit({"status": "building_calibration"})
    calib_npy, n_calib = _build_calibration(
        args.project, args.imgsz, weights_dir
    )

    # Step 3: produce a quantized model. Same target matrix as bbox.
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
        src = onnx2tf_path.convert(
            onnx_file, calib_npy, saved_model_dir,
            input_op_name=INPUT_OP_NAME,
        )
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
        default_model_tag="_centernet",
    )
