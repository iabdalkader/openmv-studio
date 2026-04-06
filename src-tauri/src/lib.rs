mod protocol;

use std::sync::Mutex;
use tauri::State;
use tauri::ipc;

use protocol::camera::{Camera, Command, Response, list_ports};

struct AppState {
    camera: Camera,
}

#[tauri::command]
fn cmd_list_ports() -> Vec<String> {
    list_ports()
}

#[tauri::command]
fn cmd_connect(port: String, state: State<Mutex<AppState>>) -> Result<Response, String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    let resp = st.camera.execute(Command::Connect { port, baudrate: 921600 });
    match &resp {
        Response::Error(e) => Err(e.clone()),
        _ => Ok(resp),
    }
}

#[tauri::command]
fn cmd_disconnect(state: State<Mutex<AppState>>) -> Result<Response, String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    Ok(st.camera.execute(Command::Disconnect))
}

#[tauri::command]
fn cmd_run_script(script: String, state: State<Mutex<AppState>>) -> Result<Response, String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    let resp = st.camera.execute(Command::RunScript(script));
    match &resp {
        Response::Error(e) => Err(e.clone()),
        _ => Ok(resp),
    }
}

#[tauri::command]
fn cmd_stop_script(state: State<Mutex<AppState>>) -> Result<Response, String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    let resp = st.camera.execute(Command::StopScript);
    match &resp {
        Response::Error(e) => Err(e.clone()),
        _ => Ok(resp),
    }
}

#[tauri::command]
fn cmd_read_stdout(state: State<Mutex<AppState>>) -> Result<Response, String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    Ok(st.camera.execute(Command::ReadStdout))
}

#[tauri::command]
fn cmd_enable_streaming(enable: bool, state: State<Mutex<AppState>>) -> Result<Response, String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    let resp = st.camera.execute(Command::EnableStreaming(enable));
    match &resp {
        Response::Error(e) => Err(e.clone()),
        _ => Ok(resp),
    }
}

/// Combined poll: returns stdout as JSON, frame as binary.
/// The response is a custom binary format:
///   [stdout_len: u32 LE] [stdout_bytes] [width: u32 LE] [height: u32 LE]
///   [format_len: u8] [format_str] [is_jpeg: u8] [frame_data]
/// If no frame, width=0 height=0.
#[tauri::command]
fn cmd_poll(state: State<Mutex<AppState>>) -> Result<ipc::Response, String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    let resp = st.camera.execute(Command::Poll);

    match resp {
        Response::PollResult { stdout, frame } => {
            let stdout_bytes = stdout.unwrap_or_default().into_bytes();
            let mut buf = Vec::with_capacity(stdout_bytes.len() + 256);

            // Stdout: length-prefixed
            buf.extend_from_slice(&(stdout_bytes.len() as u32).to_le_bytes());
            buf.extend_from_slice(&stdout_bytes);

            // Frame header
            if let Some(f) = frame {
                buf.extend_from_slice(&f.width.to_le_bytes());
                buf.extend_from_slice(&f.height.to_le_bytes());
                let fmt = f.format_str.as_bytes();
                buf.push(fmt.len() as u8);
                buf.extend_from_slice(fmt);
                buf.push(f.is_jpeg as u8);
                buf.extend_from_slice(&f.data);
            } else {
                // No frame: width=0, height=0
                buf.extend_from_slice(&0u32.to_le_bytes());
                buf.extend_from_slice(&0u32.to_le_bytes());
            }

            Ok(ipc::Response::new(buf))
        }
        Response::Error(e) => Err(e),
        _ => Err("Unexpected response".into()),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(Mutex::new(AppState {
            camera: Camera::new(),
        }))
        .invoke_handler(tauri::generate_handler![
            cmd_list_ports,
            cmd_connect,
            cmd_disconnect,
            cmd_run_script,
            cmd_stop_script,
            cmd_read_stdout,
            cmd_enable_streaming,
            cmd_poll,
        ])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
