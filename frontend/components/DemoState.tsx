/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// One shared data version: every view refetches from the API when a demo
// action (reset, sync, a study-state change) changes the backend state.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

type DemoState = { version: number; refresh: () => void };

const Context = createContext<DemoState>({ version: 0, refresh: () => {} });

export function DemoStateProvider({ children }: { children: ReactNode }) {
  const [version, setVersion] = useState(0);
  const refresh = useCallback(() => setVersion((v) => v + 1), []);
  return <Context.Provider value={{ version, refresh }}>{children}</Context.Provider>;
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
  const { version } = useDemoState();
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
          value: { state: "error", message: error instanceof Error ? error.message : "Request failed", data: r.value.data },
        })),
      );
    return () => {
      live = false;
    };
    // load is recreated each render; the tag says when to reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tag]);
  // A newer request is in flight. Never show the old data meanwhile: a reset
  // recreates the demo rows, so stale links would point at deleted IDs.
  return result.tag === tag ? result.value : { state: "loading" };
}
