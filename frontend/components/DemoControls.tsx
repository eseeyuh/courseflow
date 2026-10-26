/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// The two demo controls. Both call the real offline backend paths: reset
// rebuilds the initial Northbridge state, sync applies the demo LMS's later
// changes. Neither talks to a live university system. Every copy of the
// controls shares one running action (DemoState), so nothing runs twice.

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useDemoState } from "@/components/DemoState";

export const EXPLANATION = "This simulates the provided demo LMS changing; it is not a live university Moodle connection.";

const WORKING = {
  sync: {
    title: "CourseFlow is processing the changed LMS sources",
    body: "Bringing in the new versions, rechecking the claims they affect, then refreshing conflicts and the study plan. This takes about 10 seconds.",
  },
  reset: {
    title: "Rebuilding the initial demo state",
    body: "Replaying the recorded course processing offline. This takes about 10 seconds.",
  },
};

function Buttons({ small = false }: { small?: boolean }) {
  const { busy, run } = useDemoState();
  const size = small ? "px-2.5 py-1 text-xs" : "";
  return (
    <>
      <button type="button" className={`btn btn-quiet ${size}`} disabled={busy !== null} onClick={() => run("reset")}
        data-testid="reset-demo">
        {busy === "reset" ? "Resetting…" : "Reset demo"}
      </button>
      <button type="button" className={`btn btn-primary ${size}`} disabled={busy !== null} onClick={() => run("sync")}
        data-testid="simulate-sync">
        {busy === "sync" ? "Syncing…" : "Simulate LMS sync"}
      </button>
    </>
  );
}

/** The status of the last demo action, kept short. */
function Status() {
  const { busy, message } = useDemoState();
  return (
    <span role="status" aria-live="polite" data-testid="demo-status">
      {busy === "sync" && "Syncing the demo LMS…"}
      {busy === "reset" && "Resetting the demo…"}
      {!busy && message && (
        <>
          <span className={message.tone === "error" ? "text-conflict" : "text-verified"}>{message.text}</span>
        </>
      )}
    </span>
  );
}

/** The slim demo strip under the header: clearly a demo control, not the product. */
export function DemoBar() {
  return (
    <div className="border-t border-line bg-paper/70">
      <div role="group" aria-label="Demo controls"
        className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-2 px-5 py-2 text-xs">
        <span className="badge border border-line bg-white text-muted">Demo</span>
        <span className="text-muted" data-testid="demo-explanation">{EXPLANATION}</span>
        <span className="ml-auto flex flex-wrap items-center gap-3">
          <Status />
          <span className="flex gap-2"><Buttons small /></span>
        </span>
      </div>
    </div>
  );
}

/** The controls in a page body (the Changes page). */
export function DemoControls() {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2"><Buttons /></div>
      <p className="text-xs text-muted">{EXPLANATION}</p>
    </div>
  );
}

/** In the page while a demo action runs, then its outcome. The page under
 * it stays as it was until the fresh data arrives. */
export function DemoProgress() {
  const { busy, message, dismiss } = useDemoState();
  const path = usePathname();
  if (busy) {
    const { title, body } = WORKING[busy];
    return (
      <div className="panel mb-8 flex items-start gap-4 border-accent/40 bg-accent-soft/50 px-5 py-4" data-testid="demo-progress">
        <span aria-hidden className="mt-1 h-4 w-4 shrink-0 rounded-full border-2 border-accent/30 border-t-accent motion-safe:animate-spin" />
        <div>
          <p className="font-medium">{title}</p>
          <p className="mt-0.5 text-sm text-muted">{body}</p>
        </div>
      </div>
    );
  }
  if (!message) return null;
  const error = message.tone === "error";
  return (
    <div className={`panel mb-8 flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3 text-sm ${error ? "border-conflict/40 bg-conflict-soft" : "border-verified/30 bg-verified-soft/60"}`}
      data-testid="demo-outcome">
      <p className={error ? "text-conflict" : "text-verified"}>
        <span className="font-medium">{message.text}</span>
        {!error && message.action === "sync" && " Conflicts, deadlines and the study plan now reflect the new sources."}
        {!error && message.action === "reset" && " Every course is back at its first version, with no changes yet."}
        {message.ref && <span className="text-xs text-muted">{message.ref}</span>}
      </p>
      <span className="ml-auto flex items-center gap-4">
        {!error && message.action === "sync" && path !== "/changes" && <Link href="/changes" className="text-accent hover:underline">See what changed</Link>}
        <button type="button" onClick={dismiss} className="text-muted hover:text-ink">Dismiss</button>
      </span>
    </div>
  );
}
