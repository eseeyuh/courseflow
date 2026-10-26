/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";

import { CourseCard } from "@/components/CourseCard";
import { useApi } from "@/components/DemoState";
import { LoadState, deadlineText } from "@/components/ui";
import { courseName, formatInstant, TASK_LABELS } from "@/lib/format";
import { loadOverview, nextAssessment, openConflicts } from "@/lib/load";

export default function HomePage() {
  const result = useApi(loadOverview, "overview");
  return (
    <LoadState result={result}>
      {({ courses, plan, changes }) => {
        if (courses.length === 0) {
          return (
            <section className="panel max-w-xl p-6">
              <h1 className="page-title">No courses yet</h1>
              <p className="mt-2 text-muted">Use Reset demo to load the Northbridge demo courses.</p>
            </section>
          );
        }
        const next = (plan?.tasks ?? []).filter((t) => t.status !== "completed").slice(0, 3);
        const conflicts = courses.flatMap((b) => openConflicts(b).map((c) => ({ b, c })));
        const hidden = courses.flatMap((b) =>
          b.obligations.filter((o) => o.hidden).map((o) => ({
            b, o, assessment: b.assessments.find((a) => a.id === o.assessment_id),
          })),
        );
        const latest = changes.at(-1);
        const upcoming = nextAssessment(courses);
        const upcomingConflict = upcoming && openConflicts(upcoming.bundle).some((c) => c.assessment_id === upcoming.assessment.id);
        return (
          <div className="space-y-10">
            <section>
              <h1 className="page-title">Your courses this week</h1>
              {plan && (
                <p className="mt-1 text-sm text-muted">Plan as of {formatInstant(plan.as_of)} (demo clock)</p>
              )}
            </section>

            {upcoming && (
              <section aria-labelledby="upcoming-heading" className="panel border-accent/40 px-6 py-5" data-testid="upcoming-assessment">
                <p className="text-sm text-muted">Upcoming assessment in {courseName(upcoming.bundle.course.title)}</p>
                <div className="mt-1 flex flex-wrap items-center gap-3">
                  <h2 id="upcoming-heading" className="text-xl font-semibold">{upcoming.assessment.title}</h2>
                  {upcomingConflict && <span className="badge badge-conflict">Deadline conflict</span>}
                </div>
                <p className="mt-1 text-sm">
                  <span className="text-muted">{upcoming.deadlines.length > 1 ? "Stated deadlines: " : "Deadline: "}</span>
                  {upcoming.deadlines.map(deadlineText).join(" or ")}
                </p>
                <Link href={`/assessments/${upcoming.assessment.id}`} className="btn btn-primary mt-4" data-testid="upcoming-cta">
                  See what it requires and where it is taught
                </Link>
              </section>
            )}

            <div className="grid gap-6 lg:grid-cols-[3fr_2fr]">
              <section aria-labelledby="next-heading" className="panel p-5">
                <div className="flex items-baseline justify-between">
                  <h2 id="next-heading" className="section-title">Next actions</h2>
                  <Link href="/study-plan" className="text-sm text-accent hover:underline">Open study plan</Link>
                </div>
                <ol className="mt-4 space-y-4">
                  {next.map((task) => (
                    <li key={task.task_key} className="flex gap-4" data-testid="next-action">
                      <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent-soft text-sm font-semibold text-accent">
                        {task.position}
                      </span>
                      <div className="min-w-0">
                        <p className="text-sm text-muted">
                          {TASK_LABELS[task.task_type]} in {courseName(task.course.title)}
                        </p>
                        <p className="font-medium">{task.title}</p>
                        <p className="mt-1 text-sm text-muted">{task.why_now}</p>
                      </div>
                    </li>
                  ))}
                  {next.length === 0 && <li className="text-sm text-muted">Nothing open in the plan.</li>}
                </ol>
              </section>

              <section aria-labelledby="attention-heading" className="space-y-3">
                <h2 id="attention-heading" className="section-title">Needs your attention</h2>
                {conflicts.map(({ b, c }) => (
                  <Link key={c.id} href={`/assessments/${c.assessment_id}`} data-testid="attention-conflict"
                    className="panel block border-conflict/50 p-4 hover:border-conflict">
                    <span className="badge badge-conflict">Deadline conflict</span>
                    <p className="mt-2 font-medium">{c.assessment_title}</p>
                    <p className="text-sm text-muted">
                      {courseName(b.course.title)}: {c.sides.map(deadlineText).join(" or ")}
                    </p>
                  </Link>
                ))}
                {hidden.map(({ b, o, assessment }) => (
                  <Link key={o.id} href={`/courses/${b.course.id}#obligation-${o.id}`} data-testid="attention-hidden"
                    className="panel block p-4 hover:border-accent">
                    <span className="badge badge-found">Found by CourseFlow</span>
                    <p className="mt-2 font-medium">{o.canonical_action}</p>
                    <p className="text-sm text-muted">
                      {courseName(b.course.title)}: stated in the {o.evidence[0]?.resource_title ?? "course sources"}
                      {assessment ? `, not in the ${assessment.title} brief` : ""}
                    </p>
                  </Link>
                ))}
                {latest && (
                  <Link href="/changes" className="panel block p-4 hover:border-accent" data-testid="attention-change">
                    <span className="badge badge-neutral">Recent change</span>
                    <p className="mt-2 font-medium">
                      {latest.changes.map((c) => c.resource_title).join(" and ")} updated
                    </p>
                    <p className="text-sm text-muted">Synced {formatInstant(latest.observed_at)}</p>
                  </Link>
                )}
              </section>
            </div>

            <section aria-labelledby="courses-heading">
              <h2 id="courses-heading" className="section-title">Courses</h2>
              <div className="mt-3 grid gap-4 md:grid-cols-2">
                {courses.map((b) => <CourseCard key={b.course.id} bundle={b} />)}
              </div>
            </section>
          </div>
        );
      }}
    </LoadState>
  );
}
