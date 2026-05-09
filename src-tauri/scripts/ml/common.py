# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Shared scaffolding used by every task pipeline (bbox, centerpoint,
# future heatmap tasks). Intentionally lightweight: project IO, dataset
# split, calibration data builder, QDQ quantizer, IPC event helper.

import json
import os
import random
import shutil
import sys


def emit(event):
    """Emit a JSON event line on stdout. Single funnel for the IPC
    protocol so the Rust side never sees half-formed lines.
    """
    print(json.dumps(event), flush=True)


def load_project(project_dir):
    """Read project.json. Required field 'task' must be present; raise
    KeyError if missing (existing on-disk projects without 'task' need
    to be migrated by the user). Returns the raw dict.
    """
    with open(os.path.join(project_dir, "project.json")) as f:
        config = json.load(f)
    if "task" not in config:
        raise KeyError(
            "project.json missing required 'task' field; add "
            "\"task\": \"bbox\" (or other) and retry"
        )
    return config


def read_status_map(project_dir):
    """Read status.json mapping image stem -> review status string.
    Missing or malformed file returns {}.
    """
    status_path = os.path.join(project_dir, "status.json")
    if not os.path.exists(status_path):
        return {}
    with open(status_path) as f:
        try:
            return json.load(f) or {}
        except json.JSONDecodeError:
            return {}


def filter_dataset(project_dir):
    """Walk images/ and bucket each .jpg by review status.

    Returns (paired, backgrounds, skip_counts) where:
      paired       -- list of stems with status=accepted AND non-empty label
      backgrounds  -- list of stems with status=background; empty .txt
                      created if missing
      skip_counts  -- dict with keys unreviewed/rejected/no_labels

    Pure: no side effects beyond touching empty .txt for background images.
    """
    images_dir = os.path.join(project_dir, "images")
    labels_dir = os.path.join(project_dir, "labels")
    status_map = read_status_map(project_dir)

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

    return paired, backgrounds, {
        "unreviewed": skipped_unreviewed,
        "rejected": skipped_rejected,
        "no_labels": skipped_no_labels,
    }


def split_80_20(stems, rng=None):
    """Shuffle stems and split 80/20. If too small, val gets the last
    item duplicated so val is never empty.
    """
    if rng is None:
        rng = random
    rng.shuffle(stems)
    cut = max(1, int(len(stems) * 0.8))
    train = stems[:cut]
    val = stems[cut:] if cut < len(stems) else stems[-1:]
    return train, val


def copy_dataset_pair(stem, project_dir, dataset_dir, subset):
    """Copy <stem>.jpg and <stem>.txt from project_dir/{images,labels}
    into dataset_dir/{subset}/{images,labels}/. Caller is responsible
    for creating the dataset_dir tree first.
    """
    shutil.copy2(
        os.path.join(project_dir, "images", f"{stem}.jpg"),
        os.path.join(dataset_dir, subset, "images", f"{stem}.jpg"),
    )
    shutil.copy2(
        os.path.join(project_dir, "labels", f"{stem}.txt"),
        os.path.join(dataset_dir, subset, "labels", f"{stem}.txt"),
    )


def select_device():
    """Pick the fastest available PyTorch device and emit the
    device_selected event. Returns the device handle.
    """
    import torch
    if torch.cuda.is_available():
        device = "cuda"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        device = "mps"
    else:
        device = "cpu"
    emit({"status": "device_selected", "device": str(device)})
    return device


def dataset_summary(project_dir):
    """Filter the project's dataset, refuse-on-empty, warn on
    background ratio > 30%, split positives + backgrounds 80/20, and
    emit dataset_ready.

    Returns (train_stems, val_stems, n_accepted, n_background, skips).
    train_stems / val_stems are lists of stems mixing positives and
    backgrounds. Caller is responsible for materializing the dataset
    on disk (task-specific format).
    """
    paired, backgrounds, skips = filter_dataset(project_dir)

    if not paired:
        if backgrounds:
            msg = ("Dataset has only background images. Need at least "
                   "one accepted image with labels to train.")
        else:
            msg = (
                "No accepted images with labels found. "
                f"Skipped: {skips['unreviewed']} unreviewed, "
                f"{skips['rejected']} rejected, "
                f"{skips['no_labels']} accepted-but-empty."
            )
        emit({"error": msg})
        sys.exit(1)

    total_for_ratio = len(paired) + len(backgrounds)
    if backgrounds and len(backgrounds) > 0.30 * total_for_ratio:
        pct = round(100 * len(backgrounds) / total_for_ratio)
        emit({"warning": (
            f"Backgrounds are {pct}% of the dataset; recommended 0-10%. "
            "May reduce recall."
        )})

    train_pos, val_pos = split_80_20(paired)
    bg_train, bg_val = ([], [])
    if backgrounds:
        bg_train, bg_val = split_80_20(backgrounds)
    train_stems = train_pos + bg_train
    val_stems = val_pos + bg_val

    emit({
        "status": "dataset_ready",
        "train_images": len(train_stems),
        "val_images": len(val_stems),
        "included_accepted": len(paired),
        "included_background": len(backgrounds),
        "skipped_unreviewed": skips["unreviewed"],
        "skipped_rejected": skips["rejected"],
    })
    return train_stems, val_stems, len(paired), len(backgrounds), skips


def finalize_export(project, project_dir, src, target, imgsz,
                    models_dir, stedgeai_dir, default_model_tag=""):
    """Take a quantized artifact (int8 TFLite or QDQ ONNX) and produce
    the final deployable file under <project>/export/. Handles:
      - descriptive dest filename based on project name + model + imgsz
      - cleanup of stale .tflite siblings (Rust deploys whatever .tflite
        is present)
      - NPU compile via targets.compile_for_target (or copy for cpu)
      - .target marker for Deploy to detect stale-target artifacts
      - labels.txt write
      - emits "compiling_for_<target>" / "compile_done" / "done"

    default_model_tag is used in the filename when project["model"] is
    missing -- bbox always sets model, centerpoint may default to
    "_centernet".
    """
    import re
    import shutil
    from ml.targets import compile_for_target

    project_name = os.path.basename(os.path.normpath(project_dir))
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", project_name)
    if project.get("model"):
        model_tag = f"_{project['model']}"
    else:
        model_tag = default_model_tag
    save_name = f"{sanitized}{model_tag}_{imgsz}.tflite"

    export_dir = os.path.join(project_dir, "export")
    os.makedirs(export_dir, exist_ok=True)
    # Each export overwrites; clear stale .tflite siblings so the dir
    # holds exactly one artifact (Rust deploys whatever .tflite is
    # present).
    for f in os.listdir(export_dir):
        if f.endswith(".tflite"):
            try:
                os.remove(os.path.join(export_dir, f))
            except OSError:
                pass
    dest = os.path.join(export_dir, save_name)

    if target == "cpu":
        shutil.copy2(src, dest)
    elif target == "st-neural-art":
        emit({"status": f"compiling_for_{target}"})
        compile_for_target(src, dest, target, models_dir, stedgeai_dir)
        emit({"status": "compile_done"})
    else:
        # Vela compiles in-place: copy first, compile dest -> dest.
        shutil.copy2(src, dest)
        emit({"status": f"compiling_for_{target}"})
        compile_for_target(dest, dest, target, models_dir, stedgeai_dir)
        emit({"status": "compile_done"})

    target_marker = os.path.join(export_dir, ".target")
    with open(target_marker, "w") as f:
        f.write(target)

    labels_path = os.path.join(export_dir, "labels.txt")
    with open(labels_path, "w") as f:
        for cls in project["classes"]:
            f.write(cls + "\n")

    emit({
        "status": "done",
        "tflite_path": dest,
        "labels_path": labels_path,
        "file_size": os.path.getsize(dest),
        "target": target,
    })


def run_annotator(args, project_classes, annotate_fn):
    """Standard auto-annotator main loop. Each task supplies its own
    annotate_fn(model, image_path, output_dir, conf, class_map) -> int
    (number of detections written). The loop handles model load, class
    map setup, started/idle/done events, and the optional --watch
    polling cycle.

    annotate_fn is also responsible for writing the per-task label file
    format (5-field for bbox, 3-field for centerpoint).
    """
    import time

    model_path = os.path.join(args.models_dir, args.model)
    if not os.path.exists(model_path):
        emit({"error": f"Model not found: {model_path}"})
        sys.exit(1)

    os.makedirs(args.output, exist_ok=True)

    try:
        from ultralytics import YOLO
    except ImportError:
        emit({"error": "ultralytics not installed"})
        sys.exit(1)

    emit({"status": "loading_model", "model": args.model})
    model = YOLO(model_path)
    emit({"status": "model_ready"})

    class_map = _build_class_map(project_classes, model.names)
    if project_classes:
        mapped = {model.names.get(k, k): v for k, v in class_map.items()}
        emit({"status": "class_map", "mapping": mapped})

    processed = set()
    if os.path.isdir(args.output):
        for f in os.listdir(args.output):
            if f.endswith(".txt"):
                processed.add(f.replace(".txt", ".jpg"))

    total_images = 0
    if os.path.isdir(args.input):
        total_images = sum(
            1 for f in os.listdir(args.input) if f.endswith(".jpg")
        )
    emit({
        "status": "started",
        "total": total_images,
        "already_processed": len(processed),
    })

    def process_new_images():
        if not os.path.isdir(args.input):
            return 0
        images = sorted(
            f for f in os.listdir(args.input)
            if f.endswith(".jpg") and f not in processed
        )
        n_done = 0
        for img_name in images:
            img_path = os.path.join(args.input, img_name)
            try:
                size = os.path.getsize(img_path)
                if size == 0:
                    continue
            except OSError:
                continue
            detections = annotate_fn(
                model, img_path, args.output, args.conf, class_map
            )
            processed.add(img_name)
            n_done += 1
            emit({
                "image": img_name,
                "detections": detections,
                "total_processed": len(processed),
            })
        return n_done

    if args.watch:
        was_busy = False
        while True:
            done_this_pass = process_new_images()
            if done_this_pass > 0:
                was_busy = True
            elif was_busy:
                emit({
                    "status": "idle",
                    "total_processed": len(processed),
                })
                was_busy = False
            time.sleep(0.5)
    else:
        process_new_images()
        emit({"status": "done", "total": len(processed)})


def _build_class_map(project_classes, coco_names):
    """COCO class index -> project class index, case-insensitive name match."""
    if not project_classes:
        return None
    cmap = {}
    for proj_idx, pname in enumerate(project_classes):
        pname_lower = pname.strip().lower()
        for coco_idx, cname in coco_names.items():
            if cname.lower() == pname_lower:
                cmap[coco_idx] = proj_idx
                break
    return cmap


def quantize_onnx_qdq(onnx_in, calib_npy, work_dir):
    """Static PTQ to produce a QDQ ONNX from a float ONNX + BHWC float32
    [0..255] calibration data. Output goes to work_dir/best_qdq.onnx.

    Hand the resulting ONNX to stedgeai for the st-neural-art target;
    avoids the onnx2tf -> TFLite hop and its NCHW->NHWC graph rewrite.
    Calibration NPY is shared with the TFLite path.
    """
    import numpy as np
    from onnxruntime.quantization import (
        CalibrationDataReader,
        CalibrationMethod,
        QuantFormat,
        QuantType,
        quantize_static,
    )
    from onnxruntime.quantization.shape_inference import quant_pre_process

    # Run the recommended graph-cleanup pass before static PTQ:
    # symbolic shape inference, constant folding, op fusion. Silences
    # onnxruntime's "Please consider to run pre-processing before
    # quantization" warning and tightens the quantized model.
    preprocessed = os.path.join(work_dir, "best_pre.onnx")
    quant_pre_process(onnx_in, preprocessed)
    onnx_in = preprocessed

    # calib_npy is BHWC float32 in [0, 255] (matches what onnx2tf consumes
    # via the [[[[0,0,0]]]] / [[[[255,255,255]]]] range hints). The
    # exported ONNX expects NCHW float32 in [0, 1] (Ultralytics default),
    # so per-sample we transpose and scale.
    arr = np.load(calib_npy).astype(np.float32) / 255.0
    arr = np.transpose(arr, (0, 3, 1, 2))

    # Read the actual input tensor name from the ONNX rather than
    # hardcoding "images" -- protects us if the model changes.
    import onnx as _onnx
    model = _onnx.load(onnx_in)
    input_name = model.graph.input[0].name

    class _NpyReader(CalibrationDataReader):
        def __init__(self, samples, name):
            self._iter = iter(samples)
            self._name = name

        def get_next(self):
            try:
                x = next(self._iter)
            except StopIteration:
                return None
            return {self._name: np.expand_dims(x, 0)}

    # stedgeai's Neural-ART backend rejects QUInt8 activations in the
    # QDQ graph; signed int8 for both activations and weights.
    qdq_onnx = os.path.join(work_dir, "best_qdq.onnx")
    quantize_static(
        model_input=onnx_in,
        model_output=qdq_onnx,
        calibration_data_reader=_NpyReader(arr, input_name),
        quant_format=QuantFormat.QDQ,
        per_channel=True,
        activation_type=QuantType.QInt8,
        weight_type=QuantType.QInt8,
        # Percentile clips activation outliers so int8 levels track real
        # signal; preserves cls-score precision on small datasets where
        # MinMax wastes range and Entropy histograms are too sparse.
        calibrate_method=CalibrationMethod.Percentile,
        extra_options={
            "CalibPercentile": 99.999,
        },
    )
    return qdq_onnx
