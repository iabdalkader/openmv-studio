# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Encoder-decoder for heatmap-style dense-prediction tasks.
#
# MobileNetV2 (ImageNet) features at /32 -> 3 decoder blocks ->
# /4 -> 3x3+1x1 head with N output channels (sigmoid+clamp inside
# forward). NPU-friendly ops only.

import math

import torch
import torch.nn as nn


def _decoder_block(in_ch, out_ch):
    return nn.Sequential(
        nn.Conv2d(in_ch, out_ch, kernel_size=1, bias=False),
        nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False),
        nn.Conv2d(out_ch, out_ch, kernel_size=3, padding=1, bias=False),
        nn.BatchNorm2d(out_ch),
        nn.ReLU(inplace=True),
    )


def _head(in_ch, out_ch):
    return nn.Sequential(
        nn.Conv2d(in_ch, in_ch, kernel_size=3, padding=1),
        nn.ReLU(inplace=True),
        nn.Conv2d(in_ch, out_ch, kernel_size=1),
    )


class HeatmapModel(nn.Module):
    """forward(x) returns the heatmap as (B, num_classes, H, W) in
    raw logit space. Sigmoid is applied in the loss and in the
    firmware post-process.
    """

    def __init__(self, num_classes, neck_widths=(256, 128, 64),
                 backbone="mobilenet_v2", pretrained=True, models_dir=None):
        super().__init__()
        if backbone != "mobilenet_v2":
            raise ValueError(f"backbone {backbone!r} not supported yet")

        import os
        from torchvision import models

        mbnv2 = models.mobilenet_v2(weights=None)
        if pretrained:
            weights_path = os.path.join(models_dir, "mobilenet_v2.pth")
            state = torch.load(weights_path, map_location="cpu", weights_only=True)
            mbnv2.load_state_dict(state)
        # MobileNetV2.features outputs 1280 channels at 1/32 stride.
        self.backbone = mbnv2.features
        self.backbone_out_ch = 1280

        in_ch = self.backbone_out_ch
        decoder_layers = []
        for out_ch in neck_widths:
            decoder_layers.append(_decoder_block(in_ch, out_ch))
            in_ch = out_ch
        self.decoder = nn.Sequential(*decoder_layers)

        self.hm_head = _head(in_ch, num_classes)

        # Focal-loss prior bias: target peak frequency p~0.1, init bias
        # = -log((1-p)/p). Stops the early epochs from collapsing to
        # "predict 0 everywhere."
        pi = 0.1
        nn.init.constant_(self.hm_head[-1].bias, -math.log((1 - pi) / pi))

    def forward(self, x):
        feats = self.backbone(x)
        feats = self.decoder(feats)
        return torch.sigmoid(self.hm_head(feats)).clamp(1e-4, 1 - 1e-4)


def build_heatmap_model(num_classes, **kwargs):
    return HeatmapModel(num_classes, **kwargs)
