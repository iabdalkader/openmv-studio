// OpenMV Camera -- high-level API with I/O thread and command queues
// Ported from openmv-python/src/openmv/camera.py

use std::collections::HashMap;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use serde::Serialize;

use crate::protocol::constants::*;
use crate::protocol::transport::{ProtocolError, Transport};

// -- Messages between main thread and I/O thread --

pub enum Command {
    Connect { port: String, baudrate: u32 },
    Disconnect,
    RunScript(String),
    StopScript,
    ReadStdout,
    EnableStreaming(bool),
    ReadFrame,
    /// Combined poll: reads stdout + frame in one I/O thread cycle
    Poll,
    PollStatus,
    GetVersion,
    GetSystemInfo,
    Shutdown,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", content = "data")]
pub enum Response {
    Connected {
        board: String,
        firmware: String,
        port: String,
    },
    Disconnected,
    Ok,
    Stdout(String),
    Frame {
        width: u32,
        height: u32,
        format_str: String,
        data: Vec<u8>,
        is_jpeg: bool,
    },
    Status(HashMap<String, bool>),
    Version {
        protocol: [u8; 3],
        bootloader: [u8; 3],
        firmware: [u8; 3],
    },
    /// Combined poll result with stdout + frame in one response
    PollResult {
        stdout: Option<String>,
        frame: Option<Box<FrameInfo>>,
    },
    SystemInfo(SystemInfo),
    Error(String),
}

#[derive(Debug, Clone, Serialize)]
pub struct FrameInfo {
    pub width: u32,
    pub height: u32,
    pub format_str: String,
    pub data: Vec<u8>,  // raw bytes (JPEG or RGBA)
    pub is_jpeg: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct SystemInfo {
    pub cpu_id: u32,
    pub usb_vid: u16,
    pub usb_pid: u16,
    pub flash_size_kb: u32,
    pub ram_size_kb: u32,
    pub npu_present: bool,
    pub pmu_present: bool,
    pub pmu_eventcnt: u8,
}

#[derive(Debug, Clone)]
struct ChannelInfo {
    name: String,
    #[allow(dead_code)]
    flags: u8,
}

// -- The I/O worker that owns the serial port --

struct IoWorker {
    transport: Transport,
    channels_by_name: HashMap<String, u8>,
    channels_by_id: HashMap<u8, ChannelInfo>,
    caps: Caps,
}

struct Caps {
    crc: bool,
    seq: bool,
    ack: bool,
    events: bool,
    max_payload: usize,
}

impl IoWorker {
    fn send_cmd(&mut self, opcode: Opcode, channel: u8, data: Option<&[u8]>) -> Result<Option<Vec<u8>>, ProtocolError> {
        self.transport.send_packet(opcode, channel, PacketFlags::empty(), data)?;
        match self.transport.recv_packet() {
            Ok(r) => Ok(r),
            Err(ProtocolError::Checksum) | Err(ProtocolError::Sequence) | Err(ProtocolError::Timeout) => {
                log::warn!("Protocol error, resyncing...");
                self.resync()?;
                self.transport.send_packet(opcode, channel, PacketFlags::empty(), data)?;
                self.transport.recv_packet()
            }
            Err(e) => Err(e),
        }
    }

    /// Send command without resync on error -- used for non-critical reads
    fn send_cmd_soft(&mut self, opcode: Opcode, channel: u8, data: Option<&[u8]>) -> Result<Option<Vec<u8>>, ProtocolError> {
        self.transport.send_packet(opcode, channel, PacketFlags::empty(), data)?;
        self.transport.recv_packet()
    }

    fn resync(&mut self) -> Result<(), ProtocolError> {
        // Reset to minimal caps for sync
        self.transport.update_caps(true, true, MIN_PAYLOAD_SIZE);
        self.transport.reset_sequence();

        for attempt in 0..3 {
            self.transport.send_packet(Opcode::ProtoSync, 0, PacketFlags::empty(), None)?;
            match self.transport.recv_packet() {
                Ok(_) => {
                    self.transport.reset_sequence();
                    self.negotiate_caps()?;
                    return Ok(());
                }
                Err(_) if attempt < 2 => continue,
                Err(e) => return Err(e),
            }
        }
        Err(ProtocolError::Timeout)
    }

    fn negotiate_caps(&mut self) -> Result<(), ProtocolError> {
        let payload = self.send_cmd(Opcode::ProtoGetCaps, 0, None)?
            .ok_or(ProtocolError::Timeout)?;
        if payload.len() < 6 {
            return Err(ProtocolError::IoError("Invalid caps payload".into()));
        }
        let dev_max_payload = u16::from_le_bytes([payload[4], payload[5]]) as usize;
        self.caps.max_payload = self.caps.max_payload.min(dev_max_payload);

        let flags: u32 = (self.caps.crc as u32)
            | (self.caps.seq as u32) << 1
            | (self.caps.ack as u32) << 2
            | (self.caps.events as u32) << 3;

        let mut set_caps = vec![0u8; 16];
        set_caps[0..4].copy_from_slice(&flags.to_le_bytes());
        set_caps[4..6].copy_from_slice(&(self.caps.max_payload as u16).to_le_bytes());

        self.send_cmd(Opcode::ProtoSetCaps, 0, Some(&set_caps))?;
        self.transport.update_caps(self.caps.crc, self.caps.seq, self.caps.max_payload);
        Ok(())
    }

    fn update_channels(&mut self) -> Result<(), ProtocolError> {
        let payload = self.send_cmd(Opcode::ChannelList, 0, None)?
            .ok_or(ProtocolError::Timeout)?;

        self.channels_by_id.clear();
        self.channels_by_name.clear();

        let entry_size = 16;
        let count = payload.len() / entry_size;
        for i in 0..count {
            let ofs = i * entry_size;
            let id = payload[ofs];
            let flags = payload[ofs + 1];
            let name_bytes = &payload[ofs + 2..ofs + 16];
            let name_end = name_bytes.iter().position(|&b| b == 0).unwrap_or(14);
            let name = String::from_utf8_lossy(&name_bytes[..name_end]).to_string();

            self.channels_by_name.insert(name.clone(), id);
            self.channels_by_id.insert(id, ChannelInfo { name, flags });
        }
        Ok(())
    }

    fn channel_id(&self, name: &str) -> Result<u8, ProtocolError> {
        self.channels_by_name.get(name).copied()
            .ok_or_else(|| ProtocolError::IoError(format!("Channel '{}' not found", name)))
    }

    fn channel_lock(&mut self, id: u8) -> Result<(), ProtocolError> {
        self.send_cmd(Opcode::ChannelLock, id, None)?;
        Ok(())
    }

    fn channel_unlock(&mut self, id: u8) -> Result<(), ProtocolError> {
        self.send_cmd(Opcode::ChannelUnlock, id, None)?;
        Ok(())
    }

    fn channel_size(&mut self, id: u8) -> Result<u32, ProtocolError> {
        let p = self.send_cmd(Opcode::ChannelSize, id, None)?
            .ok_or(ProtocolError::Timeout)?;
        Ok(u32::from_le_bytes([p[0], p[1], p[2], p[3]]))
    }

    fn channel_read(&mut self, id: u8, offset: u32, length: u32) -> Result<Vec<u8>, ProtocolError> {
        let mut req = vec![0u8; 8];
        req[0..4].copy_from_slice(&offset.to_le_bytes());
        req[4..8].copy_from_slice(&length.to_le_bytes());
        self.send_cmd(Opcode::ChannelRead, id, Some(&req))?
            .ok_or(ProtocolError::Timeout)
    }

    fn channel_write(&mut self, id: u8, data: &[u8]) -> Result<(), ProtocolError> {
        let chunk_size = self.caps.max_payload - 8;
        let mut offset: u32 = 0;
        for chunk in data.chunks(chunk_size) {
            let mut payload = vec![0u8; 8 + chunk.len()];
            payload[0..4].copy_from_slice(&offset.to_le_bytes());
            payload[4..8].copy_from_slice(&(chunk.len() as u32).to_le_bytes());
            payload[8..].copy_from_slice(chunk);
            self.send_cmd(Opcode::ChannelWrite, id, Some(&payload))?;
            offset += chunk.len() as u32;
        }
        Ok(())
    }

    fn channel_ioctl(&mut self, id: u8, cmd: u32, args: &[u32]) -> Result<Option<Vec<u8>>, ProtocolError> {
        let mut payload = vec![0u8; 4 + args.len() * 4];
        payload[0..4].copy_from_slice(&cmd.to_le_bytes());
        for (i, arg) in args.iter().enumerate() {
            payload[4 + i * 4..8 + i * 4].copy_from_slice(&arg.to_le_bytes());
        }
        self.send_cmd(Opcode::ChannelIoctl, id, Some(&payload))
    }

    fn get_version(&mut self) -> Result<Response, ProtocolError> {
        let p = self.send_cmd(Opcode::ProtoVersion, 0, None)?
            .ok_or(ProtocolError::Timeout)?;
        if p.len() < 16 {
            return Err(ProtocolError::IoError("Invalid version payload".into()));
        }
        Ok(Response::Version {
            protocol: [p[0], p[1], p[2]],
            bootloader: [p[3], p[4], p[5]],
            firmware: [p[6], p[7], p[8]],
        })
    }

    fn get_system_info(&mut self) -> Result<Response, ProtocolError> {
        let p = self.send_cmd(Opcode::SysInfo, 0, None)?
            .ok_or(ProtocolError::Timeout)?;
        if p.len() < 76 {
            return Err(ProtocolError::IoError("Invalid sysinfo payload".into()));
        }
        let cpu_id = u32::from_le_bytes([p[0], p[1], p[2], p[3]]);
        let usb_id = u32::from_le_bytes([p[16], p[17], p[18], p[19]]);
        let caps = u32::from_le_bytes([p[40], p[41], p[42], p[43]]);

        Ok(Response::SystemInfo(SystemInfo {
            cpu_id,
            usb_vid: (usb_id >> 16) as u16,
            usb_pid: usb_id as u16,
            flash_size_kb: u32::from_le_bytes([p[48], p[49], p[50], p[51]]),
            ram_size_kb: u32::from_le_bytes([p[52], p[53], p[54], p[55]]),
            npu_present: caps & (1 << 1) != 0,
            pmu_present: caps & (1 << 7) != 0,
            pmu_eventcnt: ((caps >> 8) & 0xFF) as u8,
        }))
    }

    fn exec_script(&mut self, script: &str) -> Result<(), ProtocolError> {
        let id = self.channel_id("stdin")?;
        self.channel_ioctl(id, ioctl::STDIN_RESET, &[])?;
        self.channel_write(id, script.as_bytes())?;
        self.channel_ioctl(id, ioctl::STDIN_EXEC, &[])?;
        Ok(())
    }

    fn stop_script(&mut self) -> Result<(), ProtocolError> {
        let id = self.channel_id("stdin")?;
        self.channel_ioctl(id, ioctl::STDIN_STOP, &[])?;
        Ok(())
    }

    fn read_stdout(&mut self) -> Result<Option<String>, ProtocolError> {
        let id = self.channel_id("stdout")?;
        let size = self.channel_size(id)?;
        if size == 0 {
            return Ok(None);
        }
        let data = self.channel_read(id, 0, size)?;
        Ok(Some(String::from_utf8_lossy(&data).to_string()))
    }

    fn read_stdout_soft(&mut self) -> Result<Option<String>, ProtocolError> {
        let id = self.channel_id("stdout")?;
        let p = self.send_cmd_soft(Opcode::ChannelSize, id, None)?
            .ok_or(ProtocolError::Timeout)?;
        let size = u32::from_le_bytes([p[0], p[1], p[2], p[3]]);
        if size == 0 {
            return Ok(None);
        }
        let mut req = vec![0u8; 8];
        req[0..4].copy_from_slice(&0u32.to_le_bytes());
        req[4..8].copy_from_slice(&size.to_le_bytes());
        let data = self.send_cmd_soft(Opcode::ChannelRead, id, Some(&req))?
            .ok_or(ProtocolError::Timeout)?;
        Ok(Some(String::from_utf8_lossy(&data).to_string()))
    }

    fn enable_streaming(&mut self, enable: bool) -> Result<(), ProtocolError> {
        let id = self.channel_id("stream")?;
        // Set raw=0 (JPEG preferred mode) for much smaller transfers
        self.channel_ioctl(id, ioctl::STREAM_RAW_CTRL, &[0])?;
        self.channel_ioctl(id, ioctl::STREAM_CTRL, &[enable as u32])?;
        Ok(())
    }

    fn read_frame(&mut self) -> Result<Option<FrameInfo>, ProtocolError> {
        let id = self.channel_id("stream")?;

        // Try to lock -- firmware only locks if frame is ready.
        // If not ready, returns NAK/BUSY and we skip.
        match self.send_cmd_soft(Opcode::ChannelLock, id, None) {
            Ok(Some(_)) | Ok(None) => {} // locked OK (ACK or true)
            Err(ProtocolError::Nak(Status::Busy)) => return Ok(None), // not ready
            Err(_) => return Ok(None), // any error, skip
        }

        let result = (|| {
            // Lock succeeded -- frame is ready. Get size and read.
            let p = self.send_cmd_soft(Opcode::ChannelSize, id, None)?
                .ok_or(ProtocolError::Timeout)?;
            let size = u32::from_le_bytes([p[0], p[1], p[2], p[3]]);
            if size <= 20 {
                return Ok(None);
            }
            let mut req = vec![0u8; 8];
            req[0..4].copy_from_slice(&0u32.to_le_bytes());
            req[4..8].copy_from_slice(&size.to_le_bytes());
            let data = self.send_cmd_soft(Opcode::ChannelRead, id, Some(&req))?
                .ok_or(ProtocolError::Timeout)?;
            if data.len() < 20 {
                return Ok(None);
            }
            let width = u32::from_le_bytes([data[0], data[1], data[2], data[3]]);
            let height = u32::from_le_bytes([data[4], data[5], data[6], data[7]]);
            let format = u32::from_le_bytes([data[8], data[9], data[10], data[11]]);
            let offset = u32::from_le_bytes([data[16], data[17], data[18], data[19]]) as usize;

            // Validate header values
            if width == 0 || height == 0 || width > 4096 || height > 4096 || offset >= data.len() {
                log::warn!("Invalid frame header: {}x{} offset={} len={}", width, height, offset, data.len());
                return Ok(None);
            }
            let raw = &data[offset..];

            const PIXFORMAT_GRAYSCALE: u32 = 0x08020001;
            const PIXFORMAT_RGB565: u32    = 0x0C030002;
            const PIXFORMAT_JPEG: u32      = 0x06060000;

            let pixels = (width as usize).saturating_mul(height as usize);

            let frame = match format {
                PIXFORMAT_JPEG => {
                    FrameInfo {
                        width, height,
                        format_str: "JPEG".into(),
                        data: raw.to_vec(),
                        is_jpeg: true,
                    }
                }
                PIXFORMAT_RGB565 => {
                    let mut rgba = vec![255u8; pixels * 4];
                    for i in 0..pixels {
                        if i * 2 + 1 >= raw.len() { break; }
                        let pixel = u16::from_le_bytes([raw[i * 2], raw[i * 2 + 1]]);
                        let r = ((pixel >> 11) & 0x1F) as u32;
                        let g = ((pixel >> 5) & 0x3F) as u32;
                        let b = (pixel & 0x1F) as u32;
                        rgba[i * 4]     = ((r * 255) / 31) as u8;
                        rgba[i * 4 + 1] = ((g * 255) / 63) as u8;
                        rgba[i * 4 + 2] = ((b * 255) / 31) as u8;
                    }
                    FrameInfo {
                        width, height,
                        format_str: "RGB565".into(),
                        data: rgba,
                        is_jpeg: false,
                    }
                }
                PIXFORMAT_GRAYSCALE => {
                    let mut rgba = vec![255u8; pixels * 4];
                    for i in 0..pixels {
                        if i >= raw.len() { break; }
                        let g = raw[i];
                        rgba[i * 4]     = g;
                        rgba[i * 4 + 1] = g;
                        rgba[i * 4 + 2] = g;
                    }
                    FrameInfo {
                        width, height,
                        format_str: "GRAY".into(),
                        data: rgba,
                        is_jpeg: false,
                    }
                }
                _ => {
                    FrameInfo {
                        width, height,
                        format_str: format!("0x{:08X}", format),
                        data: raw.to_vec(),
                        is_jpeg: false,
                    }
                }
            };
            Ok(Some(frame))
        })();

        let _ = self.send_cmd_soft(Opcode::ChannelUnlock, id, None);
        result
    }

    /// Combined poll: read stdout + frame in one I/O thread cycle
    fn poll_all(&mut self) -> Result<Response, ProtocolError> {
        let mut need_resync = false;

        // Poll channel readiness first (like the Python CLI does)
        let status = match self.poll_status_soft() {
            Ok(s) => s,
            Err(_) => { need_resync = true; HashMap::new() }
        };

        // Only read stdout if channel has data
        let stdout = if !need_resync && status.get("stdout").copied().unwrap_or(false) {
            match self.read_stdout_soft() {
                Ok(s) => s,
                Err(_) => { need_resync = true; None }
            }
        } else {
            None
        };

        // Read frame
        let frame = if !need_resync {
            match self.read_frame() {
                Ok(f) => f.map(Box::new),
                Err(e) => {
                    log::warn!("read_frame error: {}", e);
                    need_resync = true;
                    None
                }
            }
        } else {
            None
        };

        if need_resync {
            let _ = self.resync();
        }

        Ok(Response::PollResult { stdout, frame })
    }

    fn poll_status(&mut self) -> Result<HashMap<String, bool>, ProtocolError> {
        let p = self.send_cmd(Opcode::ChannelPoll, 0, None)?
            .ok_or(ProtocolError::Timeout)?;
        let flags = u32::from_le_bytes([p[0], p[1], p[2], p[3]]);

        let mut result = HashMap::new();
        for (name, &id) in &self.channels_by_name {
            result.insert(name.clone(), flags & (1 << id) != 0);
        }
        Ok(result)
    }

    fn poll_status_soft(&mut self) -> Result<HashMap<String, bool>, ProtocolError> {
        let p = self.send_cmd_soft(Opcode::ChannelPoll, 0, None)?
            .ok_or(ProtocolError::Timeout)?;
        let flags = u32::from_le_bytes([p[0], p[1], p[2], p[3]]);

        let mut result = HashMap::new();
        for (name, &id) in &self.channels_by_name {
            result.insert(name.clone(), flags & (1 << id) != 0);
        }
        Ok(result)
    }
}

// -- Public Camera handle (holds the channel ends) --

pub struct Camera {
    cmd_tx: mpsc::Sender<Command>,
    resp_rx: mpsc::Receiver<Response>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Camera {
    pub fn new() -> Self {
        let mut cam = Self {
            cmd_tx: mpsc::channel().0, // placeholder, replaced by ensure_thread
            resp_rx: mpsc::channel().1,
            thread: None,
        };
        cam.ensure_thread();
        cam
    }

    /// Ensure the I/O thread is running. Respawn if dead.
    fn ensure_thread(&mut self) {
        let alive = self.thread.as_ref().map_or(false, |h| !h.is_finished());
        if alive {
            return;
        }

        // Clean up old thread if any
        if let Some(handle) = self.thread.take() {
            let _ = handle.join();
        }

        let (cmd_tx, cmd_rx) = mpsc::channel::<Command>();
        let (resp_tx, resp_rx) = mpsc::channel::<Response>();

        let thread = thread::spawn(move || {
            if let Err(e) = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                io_thread_main(cmd_rx, resp_tx);
            })) {
                log::error!("I/O thread panicked: {:?}", e);
            }
        });

        self.cmd_tx = cmd_tx;
        self.resp_rx = resp_rx;
        self.thread = Some(thread);
    }

    /// Send a command and wait for the response.
    pub fn execute(&mut self, cmd: Command) -> Response {
        self.ensure_thread();
        if self.cmd_tx.send(cmd).is_err() {
            return Response::Error("I/O thread not running".into());
        }
        self.resp_rx.recv().unwrap_or_else(|_| Response::Error("I/O thread died".into()))
    }

    pub fn shutdown(&mut self) {
        let _ = self.cmd_tx.send(Command::Shutdown);
        if let Some(handle) = self.thread.take() {
            let _ = handle.join();
        }
    }
}

impl Drop for Camera {
    fn drop(&mut self) {
        self.shutdown();
    }
}

// -- I/O thread main loop --

fn io_thread_main(cmd_rx: mpsc::Receiver<Command>, resp_tx: mpsc::Sender<Response>) {
    let mut worker: Option<IoWorker> = None;

    loop {
        let cmd = match cmd_rx.recv() {
            Ok(cmd) => cmd,
            Err(_) => break, // Main thread dropped the sender
        };

        let response = match cmd {
            Command::Shutdown => break,

            Command::Connect { port, baudrate } => {
                match do_connect(&port, baudrate) {
                    Ok(w) => {
                        let board = format!("OpenMV (0x{:08X})", 0); // TODO: from sysinfo
                        let fw = "0.0.0".to_string(); // TODO: from version
                        let port_name = port.clone();
                        worker = Some(w);

                        // Get version + sysinfo after connect
                        if let Some(ref mut w) = worker {
                            let fw_str = match w.get_version() {
                                Ok(Response::Version { firmware, .. }) =>
                                    format!("{}.{}.{}", firmware[0], firmware[1], firmware[2]),
                                _ => fw,
                            };
                            let board_str = match w.get_system_info() {
                                Ok(Response::SystemInfo(ref info)) =>
                                    format!("OpenMV (0x{:08X})", info.cpu_id),
                                _ => board,
                            };
                            Response::Connected {
                                board: board_str,
                                firmware: fw_str,
                                port: port_name,
                            }
                        } else {
                            Response::Error("Connect failed".into())
                        }
                    }
                    Err(e) => Response::Error(format!("{}", e)),
                }
            }

            Command::Disconnect => {
                worker = None;
                Response::Disconnected
            }

            Command::RunScript(script) => {
                match worker.as_mut() {
                    Some(w) => match w.exec_script(&script) {
                        Ok(()) => Response::Ok,
                        Err(e) => Response::Error(format!("{}", e)),
                    },
                    None => Response::Error("Not connected".into()),
                }
            }

            Command::StopScript => {
                match worker.as_mut() {
                    Some(w) => match w.stop_script() {
                        Ok(()) => Response::Ok,
                        Err(e) => Response::Error(format!("{}", e)),
                    },
                    None => Response::Error("Not connected".into()),
                }
            }

            Command::ReadStdout => {
                match worker.as_mut() {
                    Some(w) => match w.read_stdout() {
                        Ok(Some(text)) => Response::Stdout(text),
                        Ok(None) => Response::Stdout(String::new()),
                        Err(e) => Response::Error(format!("{}", e)),
                    },
                    None => Response::Error("Not connected".into()),
                }
            }

            Command::EnableStreaming(enable) => {
                match worker.as_mut() {
                    Some(w) => match w.enable_streaming(enable) {
                        Ok(()) => Response::Ok,
                        Err(e) => Response::Error(format!("{}", e)),
                    },
                    None => Response::Error("Not connected".into()),
                }
            }

            Command::ReadFrame => {
                match worker.as_mut() {
                    Some(w) => match w.read_frame() {
                        Ok(Some(f)) => Response::Frame {
                            width: f.width, height: f.height,
                            format_str: f.format_str, data: f.data,
                            is_jpeg: f.is_jpeg,
                        },
                        Ok(None) => Response::Error("No frame".into()),
                        Err(e) => Response::Error(format!("{}", e)),
                    },
                    None => Response::Error("Not connected".into()),
                }
            }

            Command::Poll => {
                match worker.as_mut() {
                    Some(w) => match w.poll_all() {
                        Ok(r) => r,
                        Err(e) => Response::Error(format!("{}", e)),
                    },
                    None => Response::Error("Not connected".into()),
                }
            }

            Command::PollStatus => {
                match worker.as_mut() {
                    Some(w) => match w.poll_status() {
                        Ok(s) => Response::Status(s),
                        Err(e) => Response::Error(format!("{}", e)),
                    },
                    None => Response::Error("Not connected".into()),
                }
            }

            Command::GetVersion => {
                match worker.as_mut() {
                    Some(w) => match w.get_version() {
                        Ok(v) => v,
                        Err(e) => Response::Error(format!("{}", e)),
                    },
                    None => Response::Error("Not connected".into()),
                }
            }

            Command::GetSystemInfo => {
                match worker.as_mut() {
                    Some(w) => match w.get_system_info() {
                        Ok(v) => v,
                        Err(e) => Response::Error(format!("{}", e)),
                    },
                    None => Response::Error("Not connected".into()),
                }
            }
        };

        if resp_tx.send(response).is_err() {
            break; // Main thread dropped the receiver
        }
    }
}

fn do_connect(port: &str, baudrate: u32) -> Result<IoWorker, ProtocolError> {
    let serial = serialport::new(port, baudrate)
        .timeout(Duration::from_secs(1))
        .open()
        .map_err(|e| ProtocolError::IoError(e.to_string()))?;

    let transport = Transport::new(
        serial, true, true, MIN_PAYLOAD_SIZE, Duration::from_secs(1),
    );

    let mut worker = IoWorker {
        transport,
        channels_by_name: HashMap::new(),
        channels_by_id: HashMap::new(),
        caps: Caps {
            crc: true,
            seq: true,
            ack: true,
            events: true,
            max_payload: 4096,
        },
    };

    worker.resync()?;
    worker.update_channels()?;
    Ok(worker)
}

/// List serial ports that match known OpenMV VID/PIDs.
pub fn list_ports() -> Vec<String> {
    let ports = serialport::available_ports().unwrap_or_default();
    ports
        .into_iter()
        .filter(|p| {
            if let serialport::SerialPortType::UsbPort(info) = &p.port_type {
                OPENMV_VID_PID.iter().any(|&(vid, pid)| {
                    info.vid == vid && pid.map_or(true, |p| info.pid == p)
                })
            } else {
                false
            }
        })
        .map(|p| p.port_name)
        .collect()
}
