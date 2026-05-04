# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Auto-annotate images using a pretrained YOLOv8/v11 model and write
# 5-field bbox labels: <cls> <cx> <cy> <w> <h> per line.
#
# The model load, polling loop, and event emission live in
# common.run_annotator; this module owns the per-image annotate_image
# and the class-agnostic NMS that drops contradictory class-pair
# detections (cat+dog on the same animal etc).

import os

# Disable all ultralytics network paths (PyPI version check, GA
# telemetry, attempt_download_asset). Must be set BEFORE the
# ultralytics import below, since utils/__init__.py caches ONLINE at
# module load.
os.environ["YOLO_OFFLINE"] = "true"

from ml import common


# Drop overlapping detections that disagree on class. COCO YOLO often
# outputs both "cat" and "dog" for the same animal when it's uncertain;
# writing both produces contradictory training labels.
NMS_IOU = 0.5


def _iou_xywhn(a, b):
    ax1 = a[0] - a[2] / 2
    ay1 = a[1] - a[3] / 2
    ax2 = a[0] + a[2] / 2
    ay2 = a[1] + a[3] / 2
    bx1 = b[0] - b[2] / 2
    by1 = b[1] - b[3] / 2
    bx2 = b[0] + b[2] / 2
    by2 = b[1] + b[3] / 2
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _detect_and_nms(model, image_path, conf, class_map):
    """Run inference + class-agnostic NMS. Returns the kept candidate
    list as (conf, cls_id, cx, cy, w, h) tuples.
    """
    results = model(image_path, conf=conf, verbose=False)
    candidates = []
    for r in results:
        if r.boxes is None:
            continue
        for box in r.boxes:
            coco_id = int(box.cls[0])
            if class_map is not None:
                if coco_id not in class_map:
                    continue
                cls_id = class_map[coco_id]
            else:
                cls_id = coco_id
            x, y, w, h = box.xywhn[0].tolist()
            candidates.append((float(box.conf[0]), cls_id, x, y, w, h))

    candidates.sort(key=lambda c: c[0], reverse=True)
    kept = []
    for c in candidates:
        box_xywh = c[2:6]
        if any(_iou_xywhn(box_xywh, k[2:6]) > NMS_IOU for k in kept):
            continue
        kept.append(c)
    return kept


def annotate_image(model, image_path, output_dir, conf, class_map=None):
    kept = _detect_and_nms(model, image_path, conf, class_map)
    stem = os.path.splitext(os.path.basename(image_path))[0]
    label_path = os.path.join(output_dir, f"{stem}.txt")
    lines = [
        f"{cls_id} {x:.6f} {y:.6f} {w:.6f} {h:.6f}"
        for _, cls_id, x, y, w, h in kept
    ]
    with open(label_path, "w") as f:
        f.write("\n".join(lines))
        if lines:
            f.write("\n")
    return len(kept)


def main(args, project):
    project_classes = [
        c.strip() for c in args.classes.split(",") if c.strip()
    ] if args.classes else []
    common.run_annotator(args, project_classes, annotate_image)
