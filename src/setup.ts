/*
 * Copyright (C) 2026 OpenMV, LLC.
 *
 * This software is licensed under terms that can be found in the
 * LICENSE file in the root directory of this software component.
 */
// First-run setup window. Opens when resources (examples, stubs, tools)
// are missing or outdated. Downloads them with progress tracking.

import { listen } from "@tauri-apps/api/event";
import { WebviewWindow } from "@tauri-apps/api/webviewWindow";
import { state } from "./state";

export interface ResourceStatus {
  name: string;
  installed_version: string | null;
  available_version: string | null;
  needs_update: boolean;
}

export async function openSetupWindow(): Promise<void> {
  const scale = state.uiScale;
  const win = new WebviewWindow("setup", {
    url: "setup.html",
    title: "OpenMV Studio Setup",
    width: Math.round(520 * scale),
    height: Math.round(420 * scale),
    resizable: false,
    center: true,
    alwaysOnTop: true,
    parent: "main",
  });

  await new Promise<void>((resolve, reject) => {
    win.once("tauri://created", () => resolve());
    win.once("tauri://error", (e) => reject(e));
  });

  // Wait for the setup window to signal completion or be closed
  await new Promise<void>((resolve) => {
    listen("setup-complete", () => {
      resolve();
    });
    win.once("tauri://destroyed", () => {
      resolve();
    });
  });
}
