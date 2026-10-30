/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { DemoControls } from "@/components/DemoControls";
import { useApi } from "@/components/DemoState";
import { EvidenceButton, fromSource } from "@/components/Evidence";
import { Icon, type IconName } from "@/components/Icon";
import { LoadState } from "@/components/ui";
import { api } from "@/lib/api";
import { courseName, formatInstant, formatLocalDate, RESOURCE_LABELS, shortDate } from "@/lib/format";
import type { Affected, Change, PlanDiff } from "@/lib/types";

export default function ChangesPage() {
  const result = useApi(api.changes, "changes");
  return (
    <div className="space-y-10">
      <header className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="animate-fade-up">
          <h1 className="page-title">Changes</h1>
          <p className="page-lede">
            When a source changes, CourseFlow rechecks only what that change affects, then updates the study plan.
          </p>
        </div>
        <div className="panel p-5">
          <h2 className="section-title">Demo LMS sync</h2>
          <p className="mt-1 text-[14px] text-muted">
            The demo LMS publishes two later updates: a module announcement and a new handbook version.
            Syncing brings them in, and CourseFlow reprocesses only what they change.
          </p>
          <div className="mt-4"><DemoControls /></div>
        </div>
      </header>
      <LoadState result={result}>
        {(syncs) =>
          syncs.length === 0 ? (
            <div className="panel flex max-w-2xl gap-3.5 border-dashed px-5 py-4 text-[15px] text-muted" data-testid="no-changes">
              <Icon name="history" size={18} className="mt-0.5 text-faint" />
              <p>
                No changes yet: every source is at the version CourseFlow first processed. Simulate LMS sync to
                bring in the demo LMS&apos;s later updates.
              </p>
            </div>
          ) : (
            <div className="space-y-14">
              {syncs.map((sync) => (
                <section key={sync.observed_at} data-testid="sync">
                  <h2 className="label mb-5">LMS sync of {formatInstant(sync.observed_at)} (demo clock)</h2>
                  <ol className="relative space-y-6 border-l border-line pl-8">
                    {sync.changes.map((change) => (
                      <TimelineStep key={change.id} icon="file" tone="neutral">
                        <ChangeCard change={change} />
                      </TimelineStep>
                    ))}
                    {sync.plan_diff && (
                      <TimelineStep icon="calendar" tone="accent">
                        <PlanDiffCard diff={sync.plan_diff} />
                      </TimelineStep>
                    )}
                  </ol>
                </section>
              ))}
            </div>
          )
        }
      </LoadState>
    </div>
  );
}

type Side = [string, string | null];

function value(v: Record<string, unknown> | null): string {
  if (!v) return "";
  return formatLocalDate(String(v.date), (v.time as string | null) ?? null);
}

function sides(v: Record<string, unknown> | null): string {
  return ((v?.sides as Side[] | undefined) ?? []).map(([d, t]) => formatLocalDate(d, t)).join(" vs ");
}

function quotes(v: Record<string, unknown> | null): string {
  return ((v?.quotes as string[] | undefined) ?? []).join(" ").replace(/\s+/g, " ").trim();
}

/** Word-level difference of two stored texts (longest common subsequence):
 * marks the words that differ. Display only; both texts are the sources'. */
function wordDiff(before: string, after: string): { before: [string, boolean][]; after: [string, boolean][] } {
  const a = before.split(" ");
  const b = after.split(" ");
  const lcs = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const outA: [string, boolean][] = [];
  const outB: [string, boolean][] = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      outA.push([a[i++], false]);
      outB.push([b[j++], false]);
    } else if (j < b.length && (i === a.length || lcs[i][j + 1] >= lcs[i + 1][j])) {
      outB.push([b[j++], true]);
    } else {
      outA.push([a[i++], true]);
    }
  }
  return { before: outA, after: outB };
}

function Words({ words, mark }: { words: [string, boolean][]; mark: string }) {
  return (
    <>
      {words.map(([w, changed], k) => (
        <span key={k}>{k > 0 && " "}{changed ? <span className={mark}>{w}</span> : w}</span>
      ))}
    </>
  );
}

const LABELS: Record<Affected["kind"], string> = {
  deadline_added: "New deadline",
  deadline_superseded: "Deadline replaced",
  conflict_changed: "Conflict changed",
  obligation_wording_changed: "Obligation reworded",
};

function AffectedBody({ item }: { item: Affected }) {
  switch (item.kind) {
    case "deadline_added":
      return (
        <p>
          {item.assessment_title}: <span className="font-semibold">{value(item.new_value)}</span>
        </p>
      );
    case "deadline_superseded":
      return (
        <p>
          {item.assessment_title}: <span className="line-through decoration-conflict/70">{value(item.old_value)}</span>
          {" replaced by "}<span className="font-semibold">{value(item.new_value)}</span>, because the source says so
        </p>
      );
    case "conflict_changed":
      return (
        <div className="space-y-1">
          <p>{item.assessment_title}: the sources still disagree, on different dates.</p>
          <dl className="grid grid-cols-[3.5rem_1fr] gap-x-3 gap-y-0.5">
            <dt className="text-muted">Before</dt><dd className="text-muted">{sides(item.old_value)}</dd>
            <dt className="text-muted">Now</dt><dd className="font-semibold">{sides(item.new_value)}</dd>
          </dl>
        </div>
      );
    case "obligation_wording_changed": {
      const diff = wordDiff(quotes(item.old_value), quotes(item.new_value));
      return (
        <div className="space-y-2">
          <p>The source rewords an obligation it states. Changed words are marked.</p>
          <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-2">
            <div>
              <dt className="text-xs text-muted">Before</dt>
              <dd className="source-quote text-muted"><Words words={diff.before} mark="rounded-sm bg-conflict-soft px-0.5 text-conflict line-through" /></dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Now</dt>
              <dd className="source-quote"><Words words={diff.after} mark="mark font-semibold" /></dd>
            </div>
          </dl>
        </div>
      );
    }
  }
}

/** One step on a sync's timeline: a node on the rail, then its card. */
function TimelineStep({ icon, tone, children }: { icon: IconName; tone: "neutral" | "accent"; children: ReactNode }) {
  return (
    <li className="relative">
      <span aria-hidden className={`absolute top-4 -left-[45px] grid h-[26px] w-[26px] place-items-center rounded-full ring-4 ring-canvas ${
        tone === "accent" ? "bg-accent text-white" : "bg-paper text-muted"}`}>
        <Icon name={icon} size={13} />
      </span>
      {children}
    </li>
  );
}

function AffectedRow({ item }: { item: Affected }) {
  const label = LABELS[item.kind];
  return (
    <li className="grid gap-x-4 gap-y-2 py-4 text-[14.5px] sm:grid-cols-[10rem_1fr_auto]" data-testid={`affected-${item.kind}`}>
      <span><span className={`badge ${item.kind === "conflict_changed" ? "badge-conflict" : "badge-neutral"}`}>{label}</span></span>
      <div className="min-w-0 text-ink-soft"><AffectedBody item={item} /></div>
      <span>
        <EvidenceButton label="Source text"
          request={{ kind: label, claim: `${label}: ${item.assessment_title ?? "an obligation"}`, items: item.sources.map(fromSource) }} />
      </span>
    </li>
  );
}

function ChangeCard({ change }: { change: Change }) {
  return (
    <article className="panel overflow-hidden" data-testid="change">
      <header className="px-6 pt-4 pb-4">
        <p className="label">Source changed</p>
        <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h3 className="text-[17.5px] font-semibold tracking-tight">{change.resource_title}</h3>
          <span className="text-[14px] text-muted">
            {RESOURCE_LABELS[change.resource_type] ?? change.resource_type} in {courseName(change.course_title)}
          </span>
          <span className="ml-auto rounded-md bg-paper px-2 py-0.5 text-[12.5px] text-ink-soft ring-1 ring-line" data-testid="change-versions">
            {change.old_version ? `version ${change.old_version.version_number} to ${change.new_version.version_number}` : `new, version ${change.new_version.version_number}`}
          </span>
        </div>
      </header>
      <div className="border-t border-line bg-canvas/40 px-6 pt-3.5">
        <p className="eyebrow text-[12.5px] text-link"><Icon name="arrowRight" size={13} />CourseFlow updated what this affects</p>
        <ul className="divide-y divide-line">
          {change.affected.map((item, i) => <AffectedRow key={`${item.kind}-${i}`} item={item} />)}
          {change.affected.length === 0 && <li className="py-3 text-[14.5px] text-muted">No stored claim was affected.</li>}
        </ul>
      </div>
    </article>
  );
}

function PlanDiffCard({ diff }: { diff: PlanDiff }) {
  return (
    <article className="panel border-accent-line px-6 py-5" data-testid="plan-diff">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <p className="label">Study plan changed</p>
          <h3 className="mt-1.5 text-[17.5px] font-semibold tracking-tight">Your study plan, recomputed</h3>
        </div>
        <Link href="/study-plan" className="inline-flex items-center gap-1 text-[14px] font-medium text-link">
          Open study plan <Icon name="arrowRight" size={13} />
        </Link>
      </div>
      <p className="mt-1 text-[14px] text-muted">
        {diff.changed.length} {diff.changed.length === 1 ? "task" : "tasks"} changed, {diff.added.length} added,{" "}
        {diff.removed.length} removed, {diff.unchanged_count} unchanged.
      </p>
      <ul className="mt-4 space-y-3 text-[14.5px]">
        {diff.changed.map((c) => (
          <li key={c.task_key} className="rounded-lg border border-line bg-canvas/50 px-4 py-3" data-testid="plan-diff-change">
            <p className="font-medium">{c.title}</p>
            <ul className="mt-1 space-y-0.5 text-muted">
              {Object.entries(c.fields).map(([field, [before, after]]) => <li key={field}>{describe(field, before, after)}</li>)}
            </ul>
          </li>
        ))}
      </ul>
    </article>
  );
}

function describe(field: string, before: unknown, after: unknown): string {
  if (field === "anchor_at") {
    return `Now planned against ${shortDate(String(after).slice(0, 10))} (was ${shortDate(String(before).slice(0, 10))})`;
  }
  if (field === "conflict_values") {
    const fmt = (v: unknown) => (v as Side[]).map(([d, t]) => formatLocalDate(d, t)).join(" or ");
    return `Stated deadlines now ${fmt(after)} (were ${fmt(before)})`;
  }
  if (field === "position") return `Now step ${String(after)} (was ${String(before)})`;
  return `${field}: ${String(before)} to ${String(after)}`;
}
