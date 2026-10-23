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
            <p className="text-muted" data-testid="no-changes">
              No changes yet. Simulate LMS sync to bring in the demo LMS&apos;s later updates.
            </p>
          ) : (
            <div className="space-y-10">
              {syncs.map((sync) => (
                <section key={sync.observed_at} className="space-y-4" data-testid="sync">
                  <h2 className="section-title">Sync of {formatInstant(sync.observed_at)}</h2>
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

function describeAffected(item: Affected): { label: string; body: string } {
  switch (item.kind) {
    case "deadline_added":
      return { label: "Deadline added", body: `${item.assessment_title}: ${value(item.new_value)}` };
    case "deadline_superseded":
      return {
        label: "Deadline superseded",
        body: `${item.assessment_title}: ${value(item.old_value)} replaced by ${value(item.new_value)}, because the source says so`,
      };
    case "conflict_changed":
      return {
        label: "Conflict changed",
        body: `${item.assessment_title}: ${sides(item.old_value)} is now ${sides(item.new_value)}`,
      };
    case "obligation_wording_changed":
      return { label: "Obligation wording changed", body: "The source rewords an obligation it states" };
  }
}

function AffectedRow({ item }: { item: Affected }) {
  const { label, body } = describeAffected(item);
  return (
    <li className="flex flex-wrap items-start gap-3 py-3" data-testid={`affected-${item.kind}`}>
      <span className={`badge ${item.kind === "conflict_changed" ? "badge-conflict" : "badge-neutral"}`}>{label}</span>
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-sm">{body}</p>
        {item.kind === "obligation_wording_changed" && (
          <div className="grid gap-2 text-sm sm:grid-cols-2">
            <p><span className="text-muted">Before: </span><span className="source-quote">{(item.old_value?.quotes as string[] | undefined)?.join(" ")}</span></p>
            <p><span className="text-muted">Now: </span><span className="source-quote mark">{(item.new_value?.quotes as string[] | undefined)?.join(" ")}</span></p>
          </div>
        )}
      </div>
      <EvidenceButton label="Source" request={{ kind: label, claim: body, items: item.sources.map(fromSource) }} />
    </li>
  );
}

function ChangeCard({ change }: { change: Change }) {
  return (
    <article className="panel" data-testid="change">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-b border-line px-5 py-3">
        <h3 className="font-semibold">{change.resource_title}</h3>
        <span className="text-sm text-muted">
          {RESOURCE_LABELS[change.resource_type] ?? change.resource_type} in {courseName(change.course_title)}
        </span>
        <span className="ml-auto text-sm" data-testid="change-versions">
          {change.old_version ? `version ${change.old_version.version_number} to ${change.new_version.version_number}` : `new, version ${change.new_version.version_number}`}
        </span>
      </header>
      <ul className="divide-y divide-line px-5">
        {change.affected.map((item, i) => <AffectedRow key={`${item.kind}-${i}`} item={item} />)}
        {change.affected.length === 0 && <li className="py-3 text-sm text-muted">No stored claim was affected.</li>}
      </ul>
    </article>
  );
}

function PlanDiffCard({ diff }: { diff: PlanDiff }) {
  return (
    <article className="panel px-5 py-4" data-testid="plan-diff">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-semibold">Study plan before and after</h3>
        <Link href="/study-plan" className="text-sm text-accent hover:underline">Open study plan</Link>
      </div>
      <p className="mt-1 text-sm text-muted">
        {diff.changed.length} changed, {diff.added.length} added, {diff.removed.length} removed,{" "}
        {diff.unchanged_count} unchanged (plan of {formatInstant(diff.before_as_of)} compared with {formatInstant(diff.after_as_of)})
      </p>
      <ul className="mt-3 space-y-2 text-sm">
        {diff.changed.map((c) => (
          <li key={c.task_key} data-testid="plan-diff-change">
            <span className="font-medium">{c.title}</span>
            <span className="text-muted">
              {": "}
              {Object.entries(c.fields).map(([field, [before, after]]) => describe(field, before, after)).join("; ")}
            </span>
          </li>
        ))}
      </ul>
    </article>
  );
}

function describe(field: string, before: unknown, after: unknown): string {
  if (field === "anchor_at") {
    return `planned against ${shortDate(String(before).slice(0, 10))}, now ${shortDate(String(after).slice(0, 10))}`;
  }
  if (field === "conflict_values") {
    const fmt = (v: unknown) => (v as Side[]).map(([d, t]) => formatLocalDate(d, t)).join(" vs ");
    return `sides ${fmt(before)}, now ${fmt(after)}`;
  }
  if (field === "position") return `position ${before} to ${after}`;
  return `${field} ${String(before)} to ${String(after)}`;
}
