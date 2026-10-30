/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect } from "react";

import { useApi } from "@/components/DemoState";
import { EvidenceButton, fromEvidence, StatusBadge } from "@/components/Evidence";
import { Icon } from "@/components/Icon";
import { ConflictCard, CourseDot, DeadlineEvidence, DependencyChain, LoadState, RunStatus, deadlineText, runNeedsNotice } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { courseCode, courseName, locator, RESOURCE_LABELS, TASK_LABELS } from "@/lib/format";
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
    target?.classList.add("ring-2", "ring-link/60");
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
    <div className="space-y-12">
      <header className="animate-fade-up">
        <p className="flex items-center gap-2 text-[13px] font-medium tracking-wide text-muted">
          <CourseDot course={course} />
          {courseCode(course.title, course.external_id)}
        </p>
        <h1 className="page-title mt-2">{courseName(course.title)}</h1>
        {runNeedsNotice(bundle.latestRun) && (
          <p className="mt-2 text-[14px] text-muted">Course sources: <RunStatus run={bundle.latestRun} /></p>
        )}
      </header>

      {next && (
        <section className="panel flex flex-wrap items-center gap-x-5 gap-y-3 px-6 py-5" data-testid="course-next">
          <div className="min-w-0 flex-1">
            <p className="label">Recommended next · {TASK_LABELS[next.task_type]}</p>
            <p className="mt-1.5 text-[17.5px] font-semibold tracking-tight">{next.title}</p>
            <p className="mt-0.5 text-[14.5px] text-muted"><span className="font-medium text-ink-soft">Why now? </span>{next.why_now}</p>
          </div>
          <Link href="/study-plan" className="btn btn-quiet">Open study plan <Icon name="arrowRight" size={14} /></Link>
        </section>
      )}

      {conflicts.map((c) => <ConflictCard key={c.id} conflict={c} link />)}

      <section aria-labelledby="assessments-heading" className="space-y-3">
        <h2 id="assessments-heading" className="label">Assessments</h2>
        <ul className="panel divide-y divide-line overflow-hidden">
          {verified.map((a) => {
            const conflict = conflicts.find((c) => c.assessment_id === a.id);
            return (
              <li key={a.id}>
                <Link href={`/assessments/${a.id}`} data-testid="assessment-link"
                  className="group flex flex-wrap items-center gap-x-6 gap-y-2 px-6 py-4 transition-colors hover:bg-paper/50">
                  <span className="min-w-56 text-[16px] font-medium">{a.title}</span>
                  <span className="text-[14px] text-muted">
                    {a.weighting_percent !== null ? `${a.weighting_percent}% of the module` : "Weighting not stated"}
                  </span>
                  <span className="text-[14px] text-ink-soft tabular-nums">
                    {activeDeadlines(a).map(deadlineText).join(" or ") || "No verified deadline"}
                  </span>
                  <span className="ml-auto flex items-center gap-2">
                    {conflict && <span className="badge badge-conflict"><Icon name="alert" size={11} />Deadline conflict</span>}
                    <StatusBadge status={a.status} />
                    <Icon name="chevronRight" size={15} className="text-faint group-hover:text-link" />
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
        {review.length > 0 && (
          <details className="text-[14px]">
            <summary className="cursor-pointer text-muted hover:text-ink">
              {review.length} more {review.length === 1 ? "item" : "items"} that look like assessments, not verified
            </summary>
            <ul className="mt-2 space-y-1.5 pl-4 text-muted">
              {review.map((a) => <li key={a.id}>{a.title} <span className="badge badge-review">Needs review</span></li>)}
            </ul>
          </details>
        )}
      </section>

      <section aria-labelledby="obligations-heading" className="space-y-3">
        <h2 id="obligations-heading" className="label">Obligations</h2>
        {hidden.map((o) => <HiddenObligation key={o.id} o={o} bundle={bundle} />)}
        {stated.length > 0 && (
          <div className="panel divide-y divide-line">
            {stated.map((o) => <ObligationRow key={o.id} o={o} bundle={bundle} />)}
          </div>
        )}
        {toReview.length > 0 && (
          <div className="panel border-dashed border-review-line">
            <h3 className="flex items-center gap-2 border-b border-review-line/60 px-6 py-3 text-[14px] font-medium text-review">
              <Icon name="alert" size={14} />
              Not verified: check these yourself ({toReview.length})
            </h3>
            <ul className="divide-y divide-line">
              {toReview.map((o) => (
                <li key={o.id} id={`obligation-${o.id}`} data-testid="obligation"
                  className="flex flex-wrap items-center gap-x-4 gap-y-2 px-6 py-3 text-[14.5px]">
                  <span className="badge badge-review">Needs review</span>
                  <span className="min-w-0 flex-1 text-ink-soft">{o.canonical_action}</span>
                  <EvidenceButton request={{ kind: "Obligation evidence (needs review)", claim: o.canonical_action,
                    items: o.evidence.filter((e) => e.current_version).map(fromEvidence) }} />
                </li>
              ))}
            </ul>
          </div>
        )}
        {obligations.length === 0 && <p className="text-[14.5px] text-muted">No obligations found in the sources.</p>}
      </section>

      <section aria-labelledby="dependencies-heading" className="space-y-3">
        <h2 id="dependencies-heading" className="label">What has to happen first</h2>
        <DependencyChain edges={dependencies} />
      </section>
    </div>
  );
}

function sourceOf(o: Obligation) {
  return o.evidence.find((e) => e.current_version) ?? o.evidence[0];
}

/** What the obligation is required for, from the stored dependency edges. */
function RequiredFor({ o, bundle }: { o: Obligation; bundle: CourseBundle }) {
  const assessment = bundle.assessments.find((a) => a.id === o.assessment_id);
  return (
    <>
      {bundle.dependencies.filter((e) => e.prerequisite.id === o.id).map((e) => (
        <div key={e.id} className="contents" data-testid="obligation-relationship">
          <dt className="text-faint">{e.relation === "required_for" ? "Required for" : "Before"}</dt>
          <dd className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {e.dependent.kind === "assessment" && e.dependent.id ? (
              <Link href={`/assessments/${e.dependent.id}`} className="font-medium text-link">{e.dependent.label}</Link>
            ) : (
              <span className="font-medium">{e.dependent.label}</span>
            )}
            {assessment && e.dependent.id === assessment.id && activeDeadlines(assessment).length > 0 && (
              <>
                <span className="text-muted tabular-nums">due {activeDeadlines(assessment).map(deadlineText).join(" or ")}</span>
                <DeadlineEvidence deadline={activeDeadlines(assessment)[0]} claim={`${assessment.title} deadline`} />
              </>
            )}
          </dd>
        </div>
      ))}
    </>
  );
}

/** A hidden obligation: a discovery, stated in a source but not in the brief. */
function HiddenObligation({ o, bundle }: { o: Obligation; bundle: CourseBundle }) {
  const assessment = bundle.assessments.find((a) => a.id === o.assessment_id);
  const source = sourceOf(o);
  const sourceKind = (RESOURCE_LABELS[source?.resource_type] ?? "source").toLowerCase();
  return (
    <article id={`obligation-${o.id}`} data-testid="hidden-obligation"
      className="panel relative overflow-hidden rounded-2xl border-accent-line shadow-lift">
      <div aria-hidden className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_top_right,rgb(84_104_228/0.12),transparent_55%)]" />
      <div className="relative p-7">
        <div className="flex flex-wrap items-center gap-2">
          <span className="eyebrow mr-1 text-link">
            <span className="grid h-6 w-6 place-items-center rounded-md bg-accent text-white"><Icon name="sparkles" size={13} /></span>
            Found by CourseFlow
          </span>
          {o.modality && <span className="badge badge-neutral">{o.modality === "mandatory" ? "Mandatory" : "Recommended"}</span>}
          <StatusBadge status={o.status} />
        </div>
        <p className="mt-4 text-[24px] leading-snug font-semibold tracking-tight">{o.canonical_action}</p>
        {assessment && (
          <p className="mt-1.5 max-w-2xl text-[15px] text-muted">
            Stated in the {source?.resource_title ?? "course sources"}, but not mentioned in the {assessment.title} brief.
          </p>
        )}
        <dl className="mt-5 grid max-w-3xl grid-cols-[6.5rem_1fr] gap-x-4 gap-y-2 text-[14.5px]">
          <RequiredFor o={o} bundle={bundle} />
          {source && (
            <>
              <dt className="text-faint">Source</dt>
              <dd className="text-ink-soft">{source.resource_title}, {locator(source).toLowerCase()}</dd>
            </>
          )}
        </dl>
        <div className="mt-6">
          <EvidenceButton primary label={`Show where the ${sourceKind} says this`}
            request={{ kind: "Found by CourseFlow", claim: o.canonical_action, items: o.evidence.filter((e) => e.current_version).map(fromEvidence) }} />
        </div>
      </div>
    </article>
  );
}

function ObligationRow({ o, bundle }: { o: Obligation; bundle: CourseBundle }) {
  const source = sourceOf(o);
  return (
    <article id={`obligation-${o.id}`} data-testid="obligation" className="px-6 py-4">
      <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
        <p className="min-w-0 flex-1 text-[15.5px] font-medium">{o.canonical_action}</p>
        <span className="flex flex-wrap items-center gap-2">
          {o.modality && <span className="badge badge-neutral">{o.modality === "mandatory" ? "Mandatory" : "Recommended"}</span>}
          <StatusBadge status={o.status} />
        </span>
      </div>
      <dl className="mt-2 grid grid-cols-[6.5rem_1fr] gap-x-4 gap-y-1.5 text-[14px]">
        {source && (
          <>
            <dt className="text-faint">Source</dt>
            <dd className="text-ink-soft">{source.resource_title}, {locator(source).toLowerCase()}</dd>
          </>
        )}
        <RequiredFor o={o} bundle={bundle} />
      </dl>
      <div className="mt-3">
        <EvidenceButton label="View evidence"
          request={{ kind: "Obligation evidence", claim: o.canonical_action, items: o.evidence.filter((e) => e.current_version).map(fromEvidence) }} />
      </div>
    </article>
  );
}
