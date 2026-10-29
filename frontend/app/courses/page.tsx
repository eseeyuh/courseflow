/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import { CourseCard } from "@/components/CourseCard";
import { useApi } from "@/components/DemoState";
import { LoadState } from "@/components/ui";
import { api } from "@/lib/api";
import { loadCourse } from "@/lib/load";

async function loadCourses() {
  return Promise.all((await api.courses()).map(loadCourse));
}

export default function CoursesPage() {
  const result = useApi(loadCourses, "courses");
  return (
    <div className="space-y-8">
      <header className="animate-fade-up">
        <h1 className="page-title">My Courses</h1>
        <p className="page-lede">Every course CourseFlow has read, with its next deadline and what it found.</p>
      </header>
      <LoadState result={result}>
        {(bundles) =>
          bundles.length === 0 ? (
            <p className="panel max-w-xl border-dashed px-5 py-4 text-muted">No courses yet. Use Reset demo to load the demo courses.</p>
          ) : (
            <div className="grid gap-4 md:grid-cols-2">
              {bundles.map((b) => <CourseCard key={b.course.id} bundle={b} />)}
            </div>
          )
        }
      </LoadState>
    </div>
  );
}
