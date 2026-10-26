/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect } from "react";

import { useApi } from "@/components/DemoState";
import { EvidenceButton, fromEvidence, StatusBadge } from "@/components/Evidence";
import { ConflictCard, DeadlineEvidence, DependencyChain, LoadState, RunStatus, deadlineText } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { courseCode, courseName, RESOURCE_LABELS, TASK_LABELS } from "@/lib/format";
import { activeDeadlines, loadCourseById, openConflicts, type CourseBundle } from "@/lib/load";
import type { Obligation, StudyTask } from "@/lib/types";

async function loadCoursePage(id: string) {
  const [bundle, plan] = await Promise.all([
    loadCourseById(id),
    api.studyPlan().catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 409) return null; // no plan yet
      throw e;
    }),
  ]);
  const next = plan?.tasks.find((t) => t.course.id === id && t.status === "ready") ?? null;
  return { bundle, next };
}

/** Scrolls to the #fragment once the client-rendered content exists. */
function useScrollToHash(ready: boolean) {
  useEffect(() => {
    if (!ready || !window.location.hash) return;
    const target = document.getElementById(window.location.hash.slice(1));
    target?.scrollIntoView({ block: "center" });
    target?.classList.add("ring-2", "ring-accent");
  }, [ready]);
}

export default function CoursePage() {
  const { id } = useParams<{ id: string }>();
  const result = useApi(() => loadCoursePage(id), `course:${id}`);
  useScrollToHash(result.state === "ready");
  return <LoadState result={result}>{({ bundle, next }) => <Course bundle={bundle} next={next} />}</LoadState>;
}

function Course({ bundle, next }: { bundle: CourseBundle; next: StudyTask | null }) {
  const { course, assessments, obligations, dependencies } = bundle;
  const conflicts = openConflicts(bundle);
  const verified = assessments.filter((a) => a.status === "verified");
  const review = assessments.filter((a) => a.status !== "verified");
  const hidden = obligations.filter((o) => o.hidden);
  const stated = obligations.filter((o) => !o.hidden && o.status === "verified");
  const toReview = obligations.filter((o) => o.status !== "verified");
  return (
    <div className="space-y-10">
      <header>
        <p className="text-sm text-muted">{courseCode(course.title, course.external_id)}</p>
        <h1 className="page-title">{courseName(course.title)}</h1>
        <p className="mt-1 text-sm text-muted">Course sources: <RunStatus run={bundle.latestRun} /></p>
      </header>

      {next && (
        <section className="panel flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-4" data-testid="course-next">
          <span className="badge badge-neutral">Next: {TASK_LABELS[next.task_type]}</span>
          <div className="min-w-0 flex-1">
            <p className="font-medium">{next.title}</p>
            <p className="text-sm text-muted">{next.why_now}</p>
          </div>
          <Link href="/study-plan" className="text-sm text-accent hover:underline">Open study plan</Link>
        </section>
      )}

      {conflicts.map((c) => <ConflictCard key={c.id} conflict={c} link />)}

      <section aria-labelledby="assessments-heading" className="space-y-3">
        <h2 id="assessments-heading" className="section-title">Assessments</h2>
        <ul className="space-y-3">
          {verified.map((a) => {
            const conflict = conflicts.find((c) => c.assessment_id === a.id);
            return (
              <li key={a.id}>
                <Link href={`/assessments/${a.id}`} data-testid="assessment-link"
                  className="panel flex flex-wrap items-center gap-x-6 gap-y-2 px-5 py-4 hover:border-accent">
                  <span className="min-w-56 font-medium">{a.title}</span>
                  <span className="text-sm text-muted">
                    {a.weighting_percent !== null ? `${a.weighting_percent}% of the module` : "Weighting not stated"}
                  </span>
                  <span className="text-sm">
                    {activeDeadlines(a).map(deadlineText).join(" or ") || "No verified deadline"}
                  </span>
                  <span className="ml-auto flex gap-2">
                    {conflict && <span className="badge badge-conflict">Deadline conflict</span>}
                    <StatusBadge status={a.status} />
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
        {review.length > 0 && (
          <details className="text-sm">
            <summary className="cursor-pointer text-muted">
              {review.length} more {review.length === 1 ? "item" : "items"} that look like assessments, not verified
            </summary>
            <ul className="mt-2 space-y-1 pl-4 text-muted">
              {review.map((a) => <li key={a.id}>{a.title} <span className="badge badge-review">Needs review</span></li>)}
            </ul>
          </details>
        )}
      </section>

      <section aria-labelledby="obligations-heading" className="space-y-4">
        <h2 id="obligations-heading" className="section-title">Obligations</h2>
        {hidden.length > 0 && (
          <div className="space-y-3">
            {hidden.map((o) => <ObligationRow key={o.id} o={o} bundle={bundle} />)}
          </div>
        )}
        {stated.map((o) => <ObligationRow key={o.id} o={o} bundle={bundle} />)}
        {toReview.length > 0 && (
          <div className="space-y-2">
            <h3 className="text-sm font-medium text-review">Not verified: check these yourself</h3>
            {toReview.map((o) => <ObligationRow key={o.id} o={o} bundle={bundle} />)}
          </div>
        )}
        {obligations.length === 0 && <p className="text-sm text-muted">No obligations found in the sources.</p>}
      </section>

      <section aria-labelledby="dependencies-heading" className="space-y-3">
        <h2 id="dependencies-heading" className="section-title">What has to happen first</h2>
        <DependencyChain edges={dependencies} />
      </section>
    </div>
  );
}

function ObligationRow({ o, bundle }: { o: Obligation; bundle: CourseBundle }) {
  const assessment = bundle.assessments.find((a) => a.id === o.assessment_id);
  const requiredFor = bundle.dependencies.filter((e) => e.prerequisite.id === o.id);
  const verified = o.status === "verified";
  return (
    <article id={`obligation-${o.id}`} data-testid={o.hidden ? "hidden-obligation" : "obligation"}
      className={`panel px-5 py-4 ${verified ? "" : "border-dashed bg-review-soft/40"} ${o.hidden ? "border-accent" : ""}`}>
      <div className="flex flex-wrap items-center gap-2">
        {o.hidden && <span className="badge badge-found">Found by CourseFlow</span>}
        <StatusBadge status={o.status} />
        {o.modality && <span className="badge badge-neutral">{o.modality === "mandatory" ? "Mandatory" : "Recommended"}</span>}
      </div>
      <p className={`mt-2 ${verified ? "font-medium" : "text-muted"}`}>{o.canonical_action}</p>
      {o.hidden && assessment && (
        <p className="mt-1 text-sm text-muted">
          Stated in the {o.evidence[0]?.resource_title}, not in the {assessment.title} brief.
        </p>
      )}
      {requiredFor.map((e) => (
        <p key={e.id} className="mt-2 text-sm" data-testid="obligation-relationship">
          <span className="badge badge-neutral mr-2">{e.relation === "required_for" ? "Required for" : "Before"}</span>
          {e.dependent.kind === "assessment" && e.dependent.id ? (
            <Link href={`/assessments/${e.dependent.id}`} className="font-medium text-accent hover:underline">{e.dependent.label}</Link>
          ) : (
            <span className="font-medium">{e.dependent.label}</span>
          )}
          {assessment && e.dependent.id === assessment.id && activeDeadlines(assessment)[0] && (
            <span className="text-muted">
              {" "}(due {deadlineText(activeDeadlines(assessment)[0])})
              <span className="ml-2 inline-block align-middle">
                <DeadlineEvidence deadline={activeDeadlines(assessment)[0]} claim={assessment.title} />
              </span>
            </span>
          )}
        </p>
      ))}
      <div className="mt-3">
        <EvidenceButton
          label={o.hidden ? `Show where the ${(RESOURCE_LABELS[o.evidence[0]?.resource_type] ?? "source").toLowerCase()} says this` : "View evidence"}
          request={{
            kind: o.hidden ? "Found by CourseFlow" : "Obligation evidence",
            claim: o.canonical_action,
            items: o.evidence.filter((e) => e.current_version).map(fromEvidence),
          }}
        />
      </div>
    </article>
  );
}
