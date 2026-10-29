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

import { useApi, useDemoState } from "@/components/DemoState";
import { Icon } from "@/components/Icon";
import { CompactNav } from "@/components/Nav";
import { api } from "@/lib/api";
import { formatInstant } from "@/lib/format";

export const EXPLANATION = "This simulates the provided demo LMS changing; it is not a live university Moodle connection.";

// What each action does on the backend, in order. A description, not a
// progress report: the API answers once the whole action has finished.
const WORKING = {
  sync: {
    title: "CourseFlow is processing the changed LMS sources",
    steps: ["Bring in the new source versions", "Recheck the claims they affect", "Refresh conflicts and the study plan"],
    duration: "This takes about 10 seconds.",
  },
  reset: {
    title: "Rebuilding the initial demo state",
    steps: ["Restore every course to its first version", "Replay the recorded course processing offline", "Rebuild conflicts and the study plan"],
    duration: "This takes about 10 seconds.",
  },
};

function Buttons({ small = false }: { small?: boolean }) {
  const { busy, run } = useDemoState();
  const size = small ? "btn-sm" : "";
  return (
    <>
      <button type="button" className={`btn btn-ghost ${size}`} disabled={busy !== null} onClick={() => run("reset")}
        data-testid="reset-demo">
        {busy === "reset" ? "Resetting…" : "Reset demo"}
      </button>
      <button type="button" className={`btn btn-quiet ${size}`} disabled={busy !== null} onClick={() => run("sync")}
        data-testid="simulate-sync">
        <Icon name="refresh" size={14} className={`text-link ${busy === "sync" ? "motion-safe:animate-spin" : ""}`} />
        {busy === "sync" ? "Syncing…" : "Simulate LMS sync"}
      </button>
    </>
  );
}

/** The status of the last demo action, kept short. */
function Status() {
  const { busy, message } = useDemoState();
  return (
    <span role="status" aria-live="polite" data-testid="demo-status" className="flex items-center gap-1.5 text-[12.5px]">
      {busy && <span aria-hidden className="spinner h-3 w-3" />}
      {busy === "sync" && <span className="text-muted">Syncing the demo LMS…</span>}
      {busy === "reset" && <span className="text-muted">Resetting the demo…</span>}
      {!busy && message && (
        <>
          <Icon name={message.tone === "error" ? "alert" : "checkCircle"} size={13}
            className={message.tone === "error" ? "text-conflict" : "text-verified"} />
          <span className={message.tone === "error" ? "text-conflict" : "text-ink-soft"}>{message.text}</span>
        </>
      )}
    </span>
  );
}

/** When the demo LMS was last synced, from the stored changes. */
function LastSync() {
  const changes = useApi(api.changes, "topbar-changes");
  const latest = changes.data?.at(-1);
  if (changes.data === undefined) return <span className="skeleton inline-block h-2 w-40 align-middle" aria-hidden />;
  return <>{latest ? `Last LMS sync ${formatInstant(latest.observed_at)}` : "No LMS sync yet: sources at their first version"}</>;
}

/** The workspace top bar: context on the left, the demo actions on the
 * right, and under them the plain statement of what the demo is. */
export function TopBar() {
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-canvas/85 backdrop-blur">
      <div className="flex min-h-16 flex-wrap items-center gap-x-6 gap-y-2 px-8 py-2.5 xl:px-12">
        <div className="mr-auto min-w-0">
          <p className="text-[14.5px] leading-tight font-semibold tracking-tight text-ink">Northbridge University</p>
          <p className="mt-0.5 text-[12px] leading-tight text-muted">
            <LastSync />
          </p>
        </div>
        <CompactNav />
        <div role="group" aria-label="Demo controls" className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <Status />
          <span className="flex gap-1.5"><Buttons small /></span>
        </div>
      </div>
      <div className="flex items-center gap-2.5 border-t border-line/60 bg-sidebar/70 px-8 py-1.5 text-[12px] xl:px-12">
        <span className="label text-[10px] text-faint">Prototype</span>
        <span className="text-faint" data-testid="demo-explanation">{EXPLANATION}</span>
      </div>
    </header>
  );
}

/** The controls in a page body (the Changes page). */
export function DemoControls() {
  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-center gap-2"><Buttons /></div>
      <p className="text-[12px] text-faint">{EXPLANATION}</p>
    </div>
  );
}

/** In the page while a demo action runs, then its outcome. The page under
 * it stays as it was until the fresh data arrives. */
export function DemoProgress() {
  const { busy, message, dismiss } = useDemoState();
  const path = usePathname();
  if (busy) {
    const { title, steps, duration } = WORKING[busy];
    return (
      <div className="panel mb-9 overflow-hidden shadow-lift animate-fade-up" data-testid="demo-progress">
        <div className="h-0.5 skeleton rounded-none" aria-hidden />
        <div className="flex items-start gap-4 px-6 py-5">
          <span aria-hidden className="spinner mt-1 h-4 w-4" />
          <div className="min-w-0 flex-1">
            <p className="text-[15px] font-semibold tracking-tight">{title}</p>
            <ol className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1.5 text-[13px] text-muted">
              {steps.map((step, i) => (
                <li key={step} className="flex items-center gap-2">
                  <span className="grid h-5 w-5 place-items-center rounded-full text-[11px] font-semibold text-faint ring-1 ring-line">{i + 1}</span>
                  {step}
                  {i < steps.length - 1 && <Icon name="arrowRight" size={13} className="text-line-strong" />}
                </li>
              ))}
            </ol>
            <p className="mt-3 text-[12px] text-faint">{duration}</p>
          </div>
        </div>
      </div>
    );
  }
  if (!message) return null;
  const error = message.tone === "error";
  return (
    <div className={`panel mb-9 flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3.5 text-[13.5px] animate-fade-up ${
      error ? "border-conflict-line bg-conflict-soft" : "border-verified-line/70"}`}
      data-testid="demo-outcome">
      <Icon name={error ? "alert" : "checkCircle"} size={17} className={error ? "text-conflict" : "text-verified"} />
      <p className="min-w-0 flex-1">
        <span className={`font-medium ${error ? "text-conflict" : "text-ink"}`}>{message.text}</span>
        <span className="text-muted">
          {!error && message.action === "sync" && " Conflicts, deadlines and the study plan now reflect the new sources."}
          {!error && message.action === "reset" && " Every course is back at its first version, with no changes yet."}
        </span>
        {message.ref && <span className="text-[12px] text-faint">{message.ref}</span>}
      </p>
      <span className="flex items-center gap-4">
        {!error && message.action === "sync" && path !== "/changes" && <Link href="/changes" className="text-link">See what changed</Link>}
        <button type="button" onClick={dismiss} className="text-muted hover:text-ink">Dismiss</button>
      </span>
    </div>
  );
}
