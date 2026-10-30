/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";

import { CourseCard } from "@/components/CourseCard";
import { DemoControls } from "@/components/DemoControls";
import { useApi } from "@/components/DemoState";
import { Icon } from "@/components/Icon";
import { CourseDot, courseTone, LoadState, deadlineText } from "@/components/ui";
import { courseName, dateParts, daysUntil, formatInstant, shortDate, TASK_LABELS } from "@/lib/format";
import { activeDeadlines, loadOverview, nextAssessment, openConflicts, type CourseBundle } from "@/lib/load";
import type { StudyTask } from "@/lib/types";

export default function HomePage() {
  const result = useApi(loadOverview, "overview");
  return (
    <LoadState result={result}>
      {({ courses, plan, changes }) => {
        if (courses.length === 0) return <NoCourses />;
        const openTasks = (plan?.tasks ?? []).filter((t) => t.status !== "completed");
        const next = openTasks.slice(0, 5);
        const conflicts = courses.flatMap((b) => openConflicts(b));
        const hidden = courses.flatMap((b) => b.obligations.filter((o) => o.hidden).map((o) => ({ b, o })));
        const latest = changes.at(-1);
        const upcoming = nextAssessment(courses);
        const upcomingConflict = upcoming && openConflicts(upcoming.bundle).some((c) => c.assessment_id === upcoming.assessment.id);
        const attention = conflicts.length + hidden.length + (latest ? 1 : 0);
        const deadlines = deadlinesAhead(courses);
        return (
          <div className="space-y-12">
            <section className="animate-fade-up">
              {plan && <p className="label">Plan as of {formatInstant(plan.as_of)} (demo clock)</p>}
              <h1 className="display-title mt-3">Your courses <em className="text-link">this week</em></h1>
              <p className="page-lede">
                {attention > 0
                  ? <><Count n={attention} /> {attention === 1 ? "thing needs" : "things need"} your attention across </>
                  : "Nothing needs your attention across "}
                <Count n={courses.length} /> {courses.length === 1 ? "course" : "courses"}.
                {openTasks.length > 0 && <> <Count n={openTasks.length} /> open {openTasks.length === 1 ? "task" : "tasks"} in your study plan.</>}
              </p>
            </section>

            <div className="grid grid-cols-1 gap-7 lg:grid-cols-12">
              <div className="min-w-0 space-y-7 lg:col-span-8">
                {upcoming && (
                  <section aria-labelledby="upcoming-heading" className="panel overflow-hidden animate-fade-up" data-testid="upcoming-assessment">
                    <div aria-hidden className={`h-1 ${upcomingConflict ? "bg-review/70" : courseTone(upcoming.bundle.course).bar}`} />
                    <div className="flex flex-col gap-7 p-8 sm:flex-row sm:items-start">
                      <DateTile date={upcoming.deadlines.map((d) => d.due_date).sort()[0]}
                        tone={upcomingConflict ? "text-review" : courseTone(upcoming.bundle.course).text} />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <p className={`eyebrow ${upcomingConflict ? "text-review" : "text-link"}`}>
                            <Icon name="clock" size={14} />
                            {upcomingConflict ? "Coming up · deadline unclear" : "Coming up"}
                          </p>
                          <span className="inline-flex items-center gap-2 text-[13px] text-muted">
                            <CourseDot course={upcoming.bundle.course} />
                            Upcoming assessment in {courseName(upcoming.bundle.course.title)}
                          </span>
                        </div>
                        <h2 id="upcoming-heading" className="mt-3 text-[32px] leading-[1.15] font-semibold tracking-tight">{upcoming.assessment.title}</h2>
                        <p className="mt-3 text-[17px]">
                          <span className="text-muted">{upcoming.deadlines.length > 1 ? "Stated deadlines: " : "Deadline: "}</span>
                          <span className="font-semibold tabular-nums">{upcoming.deadlines.map(deadlineText).join(" or ")}</span>
                        </p>
                        {upcomingConflict && (
                          <div className="mt-5 flex items-start gap-3 rounded-xl border border-conflict-line bg-conflict-soft/70 px-4 py-3.5">
                            <Icon name="alert" size={17} className="mt-0.5 text-conflict" />
                            <p className="text-[14.5px]">
                              <span className="font-semibold text-ink">Deadline conflict</span>{" "}
                              <span className="text-ink-soft">The sources state different deadlines. CourseFlow shows both and does not choose one.</span>
                            </p>
                          </div>
                        )}
                        <div className="mt-6 flex flex-wrap items-center gap-3">
                          <Link href={`/assessments/${upcoming.assessment.id}`} className="btn btn-primary max-w-full whitespace-normal" data-testid="upcoming-cta">
                            See what it requires and where it is taught <Icon name="arrowRight" size={14} />
                          </Link>
                        </div>
                      </div>
                    </div>
                  </section>
                )}

                <section aria-labelledby="next-heading" className="panel animate-fade-up">
                  <div className="flex items-baseline justify-between border-b border-line px-7 py-5">
                    <div>
                      <p className="label">Today</p>
                      <h2 id="next-heading" className="section-title mt-1">Next actions</h2>
                    </div>
                    <Link href="/study-plan" className="inline-flex items-center gap-1 text-[14px] font-medium text-link">
                      Open study plan <Icon name="arrowRight" size={14} />
                    </Link>
                  </div>
                  <ol className="divide-y divide-line">
                    {next.map((task) => (
                      <li key={task.task_key} className="flex gap-4 px-7 py-[18px]" data-testid="next-action">
                        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full bg-accent-soft text-[12.5px] font-semibold text-link ring-1 ring-accent-line">
                          {task.position}
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="text-[16px] leading-snug font-medium">{task.title}</p>
                          <p className="mt-1.5 flex items-center gap-2 text-[13px] text-muted">
                            <span className="badge badge-neutral">{TASK_LABELS[task.task_type]}</span>
                            <CourseDot course={task.course} />
                            {courseName(task.course.title)}
                          </p>
                        </div>
                        {taskDates(task) && <span className="shrink-0 text-[13.5px] font-medium text-ink-soft tabular-nums">{taskDates(task)}</span>}
                      </li>
                    ))}
                    {next.length === 0 && <li className="px-7 py-5 text-[14px] text-muted">Nothing open in the plan.</li>}
                  </ol>
                </section>
              </div>

              <div className="min-w-0 space-y-9 lg:col-span-4">
                <section aria-labelledby="attention-heading" className="space-y-3">
                  <h2 id="attention-heading" className="label">Needs your attention</h2>
                  {conflicts.map((c) => (
                    <Link key={c.id} href={`/assessments/${c.assessment_id}`} data-testid="attention-conflict"
                      className="panel panel-link group flex gap-4 p-5 hover:border-conflict-line">
                      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-conflict-soft text-conflict ring-1 ring-conflict-line">
                        <Icon name="alert" size={17} />
                      </span>
                      <span className="min-w-0">
                        <ActionLine verb="Check" rest={`the deadline for ${c.assessment_title}`} />
                        <span className="mt-1 block text-[13px] text-muted tabular-nums">{c.sides.map(deadlineText).join(" or ")}</span>
                      </span>
                    </Link>
                  ))}
                  {hidden.map(({ b, o }) => (
                    <Link key={o.id} href={`/courses/${b.course.id}#obligation-${o.id}`} data-testid="attention-hidden"
                      className="panel panel-link group flex gap-4 border-accent-line p-5 hover:border-accent">
                      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-accent text-white">
                        <Icon name="sparkles" size={17} />
                      </span>
                      <ActionLine {...splitVerb(o.canonical_action)} />
                    </Link>
                  ))}
                  {latest && (
                    <Link href="/changes" className="panel panel-link group flex gap-4 p-5" data-testid="attention-change">
                      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-paper text-muted ring-1 ring-line">
                        <Icon name="history" size={17} />
                      </span>
                      <ActionLine verb="Review" rest={`changes to ${latest.changes.map((c) => c.resource_title).join(" and ")}`} />
                    </Link>
                  )}
                  {attention === 0 && (
                    <p className="panel px-5 py-4 text-[14px] text-muted">No conflicts, discoveries or changes right now.</p>
                  )}
                </section>

                {deadlines.length > 0 && (
                  <section aria-labelledby="deadlines-heading" className="space-y-3">
                    <h2 id="deadlines-heading" className="label">Deadlines ahead</h2>
                    <ol className="panel divide-y divide-line" data-testid="deadlines-ahead">
                      {deadlines.map(({ bundle, assessment, stated, conflict }) => {
                        const parts = dateParts(stated[0].due_date);
                        return (
                          <li key={assessment.id}>
                            <Link href={`/assessments/${assessment.id}`} data-testid="deadline-row"
                              className="flex items-center gap-4 px-5 py-3.5 transition-colors first:rounded-t-xl last:rounded-b-xl hover:bg-paper/60">
                              <span className="w-11 shrink-0 text-center leading-none">
                                <span className="block text-[11px] font-semibold tracking-[0.08em] text-faint uppercase">{parts.month}</span>
                                <span className="mt-1 block font-display text-[27px] text-ink tabular-nums">{parts.day}</span>
                              </span>
                              <span className="min-w-0 flex-1">
                                <span className="line-clamp-2 block text-[14.5px] leading-snug font-medium">{assessment.title}</span>
                                <span className="mt-0.5 flex items-center gap-1.5 text-[12.5px] text-muted">
                                  <CourseDot course={bundle.course} />
                                  <span className="truncate">{courseName(bundle.course.title)}</span>
                                  {assessment.weighting_percent !== null && <span className="shrink-0 text-faint">· {assessment.weighting_percent}%</span>}
                                </span>
                              </span>
                              {conflict
                                ? <span className="badge badge-review shrink-0">{stated.length} stated dates</span>
                                : plan && <span className="shrink-0 text-[12.5px] text-faint tabular-nums">{relative(daysUntil(plan.as_of, stated[0].due_date))}</span>}
                            </Link>
                          </li>
                        );
                      })}
                    </ol>
                  </section>
                )}
              </div>
            </div>

            <section aria-labelledby="courses-heading" className="space-y-4">
              <h2 id="courses-heading" className="label">Courses</h2>
              <div className="grid gap-5 md:grid-cols-2 2xl:grid-cols-4">
                {courses.map((b) => <CourseCard key={b.course.id} bundle={b} />)}
              </div>
            </section>
          </div>
        );
      }}
    </LoadState>
  );
}

/** A count in the summary line: inline, but the first thing the eye finds. */
function Count({ n }: { n: number }) {
  return <span className="text-[1.3em] leading-none font-semibold text-link tabular-nums">{n}</span>;
}

/** An attention item as one line, its leading action verb emphasised. */
function ActionLine({ verb, rest }: { verb: string; rest: string }) {
  return (
    <span className="block min-w-0 text-[15px] leading-snug font-medium">
      <span className="font-semibold text-link">{verb}</span> {rest}
    </span>
  );
}

function splitVerb(action: string): { verb: string; rest: string } {
  const [verb, ...rest] = action.split(" ");
  return { verb, rest: rest.join(" ") };
}

/** A task's stated date(s), short; every side when sources disagree. */
function taskDates(task: StudyTask): string | null {
  const dates = task.conflict_values.length ? task.conflict_values : task.deadlines.slice(0, 1);
  return dates.length ? dates.map((d) => shortDate(d.due_date)).join(" or ") : null;
}

function relative(days: number): string {
  return days === 0 ? "today" : days === 1 ? "tomorrow" : days > 1 ? `in ${days} days` : `${-days} days ago`;
}

/** Every verified assessment with a stated deadline, earliest first, with
 * all its stated values when sources disagree (none is chosen). */
function deadlinesAhead(courses: CourseBundle[]) {
  const key = (d: { due_date: string; due_time: string | null }) => d.due_date + (d.due_time ?? "");
  return courses
    .flatMap((bundle) =>
      bundle.assessments
        .filter((a) => a.status === "verified" && activeDeadlines(a).length > 0)
        .map((assessment) => ({
          bundle,
          assessment,
          stated: [...activeDeadlines(assessment)].sort((x, y) => key(x).localeCompare(key(y))),
          conflict: openConflicts(bundle).some((c) => c.assessment_id === assessment.id),
        })),
    )
    .sort((x, y) => key(x.stated[0]).localeCompare(key(y.stated[0])))
    .slice(0, 7);
}

/** The earliest stated date, large, as a calendar tile. */
function DateTile({ date, tone }: { date: string; tone: string }) {
  const parts = dateParts(date);
  return (
    <div aria-hidden className="w-24 shrink-0 rounded-xl border border-line bg-canvas/60 px-3 py-3.5 text-center leading-none">
      <p className={`text-[12px] font-semibold tracking-[0.1em] uppercase ${tone}`}>{parts.weekday}</p>
      <p className="mt-2 font-display text-[46px] text-ink tabular-nums">{parts.day}</p>
      <p className="mt-1.5 text-[12px] font-medium tracking-[0.06em] text-muted uppercase">{parts.month} {parts.year}</p>
    </div>
  );
}

/** The entry state before any course is loaded. */
function NoCourses() {
  return (
    <section className="mx-auto max-w-2xl py-16 text-center animate-fade-up">
      <p className="label">University of Portsmouth · demo</p>
      <h1 className="display-title mt-4 text-[3.6rem]">
        Your courses, <em className="text-link">read from their own sources</em>
      </h1>
      <p className="mx-auto mt-4 max-w-md text-[16px] text-muted">
        No courses are loaded yet. Reset the demo to load the Northbridge demo courses.
      </p>
      <div className="panel mx-auto mt-9 max-w-md p-5 text-left shadow-lift">
        <p className="section-title">Load the demo</p>
        <div className="mt-3"><DemoControls /></div>
      </div>
    </section>
  );
}
