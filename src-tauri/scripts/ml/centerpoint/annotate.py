# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Auto-annotate images for the centerpoint task. Uses a YOLO-seg
# variant so we can derive the centerpoint from the segmentation
# mask centroid instead of the bbox midpoint -- mass-weighted center
# of the silhouette stays on the actual object for elongated/curled
# subjects (sleeping cats, dogs being held, etc.) where the bbox
# midpoint drifts off into hands / background.

import os

os.environ["YOLO_OFFLINE"] = "true"

from ml import common
from ml.bbox.annotate import NMS_IOU, _iou_xywhn


def _mask_centroid_or_bbox(mask, fallback_cx, fallback_cy):
    """Centroid of `mask > 0.5` in normalized [0,1] coords. Falls back
    to the supplied bbox center if the mask is empty.
    """
    ys, xs = (mask > 0.5).nonzero(as_tuple=True)
    if xs.numel() == 0:
        return fallback_cx, fallback_cy
    mh, mw = mask.shape
    cx = float(xs.float().mean()) / mw
    cy = float(ys.float().mean()) / mh
    return cx, cy


def _detect_seg_and_nms(model, image_path, conf, class_map):
    """Run inference (with masks), class-agnostic bbox NMS, then
    compute centerpoint per kept detection. Returns
    (conf, cls_id, point_cx, point_cy) tuples.
    """
    results = model(image_path, conf=conf, verbose=False)
    candidates = []
    for r in results:
        if r.boxes is None:
            continue
        masks = r.masks.data if r.masks is not None else None
        for i, box in enumerate(r.boxes):
            coco_id = int(box.cls[0])
            if class_map is not None:
                if coco_id not in class_map:
                    continue
                cls_id = class_map[coco_id]
            else:
                cls_id = coco_id
            bx, by, bw, bh = box.xywhn[0].tolist()
            mask = masks[i] if (masks is not None and i < masks.shape[0]) else None
            candidates.append((float(box.conf[0]), cls_id, bx, by, bw, bh, mask))

    candidates.sort(key=lambda c: c[0], reverse=True)
    kept = []
    for c in candidates:
        bx, by, bw, bh = c[2], c[3], c[4], c[5]
        if any(_iou_xywhn((bx, by, bw, bh), (k[2], k[3], k[4], k[5])) > NMS_IOU for k in kept):
            continue
        kept.append(c)

    out = []
    for conf_score, cls_id, bx, by, _bw, _bh, mask in kept:
        if mask is not None:
            pcx, pcy = _mask_centroid_or_bbox(mask, bx, by)
        else:
            pcx, pcy = bx, by
        out.append((conf_score, cls_id, pcx, pcy))
    return out


def annotate_image(model, image_path, output_dir, conf, class_map=None):
    kept = _detect_seg_and_nms(model, image_path, conf, class_map)
    stem = os.path.splitext(os.path.basename(image_path))[0]
    label_path = os.path.join(output_dir, f"{stem}.txt")
    lines = [
        f"{cls_id} {cx:.6f} {cy:.6f}"
        for _, cls_id, cx, cy in kept
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
