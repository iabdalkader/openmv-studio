/*
 * Copyright (C) 2026 OpenMV, LLC.
 *
 * This software is licensed under terms that can be found in the
 * LICENSE file in the root directory of this software component.
 */
import { listen } from "@tauri-apps/api/event";
import { WebviewWindow } from "@tauri-apps/api/webviewWindow";
import { state } from "./state";

let pinoutWin: WebviewWindow | null = null;

export async function openPinoutViewer() {
  if (pinoutWin) {
    return;
  }

  const scale = state.uiScale;
  const win = new WebviewWindow("pinout", {
    url: "pinout.html",
    title: "Pinout Viewer",
    width: Math.round(900 * scale),
    height: Math.round(640 * scale),
    resizable: true,
    center: true,
    parent: "main",
  });

  pinoutWin = win;

  try {
    await new Promise<void>((resolve, reject) => {
      win.once("tauri://created", () => resolve());
      win.once("tauri://error", (e) => reject(e));
    });
  } catch (e: any) {
    console.error("Failed to create pinout window:", e);
    pinoutWin = null;
    return;
  }

  const readyUnlisten = await listen("pinout-ready", () => {
    readyUnlisten();
    win.emit("pinout-init", {
      connectedBoard: state.connectedBoard,
      resolvedTheme:
        document.documentElement.getAttribute("data-theme") || "dark",
    });
  });

  win.once("tauri://destroyed", () => {
    pinoutWin = null;
  });
}
