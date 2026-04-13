// DataChannel: a Tauri Channel subclass with an in-flight guard.
// Prevents stacking requests when serial I/O is slow.
// Creates an ephemeral Channel per invoke so Tauri's cleanup
// lifecycle works correctly (each Channel lives exactly one invoke).

import { invoke, Channel } from "@tauri-apps/api/core";

export class DataChannel extends Channel<ArrayBuffer> {
  inFlight = false;

  reset() {
    this.inFlight = false;
  }

  request(cmd: string) {
    if (this.inFlight) {
      return;
    }

    this.inFlight = true;
    const ch = new Channel<ArrayBuffer>();
    ch.onmessage = this.onmessage;
    invoke(cmd, { channel: ch }).finally(() => {
      this.inFlight = false;
    });
  }
}
