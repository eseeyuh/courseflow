/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";

import { useDemoState, type Loaded } from "@/components/DemoState";
import { EvidenceButton, fromEvidence } from "@/components/Evidence";
import { api } from "@/lib/api";
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
  return <p className="text-sm text-muted" role="status">Loading from the CourseFlow API…</p>;
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <p role="alert" className="panel border-conflict/40 bg-conflict-soft px-4 py-3 text-sm text-conflict">
      {message} Start the API (scripts/demo-api.sh) and use Reset demo to load the demo courses.
    </p>
  );
}

export function deadlineText(d: Pick<Deadline, "due_date" | "due_time">): string {
  return formatLocalDate(d.due_date, d.due_time);
}

/** Which sources state a deadline: their own titles, as stored. */
export function sourcesOf(d: Deadline): string {
  return [...new Set(d.evidence.map((e) => RESOURCE_LABELS[e.resource_type] ?? e.resource_title))].join(" and ");
}

export function DeadlineEvidence({ deadline, claim }: { deadline: Deadline; claim: string }) {
  return (
    <EvidenceButton
      label="Source"
      request={{ kind: "Deadline evidence", claim: `${claim}: ${deadlineText(deadline)}`, items: deadline.evidence.map(fromEvidence) }}
    />
  );
}

/** Every side of a conflict, each with its own source. No side is marked
 * as the right one. */
export function ConflictCard({ conflict, link = false }: { conflict: Conflict; link?: boolean }) {
  return (
    <section className="panel border-conflict/50" data-testid="conflict-card" aria-label={`${conflict.assessment_title} deadline conflict`}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-conflict/20 bg-conflict-soft px-5 py-3">
        <h3 className="font-semibold text-conflict">
          The sources give different deadlines for {conflict.assessment_title}
        </h3>
        {link && (
          <Link href={`/assessments/${conflict.assessment_id}`} className="text-sm text-accent hover:underline">
            Open assessment
          </Link>
        )}
      </div>
      <div className="grid gap-px bg-line sm:grid-cols-2">
        {conflict.sides.map((side, i) => (
          <div key={side.id} className="bg-white px-5 py-4" data-testid="conflict-side">
            <p className="text-sm text-muted">Source {String.fromCharCode(65 + i)}: {sourcesOf(side)}</p>
            <p className="mt-1 text-xl font-semibold" data-testid="conflict-value">{deadlineText(side)}</p>
            <p className="mt-1 text-sm text-muted">
              {side.evidence[0]?.resource_title}, version {side.evidence[0]?.version_number}
            </p>
            <div className="mt-3">
              <DeadlineEvidence deadline={side} claim={`Source ${String.fromCharCode(65 + i)}`} />
            </div>
          </div>
        ))}
      </div>
      <p className="px-5 py-3 text-sm font-medium">
        CourseFlow will not silently choose between conflicting university sources.
      </p>
      <p className="px-5 pb-4 text-xs text-muted">
        Check the deadline with the module team. The study plan schedules work against the earlier
        date only to be safe; that is not a ruling on which source is right.
      </p>
    </section>
  );
}

/** Prerequisite, relation, dependent: one line per stored edge. */
export function DependencyChain({ edges }: { edges: DependencyEdge[] }) {
  if (edges.length === 0) return <p className="text-sm text-muted">No dependencies are stated in the sources.</p>;
  return (
    <ul className="space-y-3">
      {edges.map((edge) => (
        <li key={edge.id} className="panel flex flex-wrap items-center gap-3 px-4 py-3" data-testid="dependency">
          <span className="font-medium">{edge.prerequisite.label}</span>
          <span className="badge badge-neutral">{edge.relation === "required_for" ? "required for" : "before"}</span>
          <span className="font-medium">{edge.dependent.label}</span>
          <span className="ml-auto">
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
      ))}
    </ul>
  );
}

const STATES: { value: StudyState; label: string }[] = [
  { value: "not_started", label: "Not started" },
  { value: "studying", label: "Studying" },
  { value: "studied", label: "Studied" },
];

/** Sets the learner's state on every target of a study task (real PUT). */
export function StudyStateControl({ spanIds, state, label }: { spanIds: string[]; state: StudyState; label: string }) {
  const { refresh } = useDemoState();
  const [pending, setPending] = useState<StudyState | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function choose(value: StudyState) {
    setPending(value);
    setError(null);
    try {
      await api.setStudyState(spanIds, value);
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save.");
    } finally {
      setPending(null);
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
