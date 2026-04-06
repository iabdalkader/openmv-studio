import * as monaco from 'monaco-editor';
import { invoke } from '@tauri-apps/api/core';

// Monaco workers (required for language features)
self.MonacoEnvironment = {
  getWorker(_: string, label: string) {
    if (label === 'json') {
      return new Worker(new URL('monaco-editor/esm/vs/language/json/json.worker.js', import.meta.url), { type: 'module' });
    }
    if (label === 'typescript' || label === 'javascript') {
      return new Worker(new URL('monaco-editor/esm/vs/language/typescript/ts.worker.js', import.meta.url), { type: 'module' });
    }
    return new Worker(new URL('monaco-editor/esm/vs/editor/editor.worker.js', import.meta.url), { type: 'module' });
  }
};

// OpenMV dark theme
monaco.editor.defineTheme('openmv-dark', {
  base: 'vs-dark',
  inherit: true,
  rules: [
    { token: 'comment', foreground: '546e7a', fontStyle: 'italic' },
    { token: 'keyword', foreground: 'c792ea' },
    { token: 'string', foreground: 'c3e88d' },
    { token: 'number', foreground: 'f78c6c' },
    { token: 'identifier', foreground: 'e8e6e3' },
    { token: 'type', foreground: 'ffcb6b' },
    { token: 'delimiter', foreground: '89ddff' },
  ],
  colors: {
    'editor.background': '#1e1e23',
    'editor.foreground': '#e8e6e3',
    'editor.lineHighlightBackground': '#5b9cf510',
    'editor.selectionBackground': '#5b9cf540',
    'editorLineNumber.foreground': '#4a4845',
    'editorLineNumber.activeForeground': '#6b6966',
    'editorCursor.foreground': '#5b9cf5',
    'scrollbarSlider.background': '#ffffff14',
    'scrollbarSlider.hoverBackground': '#ffffff1f',
  }
});

// Create editor
const editor = monaco.editor.create(document.getElementById('monaco-editor')!, {
  value: [
    '# Untitled - OpenMV IDE',
    '',
    'import csi',
    'import time',
    '',
    'csi0 = csi.CSI()',
    'csi0.reset()',
    'csi0.pixformat(csi.RGB565)',
    'csi0.framesize(csi.QVGA)',
    '',
    'clock = time.clock()',
    '',
    'while True:',
    '    clock.tick()',
    '    img = csi0.snapshot()',
    '    print(clock.fps())',
    '',
  ].join('\n'),
  language: 'python',
  theme: 'openmv-dark',
  fontSize: 13,
  fontFamily: "'SF Mono', 'Menlo', 'Consolas', monospace",
  minimap: { enabled: false },
  scrollBeyondLastLine: false,
  renderLineHighlight: 'line',
  automaticLayout: true,
  padding: { top: 8, bottom: 8 },
  glyphMargin: false,
  folding: true,
  cursorBlinking: 'smooth',
  smoothScrolling: true,
  tabSize: 4,
  insertSpaces: true,
});

// Cursor position in status bar
editor.onDidChangeCursorPosition((e) => {
  const el = document.getElementById('status-cursor');
  if (el) el.textContent = `Ln ${e.position.lineNumber}, Col ${e.position.column}`;
});

// Histogram toggle
document.getElementById('hist-toggle')?.addEventListener('click', function() {
  this.classList.toggle('open');
  document.getElementById('hist-body')?.classList.toggle('open');
});

// Sidebar nav buttons (Files, Examples, Docs, Settings -- not Connect/Run)
const sidePanel = document.getElementById('side-panel')!;
let activePanelName: string | null = null;

document.querySelectorAll('.sidebar-btn[data-panel]').forEach(btn => {
  btn.addEventListener('click', () => {
    const panel = (btn as HTMLElement).dataset.panel!;

    if (activePanelName === panel) {
      // Toggle off -- hide panel
      btn.classList.remove('active');
      sidePanel.classList.remove('visible');
      layout.style.gridTemplateColumns = '56px 0px 1fr 4px 40%';
      activePanelName = null;
    } else {
      // Switch panel
      document.querySelectorAll('.sidebar-btn[data-panel]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.querySelectorAll('.side-panel-content').forEach(p => (p as HTMLElement).style.display = 'none');
      const content = sidePanel.querySelector(`[data-panel="${panel}"]`) as HTMLElement;
      if (content) content.style.display = '';
      sidePanel.classList.add('visible');
      layout.style.gridTemplateColumns = '56px 220px 1fr 4px 40%';
      activePanelName = panel;
    }
  });
});

// -- Resize handles --

const layout = document.querySelector('.ide-layout') as HTMLElement;
const mainArea = document.querySelector('.main-area') as HTMLElement;

// Horizontal: between main area and right panel
setupResize('resize-h', 'col', (delta) => {
  const rp = document.querySelector('.right-panel') as HTMLElement;
  const w = Math.max(200, Math.min(800, rp.getBoundingClientRect().width - delta));
  const spW = sidePanel.classList.contains('visible') ? '220px' : '0px';
  layout.style.gridTemplateColumns = `56px ${spW} 1fr 4px ${w}px`;
});

// Vertical: between editor and terminal
setupResize('resize-v', 'row', (delta) => {
  const tp = document.querySelector('.terminal-panel') as HTMLElement;
  const h = Math.max(60, Math.min(600, tp.getBoundingClientRect().height - delta));
  mainArea.style.gridTemplateRows = `1fr 4px ${h}px`;
});

// Vertical: between framebuffer and histogram
{
  const handle = document.getElementById('resize-fb-hist');
  if (handle) {
    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      handle.classList.add('active');
      const fb = document.querySelector('.fb-section') as HTMLElement;
      const hist = document.querySelector('.histogram-section') as HTMLElement;
      const startY = e.clientY;
      const startFbH = fb.getBoundingClientRect().height;
      const startHistH = hist.getBoundingClientRect().height;
      const totalH = startFbH + startHistH;

      const onMove = (e: MouseEvent) => {
        const delta = e.clientY - startY;
        const fbH = Math.max(80, Math.min(totalH - 80, startFbH + delta));
        fb.style.flex = 'none';
        hist.style.flex = 'none';
        fb.style.height = fbH + 'px';
        hist.style.height = (totalH - fbH) + 'px';
      };
      const onUp = () => {
        handle.classList.remove('active');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }
}

function setupResize(handleId: string, axis: 'col' | 'row', onDelta: (delta: number) => void) {
  const handle = document.getElementById(handleId);
  if (!handle) return;
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    handle.classList.add('active');
    const startPos = axis === 'col' ? e.clientX : e.clientY;
    let lastPos = startPos;
    const onMove = (e: MouseEvent) => {
      const pos = axis === 'col' ? e.clientX : e.clientY;
      onDelta(pos - lastPos);
      lastPos = pos;
    };
    const onUp = () => {
      handle.classList.remove('active');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

// -- Terminal output helper --
function termLog(text: string, cls: string = '') {
  const el = document.getElementById('terminal-output');
  if (!el) return;
  const div = document.createElement('div');
  if (cls) div.className = cls;
  div.textContent = text;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
}

// Clear terminal
document.getElementById('btn-clear-term')?.addEventListener('click', () => {
  const el = document.getElementById('terminal-output');
  if (el) el.innerHTML = '';
});

function setConnected(connected: boolean, info: string = 'Disconnected') {
  const dot = document.querySelector('.status-dot') as HTMLElement;
  const label = document.getElementById('status-board');
  const btnConnect = document.getElementById('btn-connect');
  if (dot) dot.className = 'status-dot ' + (connected ? 'connected' : 'disconnected');
  if (label) label.textContent = info;
  if (btnConnect) {
    btnConnect.classList.toggle('connected', connected);
    const lbl = btnConnect.querySelector('span');
    if (lbl) lbl.textContent = connected ? 'Disconnect' : 'Connect';
  }
}

// -- Connection state --
let isConnected = false;

// -- Connect/Disconnect button --
document.getElementById('btn-connect')?.addEventListener('click', async () => {
  if (isConnected) {
    try {
      isConnected = false;
      stopPolling();
      await new Promise(r => setTimeout(r, 100));
      await invoke('cmd_disconnect');
      setConnected(false);
      termLog('Disconnected.', 'info-line');
    } catch (e: any) {
      termLog(`Disconnect failed: ${e}`, 'error-line');
    }
    return;
  }

  try {
    termLog('Scanning for OpenMV cameras...', 'info-line');
    const ports = await invoke<string[]>('cmd_list_ports');

    if (ports.length === 0) {
      termLog('No OpenMV cameras found.', 'error-line');
      return;
    }

    termLog(`Found: ${ports.join(', ')}`, 'info-line');
    termLog(`Connecting to ${ports[0]}...`, 'info-line');

    const resp = await invoke<any>('cmd_connect', { port: ports[0] });
    isConnected = true;
    const info = resp.data;
    termLog(`Connected: ${info.board} (fw ${info.firmware})`, 'info-line');
    setConnected(true, `${info.board} | ${info.port} | v${info.firmware}`);
    try { await invoke('cmd_enable_streaming', { enable: true }); } catch(_) {}
    startPolling();
  } catch (e: any) {
    termLog(`Connection failed: ${e}`, 'error-line');
  }
});

// -- Run/Stop toggle --
let scriptRunning = false;
const btnRunStop = document.getElementById('btn-run-stop')!;
const iconPlay = btnRunStop.querySelector('.icon-play') as SVGElement;
const iconStop = btnRunStop.querySelector('.icon-stop') as SVGElement;
const runStopLabel = btnRunStop.querySelector('.run-stop-label') as HTMLElement;

function updateRunStopButton() {
  if (scriptRunning) {
    btnRunStop.title = 'Stop (Cmd+R)';
    iconPlay.style.display = 'none';
    iconStop.style.display = '';
    if (runStopLabel) runStopLabel.textContent = 'Stop';
  } else {
    btnRunStop.title = 'Run (Cmd+R)';
    iconPlay.style.display = '';
    iconStop.style.display = 'none';
    if (runStopLabel) runStopLabel.textContent = 'Run';
  }
}

async function runScript() {
  try {
    const script = editor.getValue();
    termLog('Running script...', 'info-line');
    await invoke('cmd_run_script', { script });
    termLog('Script started.', 'info-line');
    await invoke('cmd_enable_streaming', { enable: true });
    scriptRunning = true;
    updateRunStopButton();
    startPolling();
  } catch (e: any) {
    termLog(`Run failed: ${e}`, 'error-line');
  }
}

async function stopScript() {
  stopPolling();
  await new Promise(r => setTimeout(r, 200));
  try {
    await invoke('cmd_enable_streaming', { enable: false });
    await invoke('cmd_stop_script');
    termLog('Script stopped.', 'info-line');
    scriptRunning = false;
    updateRunStopButton();
  } catch (e: any) {
    termLog(`Stop failed: ${e}`, 'error-line');
  }
}

async function toggleRunStop() {
  if (scriptRunning) {
    await stopScript();
  } else {
    await runScript();
  }
}

btnRunStop.addEventListener('click', toggleRunStop);

// -- Unified polling (stdout + frame in one call) --
let pollTimer: number | null = null;
let pollInFlight = false;
const fpsTimestamps: number[] = [];

const fbCanvas = document.getElementById('framebuffer-canvas') as HTMLCanvasElement;
const fbNoImage = document.querySelector('.no-image') as HTMLElement;
const fbResolution = document.getElementById('fb-resolution')!;
const fbFormat = document.getElementById('fb-format')!;
const statusFps = document.getElementById('status-fps')!;

function startPolling() {
  stopPolling();
  pollTimer = window.setInterval(doPoll, 50);
}

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function doPoll() {
  if (pollInFlight || !isConnected) return;
  pollInFlight = true;
  try {
    const raw = await invoke<ArrayBuffer>('cmd_poll');
    const buf = new DataView(raw);
    let pos = 0;

    // Parse stdout: [len:u32 LE] [bytes]
    const stdoutLen = buf.getUint32(pos, true); pos += 4;
    if (stdoutLen > 0) {
      const stdoutBytes = new Uint8Array(raw, pos, stdoutLen);
      const text = new TextDecoder().decode(stdoutBytes);
      for (const line of text.split('\n')) {
        if (line.length > 0) termLog(line, 'fps-line');
      }
    }
    pos += stdoutLen;

    // Parse frame: [width:u32] [height:u32] ...
    if (pos + 8 > buf.byteLength) return;
    const width = buf.getUint32(pos, true); pos += 4;
    const height = buf.getUint32(pos, true); pos += 4;

    if (width > 0 && height > 0) {
      const fmtLen = buf.getUint8(pos); pos += 1;
      const fmtBytes = new Uint8Array(raw, pos, fmtLen);
      const formatStr = new TextDecoder().decode(fmtBytes); pos += fmtLen;
      const isJpeg = buf.getUint8(pos) !== 0; pos += 1;
      const frameData = new Uint8Array(raw, pos);

      fbResolution.textContent = `${width} x ${height}`;
      fbFormat.textContent = formatStr;

      const now = performance.now();
      fpsTimestamps.push(now);
      while (fpsTimestamps.length > 0 && now - fpsTimestamps[0] > 1000) {
        fpsTimestamps.shift();
      }
      statusFps.textContent = fpsTimestamps.length.toString();

      if (isJpeg) {
        const blob = new Blob([frameData], { type: 'image/jpeg' });
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
          fbCanvas.width = img.width;
          fbCanvas.height = img.height;
          const ctx = fbCanvas.getContext('2d')!;
          ctx.drawImage(img, 0, 0);
          showCanvas();
          URL.revokeObjectURL(url);
        };
        img.src = url;
      } else {
        fbCanvas.width = width;
        fbCanvas.height = height;
        const ctx = fbCanvas.getContext('2d')!;
        const imageData = new ImageData(new Uint8ClampedArray(frameData.buffer, frameData.byteOffset, frameData.byteLength), width, height);
        ctx.putImageData(imageData, 0, 0);
        showCanvas();
      }
    }
  } catch (e) {
    console.error('poll error:', e);
  } finally {
    pollInFlight = false;
  }
}

function showCanvas() {
  fbCanvas.style.display = 'block';
  fbCanvas.style.maxWidth = '100%';
  fbCanvas.style.maxHeight = '100%';
  fbCanvas.style.objectFit = 'contain';
  fbNoImage.style.display = 'none';
}

// -- Zoom: Cmd+= / Cmd+- for editor and terminal --
let terminalFontSize = 12;
const termContent = document.querySelector('.terminal-content') as HTMLElement;

document.addEventListener('keydown', (e) => {
  // Run/Stop
  if (e.key === 'F5' || e.key === 'F6' || (e.metaKey && e.key === 'r')) {
    e.preventDefault();
    toggleRunStop();
    return;
  }

  // Zoom in/out
  if (e.metaKey && (e.key === '=' || e.key === '+')) {
    e.preventDefault();
    const sz = editor.getOption(monaco.editor.EditorOption.fontSize);
    editor.updateOptions({ fontSize: sz + 1 });
    terminalFontSize = Math.min(32, terminalFontSize + 1);
    termContent.style.fontSize = terminalFontSize + 'px';
  }
  if (e.metaKey && e.key === '-') {
    e.preventDefault();
    const sz = editor.getOption(monaco.editor.EditorOption.fontSize);
    editor.updateOptions({ fontSize: Math.max(8, sz - 1) });
    terminalFontSize = Math.max(8, terminalFontSize - 1);
    termContent.style.fontSize = terminalFontSize + 'px';
  }
  if (e.metaKey && e.key === '0') {
    e.preventDefault();
    editor.updateOptions({ fontSize: 13 });
    terminalFontSize = 12;
    termContent.style.fontSize = terminalFontSize + 'px';
  }
});
