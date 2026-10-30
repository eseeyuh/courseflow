/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import Link from "next/link";

import { Icon } from "@/components/Icon";
import { CourseDot, courseTone, deadlineText, RunStatus, runNeedsNotice } from "@/components/ui";
import { courseCode, courseName } from "@/lib/format";
import { activeDeadlines, openConflicts, type CourseBundle } from "@/lib/load";

// Presentation only, for the demo recording: fixed display values, not a
// measure of anything. They are not derived from study states, the plan or
// assessments, and nothing reads them back.
const DEMO_PROGRESS: Record<string, number> = { AML7001: 68, MSD7000: 42, AIF7002: 56, DVS7003: 73 };

/** A course's demo progress as a bar, in its identity colour. */
function CourseProgress({ course }: { course: CourseBundle["course"] }) {
  const percent = DEMO_PROGRESS[courseCode(course.title, course.external_id)];
  if (percent === undefined) return null;
  return (
    <div className="mt-5" data-testid="course-progress">
      <div className="flex items-baseline justify-between">
        <span className="text-[12.5px] text-faint">Course progress</span>
        <span className="text-[17px] font-semibold text-ink tabular-nums">{percent}%</span>
      </div>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-line/70" role="progressbar"
        aria-label="Course progress" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
        <div className={`h-full rounded-full ${courseTone(course).dot}`} style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

/** A course: its next verified deadline (every stated value when sources
 * disagree), open conflicts, hidden obligations, its latest run. */
export function CourseCard({ bundle }: { bundle: CourseBundle }) {
  const { course } = bundle;
  const upcoming = bundle.assessments
    .filter((a) => a.status === "verified")
    .flatMap((a) => activeDeadlines(a).map((d) => ({ a, d })))
    .sort((x, y) => (x.d.due_date + (x.d.due_time ?? "")).localeCompare(y.d.due_date + (y.d.due_time ?? "")))[0];
  const conflicts = openConflicts(bundle);
  const hidden = bundle.obligations.filter((o) => o.hidden);
  return (
    <Link href={`/courses/${course.id}`} className="panel panel-link group relative flex flex-col overflow-hidden p-7" data-testid="course-card">
      <span aria-hidden className={`absolute inset-x-0 top-0 h-[3px] ${courseTone(course).bar}`} />
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-[12.5px] font-medium tracking-wide text-muted">
            <CourseDot course={course} />
            {courseCode(course.title, course.external_id)}
          </p>
          <h3 className="mt-1.5 text-[20px] font-semibold tracking-tight">{courseName(course.title)}</h3>
        </div>
        <Icon name="chevronRight" size={16} className="mt-1 text-faint transition-transform group-hover:translate-x-0.5 group-hover:text-link" />
      </div>
      <dl className="mt-5 space-y-2 text-[14.5px]">
        <div className="flex gap-3">
          <dt className="w-28 shrink-0 text-faint">Next due</dt>
          <dd className="text-ink-soft">
            {upcoming ? (
              <><span className="font-medium text-ink">{upcoming.a.title}</span>, <span className="tabular-nums">{activeDeadlines(upcoming.a).map(deadlineText).join(" or ")}</span></>
            ) : "No verified deadline"}
          </dd>
        </div>
        <div className="flex gap-3">
          <dt className="w-28 shrink-0 text-faint">Assessments</dt>
          <dd className="text-ink-soft">{bundle.assessments.filter((a) => a.status === "verified").length} verified</dd>
        </div>
        {runNeedsNotice(bundle.latestRun) && (
          <div className="flex gap-3">
            <dt className="w-28 shrink-0 text-faint">Sources</dt>
            <dd className="text-ink-soft"><RunStatus run={bundle.latestRun} /></dd>
          </div>
        )}
      </dl>
      <CourseProgress course={course} />
      {(conflicts.length > 0 || hidden.length > 0) && (
        <div className="mt-5 flex flex-wrap gap-2 border-t border-line pt-4">
          {conflicts.length > 0 && <span className="badge badge-conflict"><Icon name="alert" size={11} />Deadline conflict</span>}
          {hidden.length > 0 && <span className="badge badge-found"><Icon name="sparkles" size={11} />Found by CourseFlow</span>}
        </div>
      )}
    </Link>
  );
}
