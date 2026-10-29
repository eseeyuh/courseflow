/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import { useState } from "react";

import { useApi } from "@/components/DemoState";
import { EvidenceButton, fromEvidence, fromTarget } from "@/components/Evidence";
import { Icon } from "@/components/Icon";
import { CourseDot, courseTone, LoadState, StudyStateControl, deadlineText } from "@/components/ui";
import { api } from "@/lib/api";
import { courseName, dateParts, daysUntil, formatInstant, TASK_LABELS } from "@/lib/format";
import { prerequisitesOf } from "@/lib/load";
import type { StudyPlan, StudyState, StudyTask } from "@/lib/types";

type Saved = { taskKey: string; state: StudyState };

const STATE_LABELS: Record<StudyState, string> = { not_started: "Not started", studying: "Studying", studied: "Studied" };

export default function StudyPlanPage() {
  const result = useApi(api.studyPlan, "study-plan");
  const [saved, setSaved] = useState<Saved | null>(null);
  return (
    <div className="space-y-8">
      <header className="animate-fade-up">
        <h1 className="page-title">Study Plan</h1>
        <p className="page-lede">
          Ordered across your courses: what has to happen first, then the nearest deadline, mandatory
          before recommended. Every reason comes from the course sources.
        </p>
      </header>
      <LoadState result={result}>
        {(plan) => {
          const courses = [...new Map(plan.tasks.map((t) => [t.course.id, t.course])).values()];
          return (
            <>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-y border-line py-3 text-[13px]">
                <p className="text-muted" data-testid="plan-as-of">As of {formatInstant(plan.as_of)} (demo clock).</p>
                <span className="flex flex-wrap items-center gap-x-4 gap-y-1">
                  {courses.map((c) => {
                    const open = plan.tasks.filter((t) => t.course.id === c.id && t.status !== "completed").length;
                    return (
                      <span key={c.id} className="inline-flex items-center gap-2 text-ink-soft">
                        <CourseDot course={c} />
                        {courseName(c.title)}: <span className="font-medium text-ink tabular-nums">{open} open</span>
                      </span>
                    );
                  })}
                </span>
              </div>
              {saved && <SavedNotice saved={saved} plan={plan} onDismiss={() => setSaved(null)} />}
              <div className="space-y-10">
                {groupByDay(plan.tasks).map((group) => (
                  <section key={group.id} aria-label={groupLabel(group)} data-testid="plan-day" data-date={group.date ?? group.kind}
                    className="grid gap-x-10 gap-y-4 md:grid-cols-[190px_minmax(0,1fr)]">
                    <DayHeading group={group} asOf={plan.as_of} />
                    <ol className="space-y-3">
                      {group.tasks.map((task) => (
                        <TaskRow key={task.task_key} task={task} plan={plan} highlight={saved?.taskKey === task.task_key}
                          onSaved={(state) => setSaved({ taskKey: task.task_key, state })} />
                      ))}
                    </ol>
                  </section>
                ))}
              </div>
            </>
          );
        }}
      </LoadState>
    </div>
  );
}

type DayGroup = { id: string; kind: "date" | "undated" | "done"; date: string | null; tasks: StudyTask[] };

/** The stored local date a task is planned against: its stated deadline, or
 * the earliest one when sources disagree (both stay visible on the task). */
function taskDate(task: StudyTask): string | null {
  const dates = [...task.conflict_values, ...task.deadlines].map((d) => d.due_date).sort();
  return dates[0] ?? task.anchor_at?.slice(0, 10) ?? null;
}

/** Consecutive runs of the plan's own order, by planned date. The order is
 * the plan's; grouping never moves a task. */
function groupByDay(tasks: StudyTask[]): DayGroup[] {
  const groups: DayGroup[] = [];
  for (const task of tasks) {
    const date = task.status === "completed" ? null : taskDate(task);
    const kind = task.status === "completed" ? "done" : date ? "date" : "undated";
    const last = groups.at(-1);
    if (last && last.kind === kind && last.date === date) last.tasks.push(task);
    else groups.push({ id: `${kind}-${date ?? ""}-${groups.length}`, kind, date, tasks: [task] });
  }
  return groups;
}

function groupLabel(group: DayGroup): string {
  if (group.kind === "done") return "Done";
  if (group.kind === "undated") return "Not tied to a deadline";
  const p = dateParts(group.date!);
  return `${p.weekday} ${p.day} ${p.month} ${p.year}`;
}

function relativeDays(days: number): string {
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  return days > 1 ? `In ${days} days` : `${-days} days ago`;
}

/** The day column: large and sticky, so the date stays in view while its
 * tasks scroll past. */
function DayHeading({ group, asOf }: { group: DayGroup; asOf: string }) {
  const count = `${group.tasks.length} ${group.tasks.length === 1 ? "task" : "tasks"}`;
  const disputed = group.tasks.some((t) => t.conflict_values.length > 0 || t.anchor_basis === "earliest_conflicting_deadline");
  if (group.kind !== "date") {
    return (
      <div className="self-start border-t-2 border-line pt-4 md:sticky md:top-24">
        <p className={`font-display text-[30px] leading-tight ${group.kind === "done" ? "text-verified" : "text-ink"}`}>
          {group.kind === "done" ? "Done" : "No deadline"}
        </p>
        <p className="mt-2 text-[13px] text-muted">{group.kind === "done" ? count : `${count} · not tied to a stated deadline`}</p>
      </div>
    );
  }
  const p = dateParts(group.date!);
  return (
    <div className={`self-start border-t-2 pt-4 md:sticky md:top-24 ${disputed ? "border-review/70" : "border-ink/25"}`}>
      <p className={`text-[13px] font-semibold tracking-[0.12em] uppercase ${disputed ? "text-review" : "text-link"}`}>{p.weekday}</p>
      <p className="mt-1 flex items-baseline gap-2.5 leading-none">
        <span className="font-display text-[60px] text-ink tabular-nums">{p.day}</span>
        <span className="text-[17px] font-medium text-ink-soft">{p.month} {p.year}</span>
      </p>
      <p className="mt-3 text-[13.5px] text-muted">
        <span className="font-medium text-ink-soft">{relativeDays(daysUntil(asOf, group.date!))}</span> · {count}
      </p>
      {disputed && <p className="mt-1.5 text-[12.5px] text-review">Earliest stated date; the sources disagree</p>}
    </div>
  );
}

function anchorText(task: StudyTask): string | null {
  if (task.conflict_values.length > 0) return task.conflict_values.map(deadlineText).join(" or ");
  if (task.deadlines[0]) return deadlineText(task.deadlines[0]);
  return null;
}

/** What the recomputed plan (from the API) did with the task just changed. */
function SavedNotice({ saved, plan, onDismiss }: { saved: Saved; plan: StudyPlan; onDismiss: () => void }) {
  const task = plan.tasks.find((t) => t.task_key === saved.taskKey);
  // Only once the plan on screen is the recomputed one.
  if (!task || task.study_state !== saved.state) return null;
  return (
    <div role="status" className="panel flex flex-wrap items-center gap-x-4 gap-y-1 border-verified-line px-5 py-3.5 text-[13.5px] animate-fade-up" data-testid="plan-saved">
      <Icon name="checkCircle" size={17} className="text-verified" />
      <p className="min-w-0 flex-1">
        <span className="font-medium text-verified">Saved as {STATE_LABELS[saved.state]}.</span>{" "}
        <span className="text-ink-soft">
          The plan was recomputed: this task is now step {task.position} of {plan.tasks.length}
          {task.status === "completed" ? ", marked done." : "."}
        </span>
      </p>
      <button type="button" onClick={onDismiss} className="text-muted hover:text-ink">Dismiss</button>
    </div>
  );
}

function TaskRow({ task, plan, highlight, onSaved }: {
  task: StudyTask;
  plan: StudyPlan;
  highlight: boolean;
  onSaved: (state: StudyState) => void;
}) {
  const done = task.status === "completed";
  const prerequisites = prerequisitesOf(task, plan);
  const blocked = task.status === "blocked";
  const conflict = task.task_type === "check_conflict";
  const anchor = anchorText(task);
  return (
    <li data-testid="plan-task" data-task-type={task.task_type} data-status={task.status}
      className={`panel border-l-[3px] px-6 py-5 transition-shadow ${conflict ? "border-conflict-line border-l-conflict/70" : courseTone(task.course).edge} ${highlight ? "ring-2 ring-verified/40" : ""}`}>
      <div className="flex gap-4">
        <span className={`mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full text-[12.5px] font-semibold tabular-nums ring-1 ${
          done ? "bg-verified-soft text-verified ring-verified-line"
            : blocked ? "bg-paper text-faint ring-line"
              : conflict ? "bg-conflict-soft text-conflict ring-conflict-line" : "bg-accent-soft text-link ring-accent-line"}`}>
          {done ? <Icon name="check" size={13} strokeWidth={2.6} /> : blocked ? <Icon name="lock" size={12} /> : task.position}
        </span>
        <div className={`min-w-0 flex-1 ${done ? "opacity-70" : ""}`}>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[13px]">
            <span className="badge badge-neutral">{TASK_LABELS[task.task_type]}</span>
            <span className="inline-flex items-center gap-1.5 text-muted"><CourseDot course={task.course} />{courseName(task.course.title)}</span>
            {task.assessment && (
              <Link href={`/assessments/${task.assessment.id}`} className="inline-flex items-center gap-1 text-link">
                <span className="text-faint">For</span> {task.assessment.title}
              </Link>
            )}
            {blocked && <span className="badge badge-review"><Icon name="lock" size={10} />Waiting on a prerequisite</span>}
            {done && <span className="badge badge-verified">Done</span>}
            {conflict && <span className="badge badge-conflict"><Icon name="alert" size={11} />Sources disagree</span>}
            <span className="ml-auto inline-flex items-center gap-1 text-faint tabular-nums">
              <Icon name="clock" size={12} />{task.estimated_minutes} min
            </span>
          </div>
          <p className={`mt-2.5 text-[17px] leading-snug font-medium tracking-tight ${done ? "text-muted line-through decoration-faint" : ""}`} data-testid="task-title">{task.title}</p>
          {anchor && (
            <p className="mt-1.5 text-[14px]" data-testid="task-anchor">
              <span className="text-faint">
                {task.conflict_values.length > 0
                  ? "Stated deadlines: "
                  : task.anchor_basis === "earliest_conflicting_deadline"
                    ? "Planned against the earliest stated deadline (sources disagree): "
                    : "Deadline: "}
              </span>
              <span className="font-medium text-ink-soft tabular-nums">{anchor}</span>
            </p>
          )}
          <p className="mt-1.5 text-[14.5px] text-muted">
            <span className="font-medium text-ink-soft">Why now? </span>
            <span data-testid="why-now">{task.why_now}</span>
          </p>
          {prerequisites.length > 0 && (
            <div className="mt-3 rounded-lg border border-line bg-canvas/50 px-3.5 py-2.5 text-[13px]" data-testid="task-prerequisites">
              <p className="label text-[10px]">Must happen first</p>
              <ul className="mt-1.5 space-y-1">
                {prerequisites.map((p) => (
                  <li key={p.task_key} data-testid="task-prerequisite" data-status={p.status} className="flex items-center gap-2">
                    {p.status === "completed"
                      ? <Icon name="checkCircle" size={13} className="text-verified" />
                      : <span aria-hidden className="h-[13px] w-[13px] rounded-full border border-review/70" />}
                    <span className={p.status === "completed" ? "text-muted line-through decoration-faint" : "text-ink-soft"}>{p.title}</span>
                    <span className="text-faint"> ({p.status === "completed" ? "done" : `step ${p.position}`})</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-2.5">
            {task.task_type === "study_material" && task.study_state && (
              <StudyStateControl spanIds={task.targets.map((t) => t.source_span_id)} state={task.study_state} label={task.title}
                onSaved={onSaved} />
            )}
            <EvidenceButton
              label={task.targets.length ? "Open the material" : "View evidence"}
              request={{
                kind: task.targets.length ? "Mapped teaching material" : "Source evidence",
                claim: task.title,
                items: task.targets.length
                  ? task.targets.map(fromTarget)
                  : [...task.evidence.filter((e) => e.current_version), ...task.conflict_values.flatMap((d) => d.evidence),
                     ...(task.conflict_values.length ? [] : task.deadlines.flatMap((d) => d.evidence))].map(fromEvidence),
              }}
            />
          </div>
        </div>
      </div>
    </li>
  );
}
