mod protocol;

use std::sync::Mutex;
use tauri::ipc;
use tauri::menu::{MenuBuilder, SubmenuBuilder};
use tauri::{Emitter, Manager, State};

use protocol::camera::{list_ports, Camera, Command, Response};

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
    let resp = st.camera.execute(Command::Connect {
        port,
        baudrate: 921600,
    });
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

#[tauri::command]
fn cmd_poll(state: State<Mutex<AppState>>) -> Result<ipc::Response, String> {
    let mut st = state.lock().map_err(|e| e.to_string())?;
    let resp = st.camera.execute(Command::Poll);

    match resp {
        Response::PollResult { stdout, frame } => {
            let stdout_bytes = stdout.unwrap_or_default().into_bytes();
            let mut buf = Vec::with_capacity(stdout_bytes.len() + 256);

            buf.extend_from_slice(&(stdout_bytes.len() as u32).to_le_bytes());
            buf.extend_from_slice(&stdout_bytes);

            if let Some(f) = frame {
                buf.extend_from_slice(&f.width.to_le_bytes());
                buf.extend_from_slice(&f.height.to_le_bytes());
                let fmt = f.format_str.as_bytes();
                buf.push(fmt.len() as u8);
                buf.extend_from_slice(fmt);
                buf.push(f.is_jpeg as u8);
                buf.extend_from_slice(&f.data);
            } else {
                buf.extend_from_slice(&0u32.to_le_bytes());
                buf.extend_from_slice(&0u32.to_le_bytes());
            }

            Ok(ipc::Response::new(buf))
        }
        Response::Error(e) => Err(e),
        _ => Err("Unexpected response".into()),
    }
}

#[tauri::command]
fn cmd_read_file(path: String) -> Result<String, String> {
    std::fs::read_to_string(&path).map_err(|e| format!("{}: {}", path, e))
}

#[tauri::command]
fn cmd_write_file(path: String, content: String) -> Result<(), String> {
    std::fs::write(&path, &content).map_err(|e| format!("{}: {}", path, e))
}

fn build_menu(
    app: &tauri::App,
) -> Result<tauri::menu::Menu<tauri::Wry>, Box<dyn std::error::Error>> {
    // macOS app menu (first submenu becomes the app name menu)
    let app_menu = SubmenuBuilder::new(app, "OpenMV IDE")
        .about(None)
        .separator()
        .text("settings", "Settings...")
        .separator()
        .services()
        .separator()
        .hide()
        .hide_others()
        .show_all()
        .separator()
        .quit()
        .build()?;

    let file = SubmenuBuilder::new(app, "File")
        .text("new", "New")
        .text("open", "Open...")
        .text("open-recent", "Open Recent")
        .separator()
        .text("save", "Save")
        .text("save-as", "Save As...")
        .separator()
        .close_window()
        .build()?;

    let edit = SubmenuBuilder::new(app, "Edit")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .separator()
        .text("find", "Find")
        .text("replace", "Replace")
        .build()?;

    let tools = SubmenuBuilder::new(app, "Tools")
        .text("threshold-editor", "Threshold Editor")
        .text("apriltag-gen", "AprilTag Generator")
        .separator()
        .text("save-image", "Save Image")
        .text("save-template", "Save Template")
        .text("save-descriptor", "Save Descriptor")
        .separator()
        .text("model-zoo", "Model Zoo")
        .text("edge-impulse", "Edge Impulse")
        .separator()
        .text("dataset-editor", "Dataset Editor")
        .text("video-tools", "Video Tools")
        .build()?;

    let device = SubmenuBuilder::new(app, "Device")
        .text("fw-update", "Update Firmware")
        .text("romfs-editor", "ROMFS Editor")
        .separator()
        .text("wifi-settings", "WiFi Settings")
        .text("camera-settings", "Camera Settings")
        .separator()
        .text("reset-device", "Reset Device")
        .text("bootloader", "Enter Bootloader")
        .build()?;

    let view = SubmenuBuilder::new(app, "View")
        .text("zoom-in", "Zoom In")
        .text("zoom-out", "Zoom Out")
        .text("zoom-reset", "Reset Zoom")
        .separator()
        .text("toggle-terminal", "Toggle Terminal")
        .text("toggle-fb", "Toggle Frame Buffer")
        .text("toggle-histogram", "Toggle Histogram")
        .separator()
        .text("settings", "Settings...")
        .build()?;

    let help = SubmenuBuilder::new(app, "Help")
        .text("docs", "Documentation")
        .text("examples", "Examples")
        .separator()
        .text("about", "About OpenMV IDE")
        .build()?;

    let menu = MenuBuilder::new(app)
        .items(&[&app_menu, &file, &edit, &tools, &device, &view, &help])
        .build()?;

    Ok(menu)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
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
            cmd_read_file,
            cmd_write_file,
        ])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let menu = build_menu(app)?;
            app.set_menu(menu)?;

            // Handle menu events -- emit to frontend
            let handle = app.handle().clone();
            app.on_menu_event(move |_app, event| {
                let id = event.id().0.clone();
                if let Some(window) = handle.get_webview_window("main") {
                    let _ = window.emit("menu-action", id);
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
