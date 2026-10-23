/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import Link from "next/link";

import { deadlineText } from "@/components/ui";
import { courseCode, courseName } from "@/lib/format";
import { activeDeadlines, openConflicts, type CourseBundle } from "@/lib/load";

/** A course: its next verified deadline, open conflicts, hidden obligations. */
export function CourseCard({ bundle }: { bundle: CourseBundle }) {
  const { course } = bundle;
  const upcoming = bundle.assessments
    .filter((a) => a.status === "verified")
    .flatMap((a) => activeDeadlines(a).map((d) => ({ a, d })))
    .sort((x, y) => (x.d.due_date + (x.d.due_time ?? "")).localeCompare(y.d.due_date + (y.d.due_time ?? "")))[0];
  const conflicts = openConflicts(bundle);
  const hidden = bundle.obligations.filter((o) => o.hidden);
  return (
    <Link href={`/courses/${course.id}`} className="panel block p-5 hover:border-accent" data-testid="course-card">
      <p className="text-sm text-muted">{courseCode(course.title, course.external_id)}</p>
      <h3 className="mt-0.5 text-lg font-semibold">{courseName(course.title)}</h3>
      <dl className="mt-4 space-y-1.5 text-sm">
        <div className="flex gap-2">
          <dt className="w-24 shrink-0 text-muted">Next due</dt>
          <dd>{upcoming ? `${upcoming.a.title}, ${deadlineText(upcoming.d)}` : "No verified deadline"}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-24 shrink-0 text-muted">Assessments</dt>
          <dd>{bundle.assessments.filter((a) => a.status === "verified").length} verified</dd>
        </div>
      </dl>
      <div className="mt-4 flex flex-wrap gap-2">
        {conflicts.length > 0 && <span className="badge badge-conflict">Deadline conflict</span>}
        {hidden.length > 0 && <span className="badge badge-found">Found by CourseFlow</span>}
      </div>
    </Link>
  );
}
