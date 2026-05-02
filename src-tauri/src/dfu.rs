// Copyright (C) 2026 OpenMV, LLC.
//
// This software is licensed under terms that can be found in the
// LICENSE file in the root directory of this software component.

use crate::{AppState, Board, resolve_resource};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};

pub struct DfuChild(pub Mutex<Option<CommandChild>>);

impl DfuChild {
    pub fn new() -> Self {
        Self(Mutex::new(None))
    }

    pub fn kill_running(&self) {
        if let Ok(mut g) = self.0.lock() {
            if let Some(child) = g.take() {
                log::info!("Killing in-flight dfu-util child");
                let _ = child.kill();
            }
        }
    }
}

pub struct DfuConfig {
    pub vid_pid: String,
    pub fs_partition: Vec<String>,
}

fn tokenize_dfu_cmd(cmd: &str) -> Vec<String> {
    cmd.split_whitespace().map(|s| s.to_string()).collect()
}

// dfu-util emits progress lines like:
//   Upload   [=========================] 100%     20475904 bytes
//   Download [=========================]  99%      8388608 bytes
//   Erase    [=========================] 100%         4096 bytes
// Extract the byte count so the frontend can drive the progress bar.
fn parse_dfu_bytes(line: &str) -> Option<u64> {
    let trimmed = line.trim_start();
    let kind_end = trimmed.find(char::is_whitespace)?;
    let kind = &trimmed[..kind_end];
    if kind != "Upload" && kind != "Download" && kind != "Erase" {
        return None;
    }
    let bracket_end = trimmed.find(']')?;
    let after = &trimmed[bracket_end + 1..];
    let percent_end = after.find('%')?;
    let rest = after[percent_end + 1..].trim_start();
    let num_end = rest
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(rest.len());
    if num_end == 0 {
        return None;
    }
    rest[..num_end].parse::<u64>().ok()
}

fn run_dfu(app: &AppHandle, args: &[String]) -> Result<(), String> {
    let dfu_name = format!("tools/dfu-util{}", std::env::consts::EXE_SUFFIX);
    let dfu_path = resolve_resource(app, &dfu_name);
    let cmd_line = format!("{} {}", dfu_path.display(), args.join(" "));
    log::info!("Running: {}", cmd_line);
    let _ = app.emit("dfu-output", format!("$ {}", cmd_line).as_str());
    let sidecar = app.shell().command(&dfu_path).args(args);

    let (mut rx, child) = sidecar
        .spawn()
        .map_err(|e| format!("Failed to spawn dfu-util: {}", e))?;

    let child_state = app.state::<std::sync::Arc<DfuChild>>();
    if let Ok(mut g) = child_state.0.lock() {
        *g = Some(child);
    }

    let status = tauri::async_runtime::block_on(async {
        let mut exit_code: Option<i32> = None;
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                    let text = String::from_utf8_lossy(&line);
                    if let Some(bytes) = parse_dfu_bytes(&text) {
                        // Numeric payload on dfu-status drives the progress
                        // bar without overwriting the current stage string.
                        let _ = app.emit("dfu-status", bytes);
                    } else {
                        log::info!("dfu-util: {}", text);
                        let _ = app.emit("dfu-output", text.as_ref());
                    }
                }
                CommandEvent::Terminated(payload) => {
                    exit_code = payload.code;
                }
                _ => {}
            }
        }
        exit_code
    });

    let child_state = app.state::<std::sync::Arc<DfuChild>>();
    if let Ok(mut g) = child_state.0.lock() {
        *g = None;
    }

    if status != Some(0) {
        let msg = format!("dfu-util exited with status {}", status.unwrap_or(-1));
        let _ = app.emit("dfu-status", msg.as_str());
        return Err(msg);
    }
    Ok(())
}

pub fn erase_filesystem(app: &AppHandle, config: &DfuConfig) -> Result<(), String> {
    let _ = app.emit("dfu-status", "Creating temporary erase file...");

    let temp_dir = std::env::temp_dir();
    let temp_path = temp_dir.join("openmv_erase.bin");
    let erase_data = vec![0xFFu8; 4096];
    std::fs::write(&temp_path, &erase_data)
        .map_err(|e| format!("Failed to create temp file: {}", e))?;

    let total = config.fs_partition.len();

    for (i, cmd) in config.fs_partition.iter().enumerate() {
        let is_last = i == total - 1;

        let mut args = vec![
            "-w".to_string(),
            "-d".to_string(),
            format!(",{}", config.vid_pid),
        ];
        args.extend(tokenize_dfu_cmd(cmd));
        args.push("-D".to_string());
        args.push(temp_path.to_string_lossy().to_string());

        if is_last {
            args.push("--reset".to_string());
        }

        let _ = app.emit(
            "dfu-status",
            format!("Running dfu-util ({}/{})...", i + 1, total).as_str(),
        );

        if let Err(e) = run_dfu(app, &args) {
            let _ = std::fs::remove_file(&temp_path);
            let _ = app.emit("dfu-done", ());
            return Err(e);
        }
    }

    let _ = std::fs::remove_file(&temp_path);
    let _ = app.emit("dfu-status", "Erase complete.");
    let _ = app.emit("dfu-done", ());
    Ok(())
}

// If the partition uses DfuSe (-s 0xADDR) without a length suffix, append
// ":<size>" so dfu-util knows how much to upload. For plain alt-only
// partitions dfu-util reads the entire alt setting.
fn upload_args_for(partition_args: &str, size: usize) -> Vec<String> {
    let needs_size_suffix =
        partition_args.contains("-s ") && !partition_args.contains(':');
    if needs_size_suffix {
        let mut tokens = tokenize_dfu_cmd(partition_args);
        if let Some(idx) = tokens.iter().position(|t| t == "-s") {
            if let Some(addr) = tokens.get_mut(idx + 1) {
                *addr = format!("{}:0x{:x}", addr, size);
            }
        }
        tokens
    } else {
        tokenize_dfu_cmd(partition_args)
    }
}

// Scan for connected DFU devices and return a deduplicated list of
// "vid:pid" strings (lowercase hex, no `0x` prefix). The dfu-util `-l`
// output has lines of the form:
//   Found DFU: [37c5:96e3] ver=0101, devnum=4, cfg=1, intf=0, ...
//   Found Runtime: [...] ...
// We accept both DFU and Runtime entries -- the caller decides what to do.
pub fn list_devices(app: &AppHandle) -> Result<Vec<String>, String> {
    let dfu_name = format!("tools/dfu-util{}", std::env::consts::EXE_SUFFIX);
    let dfu_path = resolve_resource(app, &dfu_name);
    let output = std::process::Command::new(&dfu_path)
        .arg("-l")
        .output()
        .map_err(|e| format!("Failed to run dfu-util -l: {}", e))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut seen = std::collections::BTreeSet::new();
    for line in stdout.lines() {
        let trimmed = line.trim_start();
        if !trimmed.starts_with("Found DFU") && !trimmed.starts_with("Found Runtime") {
            continue;
        }
        let Some(start) = trimmed.find('[') else {
            continue;
        };
        let after = &trimmed[start + 1..];
        let Some(end) = after.find(']') else {
            continue;
        };
        let inner = &after[..end];
        if inner.contains(':') && inner.len() == 9 {
            seen.insert(inner.to_lowercase());
        }
    }
    Ok(seen.into_iter().collect())
}

pub fn resolve_dfu_board(
    app: &AppHandle,
    state: &Arc<Mutex<AppState>>,
) -> Result<Board, String> {
    {
        let st = state.lock().map_err(|e| e.to_string())?;
        if let Some(info) = st.sysinfo.as_ref() {
            if let Some(board) = st
                .boards
                .iter()
                .find(|b| b.vid == info.usb_vid && b.pid == info.usb_pid)
            {
                if board.bootloader_vid_pid.is_none() {
                    return Err("Board does not support DFU".to_string());
                }
                if board.romfs_partition.is_none() {
                    return Err("Board does not have a ROMFS partition".to_string());
                }
                let mut b = board.clone();
                b.in_firmware_mode = true;
                return Ok(b);
            }
        }
    }

    let devices = list_devices(app)?;
    let st = state.lock().map_err(|e| e.to_string())?;
    for vp in &devices {
        for board in &st.boards {
            let Some(ref bvp) = board.bootloader_vid_pid else {
                continue;
            };
            if bvp.to_lowercase() != *vp {
                continue;
            }
            if board.romfs_partition.is_none() {
                return Err("Board does not have a ROMFS partition".to_string());
            }
            let mut b = board.clone();
            b.in_firmware_mode = false;
            return Ok(b);
        }
    }
    Err("No supported DFU board found.".to_string())
}

pub fn upload_partition(
    app: &AppHandle,
    vid_pid: &str,
    partition_args: &str,
    size: usize,
) -> Result<Vec<u8>, String> {
    let temp_dir = std::env::temp_dir();
    let temp_path = temp_dir.join("openmv_romfs_upload.bin");
    let _ = std::fs::remove_file(&temp_path);

    let mut args = vec!["-d".to_string(), format!(",{}", vid_pid)];
    args.extend(upload_args_for(partition_args, size));
    args.push("-U".to_string());
    args.push(temp_path.to_string_lossy().to_string());

    let _ = app.emit("dfu-status", "Reading ROMFS partition...");
    let res = run_dfu(app, &args);

    if let Err(e) = res {
        let _ = std::fs::remove_file(&temp_path);
        return Err(e);
    }

    let bytes = std::fs::read(&temp_path)
        .map_err(|e| format!("Failed to read uploaded file: {}", e))?;
    let _ = std::fs::remove_file(&temp_path);
    Ok(bytes)
}

pub fn download_partition(
    app: &AppHandle,
    vid_pid: &str,
    partition_args: &str,
    data: &[u8],
    reset: bool,
) -> Result<(), String> {
    let temp_dir = std::env::temp_dir();
    let temp_path = temp_dir.join("openmv_romfs_download.bin");
    std::fs::write(&temp_path, data)
        .map_err(|e| format!("Failed to write temp image: {}", e))?;

    let mut args = vec![
        "-w".to_string(),
        "-d".to_string(),
        format!(",{}", vid_pid),
    ];
    args.extend(tokenize_dfu_cmd(partition_args));
    args.push("-D".to_string());
    args.push(temp_path.to_string_lossy().to_string());

    if reset {
        args.push("--reset".to_string());
    }

    let _ = app.emit("dfu-status", "Writing ROMFS partition...");
    let res = run_dfu(app, &args);

    let _ = std::fs::remove_file(&temp_path);
    res
}

pub fn exit_dfu(app: &AppHandle) -> Result<(), String> {
    let args = vec![
        "-a".to_string(),
        "0".to_string(),
        "--detach".to_string(),
        "--reset".to_string(),
    ];
    let _ = app.emit("dfu-status", "Exiting DFU mode...");
    run_dfu(app, &args)
}
