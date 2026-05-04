# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# NPU compile dispatch. Task-agnostic: takes a quantized artifact (int8
# TFLite for vela, QDQ ONNX or int8 TFLite for stedgeai) and returns a
# device-deployable artifact.

import os
import shutil

from . import stedgeai
from . import vela


def compile_for_target(input_path, output_path, target, models_dir, stedgeai_dir):
    """Run the NPU compiler matching the target. Compiled artifact is
    copied to output_path; intermediate build artifacts are cleaned up.
    """
    build_dir = os.path.join(os.path.dirname(output_path), "compile")
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir, exist_ok=True)

    if vela.is_target(target):
        compiled = vela.compile(input_path, build_dir, target, models_dir)
    elif target == "st-neural-art":
        compiled = stedgeai.compile(input_path, build_dir, models_dir, stedgeai_dir)
    else:
        raise ValueError(f"Unsupported target: {target}")

    shutil.copy2(compiled, output_path)
    shutil.rmtree(build_dir, ignore_errors=True)
