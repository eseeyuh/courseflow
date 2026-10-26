/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import { api, ApiError } from "@/lib/api";
import type {
  Assessment,
  Conflict,
  Course,
  Deadline,
  DependencyEdge,
  Obligation,
  StudyPlan,
  StudyTask,
  Sync,
  WorkflowRun,
} from "@/lib/types";

export type CourseBundle = {
  course: Course;
  assessments: Assessment[];
  obligations: Obligation[];
  conflicts: Conflict[];
  dependencies: DependencyEdge[];
  /** The course's latest course-intelligence run, newest first (null if none). */
  latestRun: WorkflowRun | null;
};

export async function loadCourse(course: Course): Promise<CourseBundle> {
  const [assessments, obligations, conflicts, dependencies, runs] = await Promise.all([
    api.assessments(course.id),
    api.obligations(course.id),
    api.conflicts(course.id),
    api.dependencies(course.id),
    api.workflowRuns(course.id),
  ]);
  return { course, assessments, obligations, conflicts, dependencies, latestRun: runs[0] ?? null };
}

export async function loadCourseById(id: string): Promise<CourseBundle> {
  const course = (await api.courses()).find((c) => c.id === id);
  if (!course) throw new ApiError(404, "This course is not in the demo data. Reset the demo to reload it.");
  return loadCourse(course);
}

export type Overview = { courses: CourseBundle[]; plan: StudyPlan | null; changes: Sync[] };

export async function loadOverview(): Promise<Overview> {
  const courses = await api.courses();
  const [bundles, plan, changes] = await Promise.all([
    Promise.all(courses.map(loadCourse)),
    api.studyPlan().catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 409) return null; // no plan yet
      throw e;
    }),
    api.changes(),
  ]);
  return { courses: bundles, plan, changes };
}

/** The verified deadlines that count now (active), in date order. */
export function activeDeadlines(a: Assessment): Deadline[] {
  return a.deadlines.filter((d) => d.active);
}

/** The next verified assessment by its earliest active deadline. Every
 * active deadline is returned with it: the earliest is only the sort key. */
export function nextAssessment(bundles: CourseBundle[]): { bundle: CourseBundle; assessment: Assessment; deadlines: Deadline[] } | null {
  const key = (d: Deadline) => d.due_date + (d.due_time ?? "");
  const candidates = bundles.flatMap((bundle) =>
    bundle.assessments
      .filter((a) => a.status === "verified" && activeDeadlines(a).length > 0)
      .map((assessment) => ({ bundle, assessment, deadlines: activeDeadlines(assessment) })),
  );
  const first = (c: (typeof candidates)[number]) => c.deadlines.map(key).sort()[0];
  return candidates.sort((x, y) => first(x).localeCompare(first(y)))[0] ?? null;
}

/** Tasks that must happen before this one, as the plan states them. */
export function prerequisitesOf(task: StudyTask, plan: StudyPlan): StudyTask[] {
  return task.prerequisite_keys.flatMap((k) => plan.tasks.filter((t) => t.task_key === k));
}

export function openConflicts(bundle: CourseBundle): Conflict[] {
  return bundle.conflicts.filter((c) => c.status === "open");
}
