# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# CenterNet-style training for the centerpoint task.
#
# Each labeled object is a single (cx, cy) point with class. The model
# (MobileNetV2 backbone + transposed-conv decoder) outputs:
#   - num_classes heatmap channels (logits, sigmoid'd in loss)
#   - 2 offset channels (sub-pixel correction at peak locations)
#
# Training:
#   - Target: 2D Gaussian (sigma=2 in heatmap-px) per point in the
#     correct class channel; offset = (cx - floor(cx_hm), cy - floor(cy_hm))
#     at the peak location.
#   - Loss: focal loss on heatmap channels + L1 on offset channels at
#     positive locations.
#   - Background images: empty .txt -> all-zero target. Same negative-
#     example mechanism as bbox.
#   - Augmentation: horizontal flip with point-coord transform, color
#     jitter. (Rotation is omitted in v1 to keep target-rebuild simple.)
#   - Optimizer: Adam with backbone LR 1e-5, decoder/head LR 1e-3,
#     cosine schedule.
#
# Per-epoch JSON event shape: {epoch, epochs, metrics: [[name, value], ...], timings}.
# Metric names are task-specific:
#   heatmap_loss -> focal loss on heatmap channels
#   offset_loss  -> L1 loss on offset channels at positives
#   peak_recall  -> fraction of GT centers matched on val at the
#                   distance threshold

import math
import os
import random
import time

import numpy as np

from ml import common


# Heatmap downsample factor relative to the input image. Must match
# the decoder: backbone is 1/32, three 2x upsamples -> 1/4.
DOWNSAMPLE = 4

# Gaussian supervision width in heatmap cells. Matches badger's
# sigma = RADIUS/3 = 4/3 = 1.33: cells one away from the peak hit
# target ~0.88, weighted (1-target)^4 ~ 1e-4 in focal loss, so they
# don't suppress the peak, and the peak saturates to ~0.9 without
# needing a positive-cell multiplier.
TARGET_SIGMA = 1.33

# Positive-cell multiplier. Reference CenterNet uses 1 because COCO
# has hundreds of positives per batch; on a 48x48*nc grid with 1-3
# objects per image the positive gradient is too sparse to saturate
# peaks within a 50-epoch budget.
POS_WEIGHT = 100.0

# ImageNet mean/std for the pretrained MobileNetV2 backbone. Skipping
# this and feeding raw [0,1] images is a 1.8-sigma distribution shift
# that the frozen BN running stats can't compensate for, so the
# backbone produces garbage features and nothing learns. NCHW shape so
# the broadcast against (B, 3, H, W) batches works.
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(3, 1, 1)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(3, 1, 1)


def _focal_loss(pred, target, a=2.0, b=4.0):
    """CornerNet/CenterNet penalty-reduced focal loss. `pred` is
    already sigmoid'd and clamped (model does that). `target` is
    in [0, 1].
    """
    import torch
    pos = target.eq(1).float()
    neg = 1.0 - pos
    neg_w = torch.pow(1.0 - target, b)
    pos_loss = torch.pow(1.0 - pred, a) * torch.log(pred) * pos
    neg_loss = torch.pow(pred, a) * torch.log(1.0 - pred) * neg_w * neg
    n_pos = pos.sum()
    pos_loss = pos_loss.sum() * POS_WEIGHT
    neg_loss = neg_loss.sum()
    if n_pos == 0:
        return -neg_loss
    return -(pos_loss + neg_loss) / n_pos


def _gaussian_2d(heatmap, cx, cy, sigma=TARGET_SIGMA):
    """Add a 2D Gaussian peak (max-merged into existing channel) to a
    single-channel heatmap of shape (H, W). cx, cy in heatmap pixels.
    """
    H, W = heatmap.shape
    radius = int(math.ceil(3 * sigma))
    x0, y0 = int(round(cx)), int(round(cy))
    x_min, x_max = max(0, x0 - radius), min(W, x0 + radius + 1)
    y_min, y_max = max(0, y0 - radius), min(H, y0 + radius + 1)
    if x_min >= x_max or y_min >= y_max:
        return
    ys = np.arange(y_min, y_max).reshape(-1, 1)
    xs = np.arange(x_min, x_max).reshape(1, -1)
    g = np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * sigma ** 2))
    region = heatmap[y_min:y_max, x_min:x_max]
    np.maximum(region, g.astype(np.float32), out=region)


class CenterpointDataset:
    """Iterable dataset producing (image, target_heatmap, target_offset,
    offset_mask) tuples for CenterNet training.

    Targets are precomputed per-sample in __getitem__ using numpy, then
    passed to torch via tensor conversion. Augmentation is applied
    consistently to both image and target points.
    """

    def __init__(self, project_dir, stems, num_classes, imgsz, augment):
        self.project_dir = project_dir
        self.stems = stems
        self.num_classes = num_classes
        self.imgsz = imgsz
        self.augment = augment
        self.heatmap_size = imgsz // DOWNSAMPLE

    def __len__(self):
        return len(self.stems)

    def _read_points(self, stem):
        """Returns list of (cls, cx, cy) tuples in normalized [0,1] image
        coords. Empty list for background images.
        """
        path = os.path.join(self.project_dir, "labels", f"{stem}.txt")
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return []
        points = []
        with open(path) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3:
                    cls = int(parts[0])
                    cx = float(parts[1])
                    cy = float(parts[2])
                    points.append((cls, cx, cy))
        return points

    def __getitem__(self, idx):
        from PIL import Image

        stem = self.stems[idx]
        img_path = os.path.join(self.project_dir, "images", f"{stem}.jpg")
        im = Image.open(img_path).convert("RGB").resize(
            (self.imgsz, self.imgsz), Image.BILINEAR
        )
        points = self._read_points(stem)

        # Random scale jitter + crop. Reference CenterNet relies on
        # this for small-data robustness; without it, the model
        # plateaus early because every training image is seen at the
        # same scale and framing. Crop scale in [0.7, 1.0] of the
        # resized image, random origin, resized back to imgsz. Points
        # outside the cropped window are dropped.
        if self.augment and random.random() < 0.7:
            crop_scale = random.uniform(0.7, 1.0)
            crop_size = int(self.imgsz * crop_scale)
            max_off = self.imgsz - crop_size
            cx_off = random.randint(0, max_off) if max_off > 0 else 0
            cy_off = random.randint(0, max_off) if max_off > 0 else 0
            im = im.crop((cx_off, cy_off,
                          cx_off + crop_size, cy_off + crop_size))
            im = im.resize((self.imgsz, self.imgsz), Image.BILINEAR)
            transformed = []
            for c, cx, cy in points:
                nx = (cx * self.imgsz - cx_off) / crop_size
                ny = (cy * self.imgsz - cy_off) / crop_size
                # Strict upper bound: a point at exactly 1.0 rounds to
                # cell H, which is out of grid and silently dropped.
                if 0.0 <= nx < 1.0 and 0.0 <= ny < 1.0:
                    transformed.append((c, nx, ny))
            points = transformed

        # Augmentation: horizontal flip with point coord transform.
        if self.augment and random.random() < 0.5:
            im = im.transpose(Image.FLIP_LEFT_RIGHT)
            points = [(c, 1.0 - cx, cy) for (c, cx, cy) in points]

        arr = np.asarray(im, dtype=np.float32) / 255.0
        # NHWC -> NCHW
        arr = arr.transpose(2, 0, 1)

        # Color jitter (brightness, in [0, 1] space before ImageNet norm).
        if self.augment and random.random() < 0.5:
            arr = arr * random.uniform(0.8, 1.2)
            arr = np.clip(arr, 0.0, 1.0)

        # ImageNet normalization for the pretrained MobileNetV2 backbone.
        # Without this the backbone's BN running stats see a ~1.8-sigma
        # distribution shift and the pretrained features collapse to
        # noise.
        arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
        # Force C-contiguous: the earlier transpose(2,0,1) view leaks
        # HWC-in-memory layout through numpy ops; the resulting tensor
        # is non-contiguous BCHW-shape-on-BHWC-memory, which trips MPS
        # BN backward on torch 2.5+ ("view size is not compatible").
        arr = np.ascontiguousarray(arr)

        H = self.heatmap_size
        heatmap = np.zeros((self.num_classes, H, H), dtype=np.float32)

        for cls, cx, cy in points:
            if cls < 0 or cls >= self.num_classes:
                continue
            cx_hm = cx * H
            cy_hm = cy * H
            ix, iy = int(round(cx_hm)), int(round(cy_hm))
            if 0 <= ix < H and 0 <= iy < H:
                # Gaussian centered on the integer cell so the cell
                # itself is exactly 1.0 (= peak) for focal loss.
                _gaussian_2d(heatmap[cls], ix, iy)

        return arr, heatmap


def _collate(batch):
    import torch
    imgs = torch.from_numpy(np.stack([b[0] for b in batch]))
    hm = torch.from_numpy(np.stack([b[1] for b in batch]))
    return imgs, hm


def _dump_failures(model, loader, device, num_classes, project_dir, top_n=10):
    """Run val once, find the worst-N samples (most GT centers missed),
    and dump per-sample PNGs into runs/train/failures/. Each PNG shows
    the input image, the GT centers (red), and the top predicted peaks
    (green = matched, yellow = unmatched). Lets us eyeball whether
    failures are "no peak", "peak in wrong place", or "low confidence".
    """
    import torch
    import torch.nn.functional as F
    from PIL import Image, ImageDraw

    out_dir = os.path.join(project_dir, "runs", "train", "failures")
    if os.path.isdir(out_dir):
        for f in os.listdir(out_dir):
            os.remove(os.path.join(out_dir, f))
    os.makedirs(out_dir, exist_ok=True)

    R_PX = 16
    R = max(1, R_PX // DOWNSAMPLE)
    K = 20
    PEAK_DRAW_THRESHOLD = 0.1  # draw any peak above this for inspection

    samples = []
    model.eval()
    with torch.no_grad():
        for imgs, hm in loader:
            imgs_dev = imgs.to(device)
            hm_dev = hm.to(device)
            cls = model(imgs_dev)
            keep = (F.max_pool2d(cls, kernel_size=3, stride=1, padding=1) == cls).float()
            peaks_t = cls * keep
            B, C, H, W = peaks_t.shape
            flat = peaks_t.reshape(B, C, -1)
            kk = min(K, H * W)
            vals, idx = flat.topk(kk, dim=2)
            py = (idx // W)
            px = (idx % W)

            for b in range(B):
                gt_list = []
                for c in range(C):
                    yy, xx = torch.where(hm_dev[b, c] == 1.0)
                    for y, x in zip(yy.tolist(), xx.tolist()):
                        gt_list.append((c, y, x))
                miss = 0
                gt_match = []
                for c, gy, gx in gt_list:
                    pyc = py[b, c]
                    pxc = px[b, c]
                    dy = (pyc - gy).abs()
                    dx = (pxc - gx).abs()
                    matched = bool(((dy <= R) & (dx <= R)).any().item())
                    gt_match.append((c, gy, gx, matched))
                    if not matched:
                        miss += 1
                pk_list = []
                for c in range(C):
                    for k in range(kk):
                        v = float(vals[b, c, k].item())
                        if v < PEAK_DRAW_THRESHOLD:
                            break
                        pk_list.append((c, int(py[b, c, k].item()), int(px[b, c, k].item()), v))
                samples.append({
                    "miss": miss,
                    "n_gt": len(gt_list),
                    "img": imgs[b].cpu(),
                    "gt": gt_match,
                    "peaks": pk_list,
                })

    samples.sort(key=lambda s: (s["miss"], s["n_gt"]), reverse=True)
    failures = [s for s in samples if s["miss"] > 0][:top_n]

    palette = [(255, 80, 80), (80, 200, 255), (255, 200, 80), (180, 100, 255)]
    for rank, s in enumerate(failures):
        arr = s["img"].numpy() * IMAGENET_STD + IMAGENET_MEAN
        arr = np.clip(arr * 255, 0, 255).astype(np.uint8).transpose(1, 2, 0)
        pil = Image.fromarray(arr).convert("RGB")
        # Draw on a 2x-upscaled copy so markers are crisp on small inputs.
        scale = 2
        pil = pil.resize((pil.width * scale, pil.height * scale), Image.NEAREST)
        draw = ImageDraw.Draw(pil)

        cell_to_px = DOWNSAMPLE * scale
        half = cell_to_px // 2

        # GT centers: red X, big.
        for c, gy, gx, matched in s["gt"]:
            cx = gx * cell_to_px + half
            cy = gy * cell_to_px + half
            r = 8
            color = (0, 255, 0) if matched else (255, 0, 0)
            draw.line([(cx - r, cy - r), (cx + r, cy + r)], fill=color, width=3)
            draw.line([(cx - r, cy + r), (cx + r, cy - r)], fill=color, width=3)

        # Predicted peaks: circle with class color; outline thicker if unmatched.
        for c, pyk, pxk, v in s["peaks"]:
            cx = pxk * cell_to_px + half
            cy = pyk * cell_to_px + half
            r = 6
            color = palette[c % len(palette)]
            draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=color, width=2)
            draw.text((cx + r + 2, cy - r), "%.2f" % v, fill=color)

        title = "miss=%d/%d  n_peaks=%d" % (s["miss"], s["n_gt"], len(s["peaks"]))
        draw.text((4, 4), title, fill=(255, 255, 255))
        pil.save(os.path.join(out_dir, "fail_%02d.png" % rank))

    common.emit({"status": "failures_dumped", "count": len(failures), "dir": out_dir})


def _validate(model, loader, device, num_classes):
    """Vectorized peak-detection score on val. Returns the fraction of
    GT peaks whose location is among the top-K=20 predicted peaks per
    (image, class) within an L_inf radius R of the GT.
    """
    import torch
    import torch.nn.functional as F

    K = 20
    # Match radius in INPUT pixels, converted to heatmap cells. Keeping
    # it in input-pixel units makes peak_recall comparable across
    # stride choices: at /8, R=16px == 2 cells; at /4, R=16px == 4.
    R_PX = 16
    R = max(1, R_PX // DOWNSAMPLE)
    model.eval()
    total_gt = 0
    total_hit = 0
    with torch.no_grad():
        for imgs, hm in loader:
            imgs = imgs.to(device)
            hm_dev = hm.to(device)
            cls = model(imgs)
            keep = (F.max_pool2d(cls, kernel_size=3, stride=1, padding=1) == cls).float()
            peaks = cls * keep
            B, C, H, W = peaks.shape
            flat = peaks.reshape(B, C, -1)
            kk = min(K, H * W)
            _, idx = flat.topk(kk, dim=2)
            py = (idx // W).to(torch.int32)
            px = (idx % W).to(torch.int32)

            gt_mask = (hm_dev == 1.0)
            gt_b, gt_c, gt_y, gt_x = torch.where(gt_mask)
            total_gt += int(gt_b.numel())
            if gt_b.numel() == 0:
                continue
            pred_y = py[gt_b, gt_c]
            pred_x = px[gt_b, gt_c]
            dy = (pred_y - gt_y.to(torch.int32).unsqueeze(1)).abs()
            dx = (pred_x - gt_x.to(torch.int32).unsqueeze(1)).abs()
            hit = ((dy <= R) & (dx <= R)).any(dim=1)
            total_hit += int(hit.sum().item())
    model.train()
    if total_gt == 0:
        return 0.0
    return total_hit / total_gt


def main(args, project):
    import multiprocessing as mp
    import torch
    from torch.utils.data import DataLoader

    classes = project["classes"]
    num_classes = len(classes)

    common.emit({"status": "preparing_dataset"})
    train_stems, val_stems, _, _, _ = common.dataset_summary(args.project)

    device = torch.device(common.select_device())

    # Apple Silicon CPU defaults torch to a small thread count; bump
    # it up so the optimizer step + per-batch loss/maxpool don't
    # under-utilize the cores.
    cpu_count = os.cpu_count() or 1
    torch.set_num_threads(max(1, cpu_count - 1))

    train_ds = CenterpointDataset(args.project, train_stems, num_classes, args.imgsz, augment=True)
    val_ds = CenterpointDataset(args.project, val_stems, num_classes, args.imgsz, augment=False)

    # Move per-sample JPEG decode + heatmap target generation off the
    # training thread. macOS python sidecar requires "spawn" since
    # "fork" deadlocks here. persistent_workers avoids re-spawning per
    # epoch.
    nworkers = max(0, min(4, cpu_count // 2))
    loader_kwargs = {
        "batch_size": args.batch,
        "collate_fn": _collate,
        "drop_last": False,
        "num_workers": nworkers,
    }
    if nworkers > 0:
        loader_kwargs["persistent_workers"] = True
        loader_kwargs["multiprocessing_context"] = mp.get_context("spawn")
    train_loader = DataLoader(train_ds, shuffle=True, **loader_kwargs)
    val_loader = DataLoader(val_ds, shuffle=False, **loader_kwargs)

    from ml.networks.heatmap import build_heatmap_model
    model = build_heatmap_model(num_classes=num_classes, models_dir=args.models_dir)
    model.to(device)

    # Two-LR optimizer: low rate on the pretrained backbone so its
    # AdamW + weight_decay, two LR groups, head at 1e-3, backbone at
    # 1e-4 (badger reference settings).
    LR = 1e-3
    head_params = [
        p for n, p in model.named_parameters() if not n.startswith("backbone.")
    ]
    bb_params = [
        p for n, p in model.named_parameters() if n.startswith("backbone.")
    ]
    optimizer = torch.optim.AdamW(
        [{"params": head_params, "lr": LR},
         {"params": bb_params, "lr": LR * 0.1}],
        weight_decay=1e-4,
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=args.epochs
    )

    common.emit({"status": "training_started"})
    run_start = time.monotonic()

    weights_dir = os.path.join(args.project, "runs", "train", "weights")
    os.makedirs(weights_dir, exist_ok=True)
    best_path = os.path.join(weights_dir, "best.pt")
    best_score = -1.0

    total_batches_per_epoch = max(1, len(train_loader))
    for epoch in range(args.epochs):
        # Backbone frozen on epoch 0, unfrozen from epoch 1.
        for p in model.backbone.parameters():
            p.requires_grad = (epoch >= 1)
        epoch_start = time.monotonic()
        model.train()
        epoch_focal = 0.0
        n_batches = 0
        last_progress_emit = time.monotonic()
        for imgs, hm in train_loader:
            imgs = imgs.to(device)
            hm = hm.to(device)

            cls_pred = model(imgs)
            loss = _focal_loss(cls_pred, hm)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            epoch_focal += float(loss.item())
            n_batches += 1

            # On CPU the per-epoch budget is minutes, so emit a
            # heartbeat every couple seconds so the UI sees progress.
            now = time.monotonic()
            if now - last_progress_emit > 2.0:
                common.emit({
                    "status": "training_step",
                    "epoch": epoch + 1,
                    "epochs": args.epochs,
                    "batch": n_batches,
                    "batches": total_batches_per_epoch,
                })
                last_progress_emit = now

        scheduler.step()
        if n_batches > 0:
            epoch_focal /= n_batches

        score = _validate(model, val_loader, device, num_classes)

        now = time.monotonic()
        epoch_secs = now - epoch_start
        elapsed = now - run_start
        eta = epoch_secs * max(0, args.epochs - (epoch + 1))

        common.emit({
            "epoch": epoch + 1,
            "epochs": args.epochs,
            "metrics": [
                {"name": "heatmap_loss",
                 "value": round(epoch_focal, 4),
                 "range": None},
                {"name": "peak_recall",
                 "value": round(score, 4),
                 "range": [0.0, 1.0]},
            ],
            "epoch_secs": round(epoch_secs, 2),
            "elapsed_secs": round(elapsed, 2),
            "eta_secs": round(eta, 2),
        })

        if score > best_score:
            best_score = score
            # Save state dict + minimal config so export.py can rebuild.
            torch.save({
                "state_dict": model.state_dict(),
                "num_classes": num_classes,
                "imgsz": args.imgsz,
            }, best_path)
            # Refresh failure dumps for this best so a stop-mid-training
            # still leaves us with usable diagnostics for the model
            # currently on disk.
            try:
                _dump_failures(model, val_loader, device, num_classes, args.project)
            except Exception as e:
                common.emit({"status": "failures_dump_failed", "error": str(e)})

    common.emit({
        "status": "done",
        "best_weights": best_path,
        "exists": os.path.exists(best_path),
    })

    # DataLoader workers hold shared-memory regions whose cleanup
    # hangs Python's normal exit path -- the script never returns
    # until the user kills it. Bypass Python cleanup with os._exit.
    os._exit(0)
