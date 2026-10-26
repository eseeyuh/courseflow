/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// The two demo controls. Both call the real offline backend paths: reset
// rebuilds the initial Northbridge state, sync applies the demo LMS's later
// changes. Neither talks to a live university system.

import { useState } from "react";

import { useDemoState } from "@/components/DemoState";
import { api, ApiError } from "@/lib/api";
import type { WorkflowRun } from "@/lib/types";

type Busy = null | "reset" | "sync";

const EXPLANATION = "This simulates the provided demo LMS changing; it is not a live university Moodle connection.";

/** Short run IDs: a debugging reference for the run traces, nothing more. */
function runRefs(runs: WorkflowRun[]): string {
  return runs.length ? ` (${runs.length === 1 ? "run" : "runs"} ${runs.map((r) => r.id.slice(0, 8)).join(", ")})` : "";
}

export function DemoControls({ compact = false }: { compact?: boolean }) {
  const { refresh } = useDemoState();
  const [busy, setBusy] = useState<Busy>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string; ref: string } | null>(null);

  async function run(kind: Exclude<Busy, null>) {
    setBusy(kind);
    setMessage(null);
    try {
      if (kind === "reset") {
        const result = await api.resetDemo();
        setMessage(result.succeeded
          ? { tone: "ok", text: "Demo reset to the initial course state.", ref: runRefs(result.runs) }
          : { tone: "error", text: "The reset workflow failed; see its run trace.",
              ref: runRefs(result.runs.filter((r) => r.status === "failed")) });
      } else {
        const result = await api.syncDemo();
        setMessage(!result.succeeded
          ? { tone: "error", text: "The sync workflow failed; see its run trace.",
              ref: runRefs(result.runs.filter((r) => r.status === "failed")) }
          : result.changes.length === 0
            ? { tone: "ok", text: "LMS sync found no new changes.", ref: "" }
            : { tone: "ok", text: `LMS sync brought in ${result.changes.length} changed resources.`, ref: runRefs(result.runs) });
      }
      refresh();
    } catch (error) {
      setMessage({ tone: "error", ref: "",
        text: error instanceof ApiError ? error.message : "The request failed. Try again, or reset the demo." });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-1.5">
      <div role="group" aria-label="Demo controls" className="flex flex-wrap items-center gap-2">
        {compact && <span className="text-xs text-muted">Demo controls</span>}
        <button type="button" className="btn btn-quiet" disabled={busy !== null} onClick={() => run("reset")}
          data-testid="reset-demo">
          {busy === "reset" ? "Resetting…" : "Reset demo"}
        </button>
        <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={() => run("sync")}
          data-testid="simulate-sync">
          {busy === "sync" ? "Syncing…" : "Simulate LMS sync"}
        </button>
      </div>
      <p className={`text-xs text-muted ${compact ? "max-w-80" : ""}`} data-testid="demo-explanation">{EXPLANATION}</p>
      <p role="status" aria-live="polite" className={`text-xs text-muted ${compact ? "max-w-80" : ""}`} data-testid="demo-status">
        {busy === "sync" && "Applying the demo LMS's later changes and reprocessing (about 10 seconds)…"}
        {busy === "reset" && "Rebuilding the initial demo state offline (about 10 seconds)…"}
        {!busy && message && (
          <>
            <span className={message.tone === "error" ? "text-conflict" : "text-verified"}>{message.text}</span>
            {message.ref}
          </>
        )}
      </p>
    </div>
  );
}
