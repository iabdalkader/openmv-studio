# OpenMV IDE

Custom IDE for OpenMV cameras built on Tauri 2 + Monaco Editor.

Replaces the legacy Qt Creator-based IDE with a modern, lightweight app.

## Source Tree

```
openmv-ide/
|
|-- index.html              # App HTML layout (sidebar, panels, editor, terminal, framebuffer)
|-- package.json            # npm deps (vite, monaco-editor, @tauri-apps/api)
|-- tsconfig.json           # TypeScript config
|
|-- src/                    # FRONTEND (runs in the webview)
|   |-- main.ts             # Monaco editor, UI event handlers, polling, IPC calls
|   |-- style.css           # Dark theme CSS (from openmv-redesign.html mockup)
|
|-- src-tauri/              # BACKEND (native Rust app)
|   |-- Cargo.toml          # Rust deps: tauri, serialport, bitflags, serde, log
|   |-- build.rs            # Tauri codegen build script
|   |-- tauri.conf.json     # Tauri config (window size, app name, security)
|   |-- capabilities/       # Tauri permission system
|   |-- icons/              # App icons (all platforms)
|   |-- gen/                # Auto-generated schemas (don't touch)
|   |-- src/
|       |-- main.rs         # Entry point (calls lib.rs)
|       |-- lib.rs          # Tauri command handlers (cmd_connect, cmd_poll, etc.)
|       |-- protocol/       # OpenMV Protocol V2 implementation
|           |-- mod.rs      # Module declarations
|           |-- constants.rs # Opcodes, flags (bitflags), status codes, VID/PIDs
|           |-- crc.rs      # CRC-16 (poly 0xF94F) + CRC-32 (poly 0xFA567D89)
|           |-- buffer.rs   # Ring buffer for packet parsing
|           |-- transport.rs # State machine (SYNC/HEADER/PAYLOAD), TX/RX, fragmentation
|           |-- camera.rs   # I/O thread + command/response queues, high-level Camera API
```

## Architecture

### Two halves

**Frontend** (`src/`) -- HTML/CSS/TypeScript running in native OS webview
(WebKit on macOS, WebView2 on Windows, WebKitGTK on Linux). Monaco editor,
canvas framebuffer, serial terminal, all UI.

**Backend** (`src-tauri/src/`) -- Native Rust. Serial port, protocol, file I/O.

### I/O Thread + Command Queues

```
Frontend (JS)              Tauri IPC                I/O Thread (Rust)
                                                    (owns serial port)
invoke('cmd_poll') ----->  cmd_tx.send(Poll) -----> poll_all() {
                                                      read_stdout_soft()
                                                      read_frame()
                                                    }
                <---------  resp_rx.recv()  <------- Response::PollResult { stdout, frame }
```

- Dedicated I/O thread owns the serial port
- Main thread sends commands via mpsc channel
- I/O thread sends responses back via mpsc channel
- No shared mutable state -- just message passing
- Thread auto-respawns if it panics

### Protocol V2 Summary

- Serial USB at 921600 baud
- Packet: SYNC(0xD5AA) + Header(8B) + CRC-16 + Payload(0-4096B) + CRC-32
- Flags: ACK, NAK, RTX, ACK_REQ, FRAGMENT, EVENT
- Capability negotiation (CRC, SEQ, ACK, EVENTS)
- Channel-based: stdin (script), stdout (text), stream (framebuffer)
- Channel ops: LIST, POLL, LOCK, UNLOCK, SIZE, READ, WRITE, IOCTL
- JPEG-preferred streaming mode for bandwidth efficiency

### Binary IPC for Frames

`cmd_poll` returns `tauri::ipc::Response` (raw binary, not JSON) to avoid
serializing large frame data as JSON arrays. Format:

```
[stdout_len: u32 LE] [stdout_bytes]
[width: u32 LE] [height: u32 LE]
[format_len: u8] [format_str] [is_jpeg: u8] [frame_data...]
```

If no frame: width=0, height=0.

## Current Status (Phase 1)

### Working
- [x] Connect/disconnect to OpenMV cameras (USB serial, auto-detect by VID/PID)
- [x] Run/stop Python scripts on camera
- [x] Serial terminal (stdout from camera)
- [x] Framebuffer viewer (JPEG and RGB565 formats)
- [x] Protocol V2: sync, caps negotiation, channels, fragmentation, ACK/NAK
- [x] I/O thread with command/response queues
- [x] Auto-resync on protocol errors
- [x] Monaco editor with Python syntax highlighting + dark theme
- [x] Resizable panels (editor/terminal, main/right panel, FB/histogram)
- [x] Sidebar panels (Files, Examples, Docs, Settings -- mockups)
- [x] Keyboard shortcuts (Cmd+R run/stop, Cmd+=/- zoom, F5/F6)
- [x] Status bar (connection state, cursor position, FPS)
- [x] Histogram UI (mockup, not wired to data yet)

### Known Issues
- Protocol loses sync occasionally when frame content changes rapidly
  (timeouts on frame read, causes resync which freezes briefly)
- Frame data sent as Vec<u8> in JSON for non-poll commands (slow);
  cmd_poll uses binary IPC which is fast
- No file management yet (open/save/new)
- Side panels are mockups only (not functional)
- Menus not implemented (should use Tauri native menu API for macOS)
- No high-DPI scaling option yet

### Phase 2 (Next)
- [ ] File management (new, open, save, save-as via Tauri file dialogs)
- [ ] Wire histogram to actual frame data
- [ ] Native macOS/Windows menu bar via Tauri Menu API
- [ ] Actual file tree (camera storage + local files)
- [ ] Actual examples browser (load from scripts/examples/)

### Phase 3+
- [ ] Firmware update (DFU, IMX, Alif bootloaders)
- [ ] ROMFS editor
- [ ] Machine vision tools (threshold editor, AprilTag generator)
- [ ] Model zoo + Edge Impulse integration
- [ ] Profiler
- [ ] Dataset editor
- [ ] Video recording

## Development

```bash
# Prerequisites
brew install rust          # Rust toolchain
cargo install tauri-cli    # Tauri CLI (first time only)
npm install                # Frontend deps (first time or after clean)

# Run
cargo tauri dev            # Dev mode with hot-reload

# Build distributable
cargo tauri build          # Produces DMG (macOS), MSI (Windows), AppImage (Linux)

# Clean
cd src-tauri && cargo clean  # Rust build cache
rm -rf dist                  # Vite frontend output
```

## Dependencies

- **Rust** (brew install rust) -- backend
- **Node.js + npm** -- frontend build tool (Vite) and Monaco editor
- **Tauri CLI** (cargo install tauri-cli) -- app bundler
- **serialport** (Rust crate) -- USB serial communication
- **bitflags** (Rust crate) -- protocol flag types
- **monaco-editor** (npm) -- code editor
- **@tauri-apps/api** (npm) -- frontend-to-backend IPC
