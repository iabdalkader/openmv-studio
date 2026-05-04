# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Convert a float ONNX to int8 TFLite via onnx2tf. The output is the
# *_full_integer_quant.tflite variant (uint8 IN / float32 OUT, full int8
# internal graph). Used by the bbox export for cpu/ethos-u55-* targets.
#
# Caller must have imported tensorflow before this module is reachable
# (the TF-before-onnx ordering rule); the dispatcher handles that.

import json
import os
import shutil
import sys


def convert(onnx_in, calib_npy, saved_model_dir, input_op_name="images"):
    """Run onnx2tf -> int8 TFLite and return the path of the
    full_integer_quant variant. saved_model_dir is wiped+recreated.
    input_op_name must match the ONNX graph's first input -- the
    bundled tf_wrapper-patched YOLO ONNX uses "images".
    """
    import numpy as np

    if os.path.exists(saved_model_dir):
        shutil.rmtree(saved_model_dir)

    # onnx2tf calls download_test_image_data() during convert for
    # auxiliary tensor shape inference. Its hardcoded URL returns 404,
    # and the bundled Python's network kill switch blocks the request
    # anyway. Patch it in-process to return a deterministic dummy
    # array. Patch BOTH the source module and the onnx2tf.onnx2tf
    # module: the latter does `from ...common_functions import
    # download_test_image_data`, so it holds its own binding that the
    # source-module patch alone won't reach.
    import onnx2tf
    import onnx2tf.onnx2tf as _o2t_main
    import onnx2tf.utils.common_functions as _o2t_cf

    def _stub_download_test_image_data():
        return np.zeros((20, 128, 128, 3), dtype=np.float32)

    _o2t_cf.download_test_image_data = _stub_download_test_image_data
    _o2t_main.download_test_image_data = _stub_download_test_image_data

    # Suppress json_auto_generator recovery. On any per-op conversion
    # error onnx2tf otherwise spawns `python -m onnx2tf` in a subprocess
    # (bare "python", no env) up to 3 times to search for parameter
    # replacements. In our bundled-Python sandbox bare "python" doesn't
    # resolve; even if it did, recovery only writes a hint JSON and
    # re-raises -- it never retries the conversion. Pass an empty
    # replacements file so the recovery branch is skipped and errors
    # propagate immediately.
    empty_prf = os.path.join(
        os.path.dirname(saved_model_dir), "_no_replacements.json"
    )
    with open(empty_prf, "w") as f:
        json.dump({"format_version": 1, "operations": []}, f)

    onnx2tf.convert(
        input_onnx_file_path=onnx_in,
        output_folder_path=saved_model_dir,
        not_use_onnxsim=True,
        verbosity="error",
        output_integer_quantized_tflite=True,
        custom_input_op_name_np_data_path=[
            [input_op_name, calib_npy, [[[[0, 0, 0]]]], [[[[255, 255, 255]]]]],
        ],
        enable_batchmatmul_unfold=False,
        output_signaturedefs=True,
        input_quant_dtype="uint8",
        output_quant_dtype="float32",
        param_replacement_file=empty_prf,
    )

    # Pick the full_integer_quant variant (uint8 IN / float32 OUT,
    # full int8 graph). Skip int16-act siblings.
    for f in sorted(os.listdir(saved_model_dir)):
        if "full_integer_quant" in f and "int16" not in f:
            return os.path.join(saved_model_dir, f)
    raise FileNotFoundError("full_integer_quant TFLite not found")
