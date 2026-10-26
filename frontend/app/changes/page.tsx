/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";

import { DemoControls } from "@/components/DemoControls";
import { useApi } from "@/components/DemoState";
import { EvidenceButton, fromSource } from "@/components/Evidence";
import { LoadState } from "@/components/ui";
import { api } from "@/lib/api";
import { courseName, formatInstant, formatLocalDate, RESOURCE_LABELS, shortDate } from "@/lib/format";
import type { Affected, Change, PlanDiff } from "@/lib/types";

export default function ChangesPage() {
  const result = useApi(api.changes, "changes");
  return (
    <div className="space-y-8">
      <header className="space-y-4">
        <h1 className="page-title">Changes</h1>
        <div className="panel max-w-2xl p-5">
          <h2 className="font-semibold">Demo LMS sync</h2>
          <p className="mt-1 text-sm text-muted">
            The demo LMS publishes two later updates: a module announcement and a new handbook version.
            Syncing brings them in, and CourseFlow reprocesses only what they change.
          </p>
          <div className="mt-4"><DemoControls /></div>
        </div>
      </header>
      <LoadState result={result}>
        {(syncs) =>
          syncs.length === 0 ? (
            <p className="panel max-w-2xl border-dashed px-5 py-4 text-muted" data-testid="no-changes">
              No changes yet: every source is at the version CourseFlow first processed. Simulate LMS sync to
              bring in the demo LMS&apos;s later updates.
            </p>
          ) : (
            <div className="space-y-12">
              {syncs.map((sync) => (
                <section key={sync.observed_at} className="space-y-5" data-testid="sync">
                  <h2 className="section-title">LMS sync of {formatInstant(sync.observed_at)} (demo clock)</h2>
                  {sync.changes.map((change) => <ChangeCard key={change.id} change={change} />)}
                  {sync.plan_diff && <PlanDiffCard diff={sync.plan_diff} />}
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

function AffectedRow({ item }: { item: Affected }) {
  const label = LABELS[item.kind];
  return (
    <li className="grid gap-x-4 gap-y-2 py-4 text-sm sm:grid-cols-[10rem_1fr_auto]" data-testid={`affected-${item.kind}`}>
      <span><span className={`badge ${item.kind === "conflict_changed" ? "badge-conflict" : "badge-neutral"}`}>{label}</span></span>
      <div className="min-w-0"><AffectedBody item={item} /></div>
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
      <header className="flex flex-wrap items-center gap-x-4 gap-y-1 bg-paper/60 px-5 py-3">
        <span className="badge border border-line bg-white text-ink">Source changed</span>
        <h3 className="font-semibold">{change.resource_title}</h3>
        <span className="text-sm text-muted">
          {RESOURCE_LABELS[change.resource_type] ?? change.resource_type} in {courseName(change.course_title)}
        </span>
        <span className="ml-auto text-sm" data-testid="change-versions">
          {change.old_version ? `version ${change.old_version.version_number} to ${change.new_version.version_number}` : `new, version ${change.new_version.version_number}`}
        </span>
      </header>
      <div className="border-t border-line px-5 pt-3">
        <p className="text-xs font-medium text-muted">What CourseFlow updated as a result</p>
        <ul className="divide-y divide-line">
          {change.affected.map((item, i) => <AffectedRow key={`${item.kind}-${i}`} item={item} />)}
          {change.affected.length === 0 && <li className="py-3 text-sm text-muted">No stored claim was affected.</li>}
        </ul>
      </div>
    </article>
  );
}

function PlanDiffCard({ diff }: { diff: PlanDiff }) {
  return (
    <article className="panel px-5 py-4" data-testid="plan-diff">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-semibold">Your study plan, recomputed</h3>
        <Link href="/study-plan" className="text-sm text-accent hover:underline">Open study plan</Link>
      </div>
      <p className="mt-1 text-sm text-muted">
        {diff.changed.length} {diff.changed.length === 1 ? "task" : "tasks"} changed, {diff.added.length} added,{" "}
        {diff.removed.length} removed, {diff.unchanged_count} unchanged.
      </p>
      <ul className="mt-3 space-y-3 text-sm">
        {diff.changed.map((c) => (
          <li key={c.task_key} data-testid="plan-diff-change">
            <p className="font-medium">{c.title}</p>
            <ul className="mt-0.5 text-muted">
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
