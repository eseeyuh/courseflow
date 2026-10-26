/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// One shared data version: every view refetches from the API when a demo
// action (reset, sync, a study-state change) changes the backend state. The
// demo actions themselves live here too, so every set of controls and the
// processing banner see the same running action.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import { api, ApiError } from "@/lib/api";
import type { WorkflowRun } from "@/lib/types";

export type DemoAction = "reset" | "sync";
export type DemoMessage = { action: DemoAction; tone: "ok" | "error"; text: string; ref: string };

type DemoState = {
  version: number;
  /** The latest refresh left every row ID in place (a study-state change). */
  soft: boolean;
  /** Reload every view. ``soft``: no row was recreated, so views may keep
   * showing their current data until the new data arrives. */
  refresh: (options?: { soft?: boolean }) => void;
  busy: DemoAction | null;
  message: DemoMessage | null;
  run: (action: DemoAction) => Promise<void>;
  dismiss: () => void;
};

const Context = createContext<DemoState>({
  version: 0,
  soft: false,
  refresh: () => {},
  busy: null,
  message: null,
  run: async () => {},
  dismiss: () => {},
});

/** Short run IDs: a debugging reference for the run traces, nothing more. */
function runRefs(runs: WorkflowRun[]): string {
  return runs.length ? ` (${runs.length === 1 ? "run" : "runs"} ${runs.map((r) => r.id.slice(0, 8)).join(", ")})` : "";
}

export function DemoStateProvider({ children }: { children: ReactNode }) {
  const [{ version, soft }, setVersion] = useState({ version: 0, soft: false });
  const [busy, setBusy] = useState<DemoAction | null>(null);
  const [message, setMessage] = useState<DemoMessage | null>(null);
  const refresh = useCallback(
    (options?: { soft?: boolean }) => setVersion((v) => ({ version: v.version + 1, soft: options?.soft ?? false })),
    [],
  );

  const run = useCallback(async (action: DemoAction) => {
    setBusy(action);
    setMessage(null);
    try {
      if (action === "reset") {
        const result = await api.resetDemo();
        setMessage(result.succeeded
          ? { action, tone: "ok", text: "Demo reset to the initial course state.", ref: runRefs(result.runs) }
          : { action, tone: "error", text: "The reset workflow failed; see its run trace.",
              ref: runRefs(result.runs.filter((r) => r.status === "failed")) });
      } else {
        const result = await api.syncDemo();
        setMessage(!result.succeeded
          ? { action, tone: "error", text: "The sync workflow failed; see its run trace.",
              ref: runRefs(result.runs.filter((r) => r.status === "failed")) }
          : result.changes.length === 0
            ? { action, tone: "ok", text: "LMS sync found no new changes.", ref: "" }
            : { action, tone: "ok", text: `LMS sync brought in ${result.changes.length} changed resources.`, ref: runRefs(result.runs) });
      }
      refresh();
    } catch (error) {
      setMessage({ action, tone: "error", ref: "",
        text: error instanceof ApiError ? error.message : "The request failed. Try again, or reset the demo." });
    } finally {
      setBusy(null);
    }
  }, [refresh]);

  const dismiss = useCallback(() => setMessage(null), []);
  return <Context.Provider value={{ version, soft, refresh, busy, message, run, dismiss }}>{children}</Context.Provider>;
}

export function useDemoState(): DemoState {
  return useContext(Context);
}

export type Loaded<T> =
  | { state: "loading"; data?: T }
  | { state: "ready"; data: T }
  | { state: "error"; message: string; data?: T };

/** Loads ``load()`` from the API, again whenever the shared data version or
 * ``key`` changes. */
export function useApi<T>(load: (() => Promise<T>) | null, key: string): Loaded<T> {
  const { version, soft } = useDemoState();
  const tag = `${key}#${version}`;
  const [result, setResult] = useState<{ tag: string; value: Loaded<T> }>({ tag: "", value: { state: "loading" } });
  useEffect(() => {
    if (load === null) return;
    let live = true;
    load()
      .then((data) => live && setResult({ tag, value: { state: "ready", data } }))
      .catch((error: unknown) =>
        live && setResult((r) => ({
          tag,
          value: {
            state: "error",
            // Our own API errors are written for people; anything else stays in the console.
            message: error instanceof ApiError ? error.message : "The course data could not be read.",
            data: r.value.data,
          },
        })),
      );
    return () => {
      live = false;
    };
    // load is recreated each render; the tag says when to reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tag]);
  if (result.tag === tag) return result.value;
  // A newer request is in flight. After a reset or sync never show the old
  // data meanwhile: a reset recreates the demo rows, so stale links would
  // point at deleted IDs. A soft refresh keeps every ID, so the old data
  // stays on screen until the new data replaces it.
  return soft && result.tag.startsWith(`${key}#`) ? { state: "loading", data: result.value.data } : { state: "loading" };
}
