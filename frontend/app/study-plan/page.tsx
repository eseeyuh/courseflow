/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";

import { useApi } from "@/components/DemoState";
import { EvidenceButton, fromEvidence, fromTarget } from "@/components/Evidence";
import { LoadState, StudyStateControl, deadlineText } from "@/components/ui";
import { api } from "@/lib/api";
import { courseName, formatInstant, TASK_LABELS } from "@/lib/format";
import { prerequisitesOf } from "@/lib/load";
import type { StudyPlan, StudyTask } from "@/lib/types";

export default function StudyPlanPage() {
  const result = useApi(api.studyPlan, "study-plan");
  return (
    <div className="space-y-6">
      <header>
        <h1 className="page-title">Study plan</h1>
        <p className="mt-1 max-w-prose text-sm text-muted">
          Ordered across your courses: what has to happen first, then the nearest deadline, mandatory
          before recommended. Every reason comes from the course sources.
        </p>
      </header>
      <LoadState result={result}>
        {(plan) => (
          <>
            <p className="text-sm text-muted" data-testid="plan-as-of">
              As of {formatInstant(plan.as_of)} (demo clock).{" "}
              {[...new Set(plan.tasks.map((t) => courseName(t.course.title)))].map((name) => {
                const open = plan.tasks.filter((t) => courseName(t.course.title) === name && t.status !== "completed").length;
                return `${name}: ${open} open`;
              }).join(", ")}.
            </p>
            <ol className="space-y-3">
              {plan.tasks.map((task) => <TaskRow key={task.task_key} task={task} plan={plan} />)}
            </ol>
          </>
        )}
      </LoadState>
    </div>
  );
}

function anchorText(task: StudyTask): string | null {
  if (task.conflict_values.length > 0) return task.conflict_values.map(deadlineText).join(" or ");
  if (task.deadlines[0]) return deadlineText(task.deadlines[0]);
  return null;
}

function TaskRow({ task, plan }: { task: StudyTask; plan: StudyPlan }) {
  const done = task.status === "completed";
  const prerequisites = prerequisitesOf(task, plan);
  const blocked = task.status === "blocked";
  const anchor = anchorText(task);
  return (
    <li data-testid="plan-task" data-task-type={task.task_type} data-status={task.status}
      className={`panel px-5 py-4 ${done ? "opacity-70" : ""} ${task.task_type === "check_conflict" ? "border-conflict/50" : ""}`}>
      <div className="flex gap-4">
        <span className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-sm font-semibold ${done ? "bg-verified-soft text-verified" : "bg-accent-soft text-accent"}`}>
          {task.position}
        </span>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="badge badge-neutral">{TASK_LABELS[task.task_type]}</span>
            <span className="text-muted">{courseName(task.course.title)}</span>
            {task.assessment && (
              <Link href={`/assessments/${task.assessment.id}`} className="text-accent hover:underline">{task.assessment.title}</Link>
            )}
            {blocked && <span className="badge badge-review">Waiting on a prerequisite</span>}
            {done && <span className="badge badge-verified">Done</span>}
            {task.task_type === "check_conflict" && <span className="badge badge-conflict">Sources disagree</span>}
            <span className="ml-auto text-muted">{task.estimated_minutes} min</span>
          </div>
          <p className={`font-medium ${done ? "line-through decoration-muted" : ""}`} data-testid="task-title">{task.title}</p>
          {anchor && (
            <p className="text-sm" data-testid="task-anchor">
              <span className="text-muted">
                {task.conflict_values.length > 0
                  ? "Stated deadlines: "
                  : task.anchor_basis === "earliest_conflicting_deadline"
                    ? "Planned against the earliest stated deadline (sources disagree): "
                    : "Deadline: "}
              </span>
              {anchor}
            </p>
          )}
          <p className="text-sm text-muted" data-testid="why-now">{task.why_now}</p>
          {prerequisites.length > 0 && (
            <div className="text-sm" data-testid="task-prerequisites">
              <p className="text-muted">Must happen first:</p>
              <ul className="mt-0.5 list-disc space-y-0.5 pl-5">
                {prerequisites.map((p) => (
                  <li key={p.task_key} data-testid="task-prerequisite" data-status={p.status}>
                    <span className={p.status === "completed" ? "text-muted line-through" : ""}>{p.title}</span>
                    <span className="text-muted"> ({p.status === "completed" ? "done" : `step ${p.position}`})</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            {task.task_type === "study_material" && task.study_state && (
              <StudyStateControl spanIds={task.targets.map((t) => t.source_span_id)} state={task.study_state} label={task.title} />
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
