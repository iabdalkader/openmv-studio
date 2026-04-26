/*
 * Copyright (C) 2026 OpenMV, LLC.
 *
 * This software is licensed under terms that can be found in the
 * LICENSE file in the root directory of this software component.
 */
// Resource management window. Opens on first run when resources are missing,
// or when updates are available. Downloads them with progress tracking.

import { listen } from "@tauri-apps/api/event";
import { WebviewWindow } from "@tauri-apps/api/webviewWindow";
import { state } from "./state";

export interface ResourceStatus {
  name: string;
  installed_version: string | null;
  available_version: string | null;
  needs_update: boolean;
}

export async function openResourceWindow(
  mode: "setup" | "update" = "setup",
): Promise<void> {
  const scale = state.uiScale;
  const title =
    mode === "update" ? "Resource Updates" : "OpenMV Studio Setup";
  const win = new WebviewWindow("resources", {
    url: `resources.html?mode=${mode}`,
    title,
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

  // Wait for the window to signal completion or be closed
  await new Promise<void>((resolve) => {
    listen("setup-complete", () => {
      resolve();
    });
    win.once("tauri://destroyed", () => {
      resolve();
    });
  });
}
