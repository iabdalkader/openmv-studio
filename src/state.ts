/*
 * Copyright (C) 2026 OpenMV, LLC.
 *
 * This software is licensed under terms that can be found in the
 * LICENSE file in the root directory of this software component.
 */
// Global application state shared across all UI modules.
// Single source of truth for connection status, UI preferences, etc.

import { currentMonitor, primaryMonitor } from "@tauri-apps/api/window";

export type ThemeSetting = "dark" | "light" | "system";

// Compute a window size from a fixed pixel default. The default is scaled
// by the user's UI scale and clamped so the window never opens larger than
// the primary monitor. Used by all child/dialog windows for consistency.
async function sizedWindow(
  defaultWidth: number,
  defaultHeight: number,
): Promise<{ width: number; height: number }> {
  const scale = state.uiScale;
  let width = Math.round(defaultWidth * scale);
  let height = Math.round(defaultHeight * scale);
  const m = (await primaryMonitor()) || (await currentMonitor());
  if (m) {
    const sf = m.scaleFactor || 1;
    const maxW = Math.round((m.size.width / sf) * 0.9);
    const maxH = Math.round((m.size.height / sf) * 0.9);
    width = Math.min(width, maxW);
    height = Math.min(height, maxH);
  }
  return { width, height };
}

// Tool windows: ROMFS editor, training, pinout, etc.
export async function childWindowSize(): Promise<{
  width: number;
  height: number;
}> {
  return sizedWindow(720, 540);
}

// Dialog windows: settings, resource setup/update, erase-filesystem progress.
export async function dialogWindowSize(): Promise<{
  width: number;
  height: number;
}> {
  return sizedWindow(540, 432);
}

export const state = {
  isConnected: false,
  scriptRunning: false,
  connectedBoard: null as string | null,
  connectedSensor: null as string | null,
  uiScale: 1.0,
  ioIntervalMs: 10,
  filterExamples: true,
  canvasVisible: false,
  splitLocked: false,
  showLog: false,
  currentThemeSetting: "dark" as ThemeSetting,
  serialPort: "" as string,
  transportType: "serial" as "serial" | "udp",
  networkAddress: "openmv.local:5555" as string,
  resourceChannel: "stable" as "stable" | "development",
};

// Callback slot -- settings.ts fills this during init.
// Other modules call it without importing settings.ts (no circular dep).
export let scheduleSaveSettings: () => void = () => {};

export function setScheduleSaveSettings(fn: () => void) {
  scheduleSaveSettings = fn;
}
