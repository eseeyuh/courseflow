/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";

import { useDemoState, type Loaded } from "@/components/DemoState";
import { EvidenceButton, fromEvidence } from "@/components/Evidence";
import { api, ApiError } from "@/lib/api";
import { formatInstant, formatLocalDate, RESOURCE_LABELS } from "@/lib/format";
import type { Conflict, Deadline, DependencyEdge, StudyState, WorkflowRun } from "@/lib/types";

/** The course's latest course-intelligence run, as its trace records it. The
 * run ID is a small reference for debugging, not something to act on. */
export function RunStatus({ run }: { run: WorkflowRun | null }) {
  if (!run) return <span className="text-muted">Not processed yet</span>;
  const when = run.finished_at ?? run.started_at;
  const label = { succeeded: "Processed", failed: "Processing failed", running: "Processing", pending: "Queued" }[run.status];
  return (
    <span className={run.status === "failed" ? "text-conflict" : undefined} data-testid="run-status">
      {label}{when ? ` ${formatInstant(when)}` : ""}
      <span className="text-muted"> (run {run.id.slice(0, 8)})</span>
    </span>
  );
}

/** Renders loaded data, a loading line, or an error with what to do. */
export function LoadState<T>({ result, children }: { result: Loaded<T>; children: (data: T) => ReactNode }) {
  if (result.data !== undefined) {
    return (
      <>
        {result.state === "error" && <ErrorNote message={result.message} />}
        {children(result.data)}
      </>
    );
  }
  if (result.state === "error") return <ErrorNote message={result.message} />;
  return (
    <p className="flex items-center gap-3 text-sm text-muted" role="status">
      <span aria-hidden className="h-3.5 w-3.5 rounded-full border-2 border-accent/25 border-t-accent motion-safe:animate-spin" />
      Loading from the CourseFlow API…
    </p>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div role="alert" className="panel max-w-2xl border-conflict/40 bg-conflict-soft px-5 py-4 text-sm" data-testid="load-error">
      <p className="font-medium text-conflict">CourseFlow could not load this page.</p>
      <p className="mt-1 text-ink/80">{message}</p>
      <p className="mt-2 text-muted">
        Check that the CourseFlow API is running (scripts/demo-api.sh), then use Reset demo or reload the page.
      </p>
    </div>
  );
}

export function deadlineText(d: Pick<Deadline, "due_date" | "due_time">): string {
  return formatLocalDate(d.due_date, d.due_time);
}

/** Which sources state a deadline: their own titles, as stored. */
export function sourcesOf(d: Deadline): string {
  return [...new Set(d.evidence.map((e) => RESOURCE_LABELS[e.resource_type] ?? e.resource_title))].join(" and ");
}

export function DeadlineEvidence({ deadline, claim, label = "Deadline source" }: { deadline: Deadline; claim: string; label?: string }) {
  return (
    <EvidenceButton
      label={label}
      request={{ kind: "Deadline evidence", claim: `${claim}: ${deadlineText(deadline)}`, items: deadline.evidence.map(fromEvidence) }}
    />
  );
}

/** Every side of a conflict, each with its own source, side by side and
 * styled alike. No side is marked as the right one, and none comes first by
 * date: the order is the API's. */
export function ConflictCard({ conflict, link = false }: { conflict: Conflict; link?: boolean }) {
  return (
    <section className="panel overflow-hidden border-conflict/50" data-testid="conflict-card" aria-label={`${conflict.assessment_title} deadline conflict`}>
      <div className="border-b border-conflict/20 bg-conflict-soft px-5 py-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="font-semibold text-conflict">
            The sources give different deadlines for {conflict.assessment_title}
          </h3>
          {link && (
            <Link href={`/assessments/${conflict.assessment_id}`} className="text-sm text-accent hover:underline">
              Open assessment
            </Link>
          )}
        </div>
        <p className="mt-1 font-medium" data-testid="conflict-trust">
          CourseFlow will not silently choose between conflicting university sources.
        </p>
      </div>
      <div className="grid gap-px bg-line sm:grid-cols-2">
        {conflict.sides.map((side, i) => {
          const name = `Source ${String.fromCharCode(65 + i)}`;
          return (
            <div key={side.id} className="flex flex-col bg-white px-5 py-4" data-testid="conflict-side">
              <p className="text-sm text-muted">{name}: {sourcesOf(side)}</p>
              <p className="mt-1 text-2xl font-semibold tracking-tight" data-testid="conflict-value">{deadlineText(side)}</p>
              <p className="mt-1 text-sm text-muted">
                {side.evidence[0]?.resource_title}, version {side.evidence[0]?.version_number}
              </p>
              <div className="mt-3">
                <EvidenceButton
                  label={`Show what ${name} says`}
                  request={{ kind: "Deadline evidence", claim: `${name} states the ${conflict.assessment_title} deadline as ${deadlineText(side)}`,
                    items: side.evidence.map(fromEvidence) }}
                />
              </div>
            </div>
          );
        })}
      </div>
      <p className="border-t border-conflict/20 px-5 py-3 text-sm text-muted">
        Check the deadline with the module team. The study plan schedules work against the earlier date
        only to be safe; that is not a ruling on which source is right.
      </p>
    </section>
  );
}

const END_KIND: Record<DependencyEdge["prerequisite"]["kind"], string> = {
  assessment: "Assessment",
  obligation: "Obligation",
  milestone: "Milestone",
};

/** Prerequisite, relation, dependent: one row per stored edge. */
export function DependencyChain({ edges }: { edges: DependencyEdge[] }) {
  if (edges.length === 0) return <p className="text-sm text-muted">No dependencies are stated in the sources.</p>;
  return (
    <ul className="space-y-3">
      {edges.map((edge) => {
        const relation = edge.relation === "required_for" ? "required for" : "before";
        return (
          <li key={edge.id} data-testid="dependency"
            className="panel grid items-center gap-x-4 gap-y-2 px-5 py-3 md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto]">
            <div className="min-w-0">
              <p className="text-xs text-muted">{END_KIND[edge.prerequisite.kind]}</p>
              <p className="font-medium">{edge.prerequisite.label}</p>
            </div>
            <span className="badge badge-neutral justify-self-start">
              {relation}
              <svg aria-hidden viewBox="0 0 16 16" className="ml-1 h-3 w-3"><path d="M3 8h9m-3-3 3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.6" /></svg>
            </span>
            <div className="min-w-0">
              <p className="text-xs text-muted">{END_KIND[edge.dependent.kind]}</p>
              <p className="font-medium">{edge.dependent.label}</p>
            </div>
            <span className="justify-self-start md:justify-self-end">
              <EvidenceButton
                label="Source"
                request={{
                  kind: "Dependency evidence",
                  claim: `${edge.prerequisite.label} ${edge.relation === "required_for" ? "is required for" : "comes before"} ${edge.dependent.label}`,
                  items: edge.evidence.filter((e) => e.current_version).map(fromEvidence),
                }}
              />
            </span>
          </li>
        );
      })}
    </ul>
  );
}

const STATES: { value: StudyState; label: string }[] = [
  { value: "not_started", label: "Not started" },
  { value: "studying", label: "Studying" },
  { value: "studied", label: "Studied" },
];

/** Sets the learner's state on every target of a study task (real PUT). */
export function StudyStateControl({ spanIds, state, label, onSaved }: {
  spanIds: string[];
  state: StudyState;
  label: string;
  /** Called after the API accepted the change, before the plan reloads. */
  onSaved?: (state: StudyState) => void;
}) {
  const { refresh } = useDemoState();
  const [chosen, setChosen] = useState<StudyState | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Saving until the reloaded plan shows the chosen state: nothing is shown
  // as changed before the API has accepted it and the plan reflects it.
  const pending = chosen !== null && chosen !== state ? chosen : null;
  async function choose(value: StudyState) {
    setChosen(value);
    setError(null);
    try {
      await api.setStudyState(spanIds, value);
      onSaved?.(value);
      refresh({ soft: true });
    } catch (e) {
      setChosen(null);
      setError(e instanceof ApiError ? e.message : "Could not save. Try again.");
    }
  }
  return (
    <div>
      <div role="group" aria-label={`Study state: ${label}`} className="inline-flex overflow-hidden rounded-md border border-line">
        {STATES.map((s) => (
          <button key={s.value} type="button" aria-pressed={state === s.value} disabled={pending !== null}
            onClick={() => choose(s.value)} data-testid={`state-${s.value}`}
            className={`px-2.5 py-1 text-xs ${state === s.value ? "bg-accent text-white" : "bg-white text-muted hover:bg-paper"} border-l border-line first:border-l-0`}>
            {pending === s.value ? "Saving…" : s.label}
          </button>
        ))}
      </div>
      {error && <p className="mt-1 text-xs text-conflict">{error}</p>}
    </div>
  );
}
