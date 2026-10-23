/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// The two demo controls. Both call the real offline backend paths: reset
// rebuilds the initial Northbridge state, sync applies the demo LMS's later
// changes. Neither talks to a live university system.

import { useState } from "react";

import { useDemoState } from "@/components/DemoState";
import { api } from "@/lib/api";

type Busy = null | "reset" | "sync";

export function DemoControls({ compact = false }: { compact?: boolean }) {
  const { refresh } = useDemoState();
  const [busy, setBusy] = useState<Busy>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  async function run(kind: Exclude<Busy, null>) {
    setBusy(kind);
    setMessage(null);
    try {
      if (kind === "reset") {
        const result = await api.resetDemo();
        setMessage(result.succeeded
          ? { tone: "ok", text: "Demo reset to the initial course state." }
          : { tone: "error", text: "The reset workflow failed; see its run trace." });
      } else {
        const result = await api.syncDemo();
        setMessage(!result.succeeded
          ? { tone: "error", text: "The sync workflow failed; see its run trace." }
          : result.changes.length === 0
            ? { tone: "ok", text: "LMS sync found no new changes." }
            : { tone: "ok", text: `LMS sync brought in ${result.changes.length} changed resources.` });
      }
      refresh();
    } catch (error) {
      setMessage({ tone: "error", text: error instanceof Error ? error.message : "Request failed." });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className={compact ? "flex flex-wrap items-center gap-2" : "space-y-2"}>
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn btn-quiet" disabled={busy !== null} onClick={() => run("reset")}
          data-testid="reset-demo">
          {busy === "reset" ? "Resetting…" : "Reset demo"}
        </button>
        <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={() => run("sync")}
          data-testid="simulate-sync">
          {busy === "sync" ? "Syncing…" : "Simulate LMS sync"}
        </button>
      </div>
      <p role="status" aria-live="polite" className="text-xs text-muted" data-testid="demo-status">
        {busy === "sync" && "Applying the demo LMS's later changes and reprocessing (about 10 seconds)…"}
        {busy === "reset" && "Rebuilding the initial demo state offline (about 10 seconds)…"}
        {!busy && message && <span className={message.tone === "error" ? "text-conflict" : "text-verified"}>{message.text}</span>}
        {!busy && !message && !compact && "Simulates the provided demo LMS changing its course pages. It does not connect to a live university Moodle."}
      </p>
    </div>
  );
}
