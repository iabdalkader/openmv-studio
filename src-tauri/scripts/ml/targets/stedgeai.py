# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# stedgeai compile path for STM32 N6 (st-neural-art). Takes a QDQ ONNX
# (or int8 TFLite) and produces a relocatable network_rel.bin via the
# stedgeai generate + N6 reloc pipeline. Task-agnostic.

import os
import subprocess
import sys


def _find_stedgeai_bin(stedgeai_dir):
    # Bundle layout: <stedgeai_dir>/Utilities/<platform_subdir>/stedgeai
    # (subdir name varies per OS/arch -- macarm, linuxx86_64, etc.).
    # Exactly one platform's binaries ship per build, so first match wins.
    binname = "stedgeai.exe" if sys.platform == "win32" else "stedgeai"
    utilities = os.path.join(stedgeai_dir, "Utilities")
    if os.path.isdir(utilities):
        for sub in os.listdir(utilities):
            candidate = os.path.join(utilities, sub, binname)
            if os.path.isfile(candidate):
                return candidate
    raise FileNotFoundError(
        f"stedgeai binary not found under {utilities}"
    )


def compile(model_path, build_dir, models_dir, stedgeai_dir):
    if not stedgeai_dir or not os.path.isdir(stedgeai_dir):
        raise FileNotFoundError(
            f"stedgeai dir not provided or missing: {stedgeai_dir}"
        )
    npu_driver = os.path.join(
        stedgeai_dir, "scripts", "N6_reloc", "npu_driver.py"
    )
    if not os.path.isfile(npu_driver):
        raise FileNotFoundError(f"npu_driver.py not found at: {npu_driver}")
    stedgeai_bin = _find_stedgeai_bin(stedgeai_dir)
    print(f"stedgeai_bin={stedgeai_bin}", file=sys.stderr, flush=True)
    if not models_dir or not os.path.isdir(models_dir):
        raise FileNotFoundError(
            "STM32 N6 target requires --models-dir with neuralart.json"
        )
    config = os.path.join(models_dir, "neuralart.json")
    if not os.path.isfile(config):
        raise FileNotFoundError(f"neuralart.json not found at: {config}")

    model_name = os.path.basename(os.path.splitext(model_path)[0])
    output_dir = os.path.join(build_dir, model_name)
    os.makedirs(output_dir, exist_ok=True)

    # Strip Make-related env vars that could leak into the subprocess.
    env = os.environ.copy()
    for var in ["RM", "CFLAGS", "CPPFLAGS", "CXXFLAGS", "LDFLAGS", "MAKEFLAGS"]:
        env.pop(var, None)

    # --inputs-ch-position chlast presents NHWC IO at the model boundary
    # (stedgeai inserts a transpose) so the on-camera preprocessing -
    # which reads channels from the last dim of input_shape - sees
    # (1,H,W,C) like the old TFLite path. The internal graph stays NCHW.
    generate_command = [
        stedgeai_bin,
        "generate",
        "--target", "stm32n6",
        "--model", model_path,
        "--inputs-ch-position", "chlast",
        "--input-data-type", "uint8",
        "--output-data-type", "float32",
        "--relocatable",
        "--st-neural-art", f"default@{config}",
        "--workspace", os.path.join(output_dir, "workspace"),
        "--output", os.path.join(output_dir, "gen"),
        "--verbosity", "1",
        "--quiet",
    ]
    print(f"running stedgeai: {' '.join(generate_command)}",
          file=sys.stderr, flush=True)
    # Inherit stdout/stderr so stedgeai's diagnostics stream into our
    # log in real time (capture_output was swallowing the actual error).
    rc = subprocess.run(generate_command, env=env, stdin=subprocess.DEVNULL).returncode
    if rc != 0:
        raise RuntimeError(f"stedgeai generate failed (exit {rc})")

    reloc_command = [
        sys.executable,
        npu_driver,
        "--input", os.path.join(output_dir, "gen", "network.c"),
        "--output", output_dir,
        "--verbosity", "1",
    ]
    print(f"running N6 reloc: {' '.join(reloc_command)}",
          file=sys.stderr, flush=True)
    rc = subprocess.run(reloc_command, env=env, stdin=subprocess.DEVNULL).returncode
    if rc != 0:
        raise RuntimeError(f"N6 relocation failed (exit {rc})")

    out = os.path.join(output_dir, "network_rel.bin")
    if not os.path.exists(out):
        raise FileNotFoundError(f"stedgeai output not found: {out}")
    return out
