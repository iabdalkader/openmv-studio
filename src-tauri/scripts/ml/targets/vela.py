# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenMV, LLC.
#
# Vela compile path for Ethos-U55 targets. Takes an int8 TFLite, runs
# Vela through the bundled Python, returns the path of the compiled
# *_vela.tflite. Task-agnostic.

import os
import subprocess
import sys


# Per-target Vela flags. Match tools/vela.ini system configs and the
# firmware's per-core layout (HP core gets DTCM_MRAM, HE core gets
# SRAM_MRAM). All targets use Shared_Sram and Performance.
TARGET_ARGS = {
    "ethos-u55-256": [
        "--accelerator-config", "ethos-u55-256",
        "--system-config", "RTSS_HP_DTCM_MRAM",
        "--memory-mode", "Shared_Sram",
        "--optimise", "Performance",
    ],
    "ethos-u55-128": [
        "--accelerator-config", "ethos-u55-128",
        "--system-config", "RTSS_HE_SRAM_MRAM",
        "--memory-mode", "Shared_Sram",
        "--optimise", "Performance",
    ],
}


def is_target(target):
    return target in TARGET_ARGS


def compile(model_path, build_dir, target, models_dir):
    if target not in TARGET_ARGS:
        raise ValueError(f"Unsupported Vela target: {target}")
    if not models_dir or not os.path.isdir(models_dir):
        raise FileNotFoundError(
            "Vela target requires --models-dir with vela.ini"
        )
    vela_ini = os.path.join(models_dir, "vela.ini")
    if not os.path.isfile(vela_ini):
        raise FileNotFoundError(f"vela.ini not found at: {vela_ini}")

    model = os.path.basename(os.path.splitext(model_path)[0])
    # Run vela through the bundled Python instead of the `vela` console
    # script: the script lives in the Python install's bin/ which is not
    # on PATH for the Tauri-spawned subprocess. `python -m ethosu.vela`
    # works off the package's __main__.py and matches the console-script
    # entry.
    command = [
        sys.executable,
        "-m", "ethosu.vela",
        *TARGET_ARGS[target],
        "--output-dir", build_dir,
        "--config", vela_ini,
        model_path,
    ]
    try:
        subprocess.run(command, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"vela failed (exit {e.returncode}): {e.stderr}"
        )

    out = os.path.join(build_dir, f"{model}_vela.tflite")
    if not os.path.exists(out):
        raise FileNotFoundError(f"Vela output not found: {out}")
    return out
