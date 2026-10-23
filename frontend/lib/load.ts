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
  Sync,
} from "@/lib/types";

export type CourseBundle = {
  course: Course;
  assessments: Assessment[];
  obligations: Obligation[];
  conflicts: Conflict[];
  dependencies: DependencyEdge[];
};

export async function loadCourse(course: Course): Promise<CourseBundle> {
  const [assessments, obligations, conflicts, dependencies] = await Promise.all([
    api.assessments(course.id),
    api.obligations(course.id),
    api.conflicts(course.id),
    api.dependencies(course.id),
  ]);
  return { course, assessments, obligations, conflicts, dependencies };
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

export function openConflicts(bundle: CourseBundle): Conflict[] {
  return bundle.conflicts.filter((c) => c.status === "open");
}
