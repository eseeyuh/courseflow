/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";

import { useDemoState, type Loaded } from "@/components/DemoState";
import { EvidenceButton, fromEvidence } from "@/components/Evidence";
import { Icon } from "@/components/Icon";
import { api, ApiError } from "@/lib/api";
import { formatInstant, formatLocalDate, RESOURCE_LABELS } from "@/lib/format";
import type { Conflict, Course, Deadline, DependencyEdge, StudyState, WorkflowRun } from "@/lib/types";

export type CourseTone = { dot: string; bar: string; text: string; edge: string };

// Full class names, so Tailwind sees every one of them.
const TONES: CourseTone[] = [
  { dot: "bg-course-1", bar: "bg-course-1/80", text: "text-course-1", edge: "border-l-course-1/70" },
  { dot: "bg-course-2", bar: "bg-course-2/80", text: "text-course-2", edge: "border-l-course-2/70" },
  { dot: "bg-course-3", bar: "bg-course-3/80", text: "text-course-3", edge: "border-l-course-3/70" },
  { dot: "bg-course-4", bar: "bg-course-4/80", text: "text-course-4", edge: "border-l-course-4/70" },
  { dot: "bg-course-5", bar: "bg-course-5/80", text: "text-course-5", edge: "border-l-course-5/70" },
];
// Presentation only: a fixed colour for each demo course code, so the four
// demo courses read as distinct. Any other course gets one from its title.
const BY_CODE: Record<string, number> = { AML7001: 0, DVS7003: 1, AIF7002: 2, MSD7000: 3 };

/** A course's identity colour. Keyed on its title (whose code prefix names
 * the course), not its row ID, so a demo reset keeps every course's colour. */
export function courseTone(course: Pick<Course, "title">): CourseTone {
  const fixed = BY_CODE[course.title.split(" ")[0]];
  if (fixed !== undefined) return TONES[fixed];
  let hash = 0;
  for (const ch of course.title) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return TONES[hash % TONES.length];
}

export function CourseDot({ course, size = "h-2 w-2" }: { course: Pick<Course, "title">; size?: string }) {
  return <span aria-hidden className={`${size} shrink-0 rounded-[3px] ${courseTone(course).dot}`} />;
}

/** The course's latest course-intelligence run, as its trace records it. The
 * run ID is a small reference for debugging, not something to act on. */
export function RunStatus({ run, testId = "run-status" }: { run: WorkflowRun | null; testId?: string }) {
  if (!run) return <span className="text-muted">Not processed yet</span>;
  const when = run.finished_at ?? run.started_at;
  const label = { succeeded: "Processed", failed: "Processing failed", running: "Processing", pending: "Queued" }[run.status];
  const dot = { succeeded: "bg-verified", failed: "bg-conflict", running: "bg-link", pending: "bg-faint" }[run.status];
  return (
    <span className={`inline-flex flex-wrap items-center gap-x-1.5 ${run.status === "failed" ? "text-conflict" : ""}`} data-testid={testId}>
      <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}{when ? ` ${formatInstant(when)}` : ""}
      <span className="text-faint"> (run {run.id.slice(0, 8)})</span>
    </span>
  );
}

/** A quiet placeholder in the shape of a page: title, then cards. */
function PageSkeleton() {
  return (
    <div aria-hidden className="space-y-6">
      <div className="space-y-3">
        <span className="skeleton block h-9 w-80 max-w-full" />
        <span className="skeleton block h-3 w-56" />
      </div>
      <div className="grid gap-5 lg:grid-cols-3">
        <div className="panel space-y-3 p-6 lg:col-span-2">
          <span className="skeleton block h-3 w-28" />
          <span className="skeleton block h-5 w-72 max-w-full" />
          <span className="skeleton block h-3 w-96 max-w-full" />
        </div>
        <div className="panel space-y-3 p-6">
          <span className="skeleton block h-3 w-24" />
          <span className="skeleton block h-3 w-40" />
        </div>
      </div>
      <div className="panel space-y-3 p-6">
        <span className="skeleton block h-3 w-full" />
        <span className="skeleton block h-3 w-4/5" />
      </div>
    </div>
  );
}

/** Renders loaded data, a loading placeholder, or an error with what to do. */
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
    <div role="status" className="space-y-5">
      <p className="flex items-center gap-2.5 text-[14px] text-muted">
        <span aria-hidden className="spinner h-3.5 w-3.5" />
        Loading from the CourseFlow API…
      </p>
      <PageSkeleton />
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div role="alert" className="panel mb-6 flex max-w-2xl gap-3.5 border-conflict-line bg-conflict-soft px-5 py-4 text-[14.5px]" data-testid="load-error">
      <Icon name="alert" size={18} className="mt-0.5 text-conflict" />
      <div>
        <p className="font-medium text-conflict">CourseFlow could not load this page.</p>
        <p className="mt-1 text-ink-soft">{message}</p>
        <p className="mt-2 text-muted">
          Check that the CourseFlow API is running (scripts/demo-api.sh), then use Reset demo or reload the page.
        </p>
      </div>
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
    <section className="panel overflow-hidden border-conflict-line" data-testid="conflict-card" aria-label={`${conflict.assessment_title} deadline conflict`}>
      <div className="h-1 bg-conflict/60" aria-hidden />
      <div className="flex items-start gap-3.5 px-6 pt-5">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-conflict-soft text-conflict ring-1 ring-conflict-line">
          <Icon name="alert" size={17} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="eyebrow text-conflict">Conflicting deadline detected</p>
            {link && (
              <Link href={`/assessments/${conflict.assessment_id}`} className="inline-flex items-center gap-1 text-[14px] font-medium text-link">
                Open assessment <Icon name="arrowRight" size={13} />
              </Link>
            )}
          </div>
          <h3 className="mt-1 text-[19px] font-semibold tracking-tight">
            The sources give different deadlines for {conflict.assessment_title}
          </h3>
          <p className="mt-1 text-[15px] font-medium text-ink-soft" data-testid="conflict-trust">
            CourseFlow will not silently choose between conflicting university sources.
          </p>
        </div>
      </div>
      <div className="grid gap-3 px-6 pt-5 sm:grid-cols-2">
        {conflict.sides.map((side, i) => {
          const name = `Source ${String.fromCharCode(65 + i)}`;
          return (
            <div key={side.id} className="flex flex-col rounded-lg border border-line bg-canvas/50 px-5 py-4" data-testid="conflict-side">
              <p className="label">{name}</p>
              <p className="mt-1 text-[14px] text-muted">{sourcesOf(side)}</p>
              <p className="mt-2 text-[26px] font-semibold tracking-tight tabular-nums" data-testid="conflict-value">{deadlineText(side)}</p>
              <p className="mt-1 text-[13px] text-faint">
                {side.evidence[0]?.resource_title}, version {side.evidence[0]?.version_number}
              </p>
              <div className="mt-4">
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
      <p className="px-6 pt-4 pb-5 text-[14px] text-muted">
        <span className="font-medium text-ink-soft">Check the deadline with the module team.</span> The study plan schedules
        work against the earlier date only to be safe; that is not a ruling on which source is right.
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
  if (edges.length === 0) return <p className="text-[14.5px] text-muted">No dependencies are stated in the sources.</p>;
  return (
    <ul className="space-y-2.5">
      {edges.map((edge) => {
        const relation = edge.relation === "required_for" ? "required for" : "before";
        return (
          <li key={edge.id} data-testid="dependency"
            className="panel grid items-center gap-x-3 gap-y-2 p-3 md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto]">
            <div className="min-w-0 rounded-lg border border-line bg-canvas/50 px-3.5 py-2.5">
              <p className="label text-[10.5px]">{END_KIND[edge.prerequisite.kind]}</p>
              <p className="mt-0.5 text-[14.5px] font-medium">{edge.prerequisite.label}</p>
            </div>
            <span className="inline-flex items-center gap-1 justify-self-start px-1 text-[12.5px] font-medium text-muted md:justify-self-center">
              {relation}
              <Icon name="arrowRight" size={14} className="text-faint" />
            </span>
            <div className="min-w-0 rounded-lg border border-line bg-canvas/50 px-3.5 py-2.5">
              <p className="label text-[10.5px]">{END_KIND[edge.dependent.kind]}</p>
              <p className="mt-0.5 text-[14.5px] font-medium">{edge.dependent.label}</p>
            </div>
            <span className="justify-self-start px-1 md:justify-self-end">
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
      <div role="group" aria-label={`Study state: ${label}`} className="inline-flex rounded-lg border border-line bg-canvas/60 p-0.5">
        {STATES.map((s) => {
          const on = state === s.value;
          const tone = on ? (s.value === "studied" ? "bg-verified-soft text-verified ring-1 ring-verified-line"
            : s.value === "studying" ? "bg-review-soft text-review ring-1 ring-review-line" : "bg-paper text-ink ring-1 ring-line") : "text-muted hover:text-ink";
          return (
            <button key={s.value} type="button" aria-pressed={on} disabled={pending !== null}
              onClick={() => choose(s.value)} data-testid={`state-${s.value}`}
              className={`rounded-md px-2.5 py-1 text-[12.5px] font-medium transition-colors disabled:cursor-wait ${tone}`}>
              {pending === s.value ? "Saving…" : s.label}
            </button>
          );
        })}
      </div>
      {error && <p className="mt-1 text-[12.5px] text-conflict">{error}</p>}
    </div>
  );
}
