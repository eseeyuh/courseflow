/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { useApi } from "@/components/DemoState";
import { EvidenceButton, fromEvidence, fromMapping, StatusBadge, useEvidence } from "@/components/Evidence";
import { Icon } from "@/components/Icon";
import { ConflictCard, CourseDot, DeadlineEvidence, LoadState, deadlineText, sourcesOf } from "@/components/ui";
import { api } from "@/lib/api";
import { courseName, locator, RESOURCE_LABELS } from "@/lib/format";
import { loadCourseById } from "@/lib/load";
import type { MaterialMapping, Requirement } from "@/lib/types";

async function loadAssessment(id: string) {
  const assessment = await api.assessment(id);
  const [requirements, conflicts, bundle] = await Promise.all([
    api.assessmentRequirements(id),
    api.assessmentConflicts(id),
    loadCourseById(assessment.course_id),
  ]);
  return { assessment, requirements, conflicts, bundle };
}

export default function AssessmentPage() {
  const { id } = useParams<{ id: string }>();
  const result = useApi(() => loadAssessment(id), `assessment:${id}`);
  return (
    <LoadState result={result}>
      {({ assessment, requirements, conflicts, bundle }) => {
        const active = assessment.deadlines.filter((d) => d.active);
        const history = assessment.deadlines.filter((d) => !d.active && d.superseded_by_deadline_id);
        const open = conflicts.filter((c) => c.status === "open");
        const obligations = bundle.obligations.filter((o) => o.assessment_id === assessment.id && o.status === "verified");
        return (
          <div className="space-y-12">
            <header className="animate-fade-up">
              <Link href={`/courses/${bundle.course.id}`} className="inline-flex items-center gap-2 text-[13px] font-medium text-muted hover:text-link">
                <CourseDot course={bundle.course} />
                {courseName(bundle.course.title)}
                <Icon name="chevronRight" size={12} className="text-faint" />
                <span className="text-faint">Assessment</span>
              </Link>
              <div className="mt-2 flex flex-wrap items-center gap-3">
                <h1 className="page-title">{assessment.title}</h1>
                <StatusBadge status={assessment.status} />
                {open.length > 0 && <span className="badge badge-conflict"><Icon name="alert" size={11} />Deadline conflict</span>}
              </div>
              <div className="mt-4 flex flex-wrap items-center gap-x-8 gap-y-2 text-[14.5px]">
                <span>
                  <span className="text-faint">Weighting </span>
                  <span className="font-medium">{assessment.weighting_percent !== null ? `${assessment.weighting_percent}%` : "not stated"}</span>
                </span>
                <span data-testid="current-deadlines">
                  <span className="text-faint">{active.length > 1 ? "Stated deadlines " : "Deadline "}</span>
                  <span className="font-medium tabular-nums">{active.map(deadlineText).join(" or ") || "no verified deadline"}</span>
                </span>
                <EvidenceButton
                  label="Weighting and title source"
                  request={{ kind: "Assessment evidence", claim: assessment.title, items: assessment.evidence.map(fromEvidence) }}
                />
              </div>
            </header>

            {open.map((c) => <ConflictCard key={c.id} conflict={c} />)}
            {open.length === 0 && active.length === 1 && (
              <div className="panel flex flex-wrap items-center gap-x-5 gap-y-2 px-6 py-4">
                <span className="label">Deadline</span>
                <span className="text-[19px] font-semibold tabular-nums">{deadlineText(active[0])}</span>
                <span className="text-[14px] text-muted">{sourcesOf(active[0])}</span>
                <span className="ml-auto"><DeadlineEvidence deadline={active[0]} claim={assessment.title} /></span>
              </div>
            )}
            {history.map((d) => (
              <div key={d.id} className="panel flex flex-wrap items-baseline gap-x-4 gap-y-1.5 px-6 py-3.5 text-[14.5px]" data-testid="superseded-deadline">
                <span className="badge badge-neutral">Replaced deadline</span>
                <span><span className="line-through decoration-faint">{deadlineText(d)}</span> <span className="text-faint">({sourcesOf(d)})</span></span>
                <span className="text-muted">
                  no longer counts: a newer source says <span className="source-quote mark text-ink">{d.superseded_by_quote}</span>
                </span>
                <span className="ml-auto"><DeadlineEvidence deadline={d} claim={`Replaced ${assessment.title} deadline`} label="Original source" /></span>
              </div>
            ))}

            <section aria-labelledby="requirements-heading" className="space-y-4">
              <div>
                <h2 id="requirements-heading" className="text-[23px] font-semibold tracking-tight">Requirements and where to study for them</h2>
                <p className="mt-1 max-w-3xl text-[14.5px] text-muted">
                  Each requirement is quoted from the brief. Under it: where the course&apos;s own teaching material
                  covers it. Open any slide, page or section to see its exact text.
                </p>
              </div>
              <ol className="space-y-3">
                {briefOrder(requirements).map(([n, r]) => <RequirementRow key={r.id} r={r} n={n} />)}
              </ol>
              {requirements.length === 0 && <p className="text-[14.5px] text-muted">No requirements were found in the brief.</p>}
            </section>

            {obligations.length > 0 && (
              <section aria-labelledby="assessment-obligations" className="space-y-3">
                <h2 id="assessment-obligations" className="label">Also required for this assessment</h2>
                {obligations.map((o) => (
                  <div key={o.id} className={`panel flex flex-wrap items-center gap-x-4 gap-y-3 px-6 py-4 ${o.hidden ? "border-accent-line" : ""}`}>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        {o.hidden && <span className="badge badge-found"><Icon name="sparkles" size={11} />Found by CourseFlow</span>}
                        <StatusBadge status={o.status} />
                      </div>
                      <p className="mt-2 text-[15.5px] font-medium">{o.canonical_action}</p>
                    </div>
                    <EvidenceButton
                      request={{ kind: "Obligation evidence", claim: o.canonical_action,
                        items: o.evidence.filter((e) => e.current_version).map(fromEvidence) }}
                    />
                  </div>
                ))}
              </section>
            )}
          </div>
        );
      }}
    </LoadState>
  );
}

/** The brief's own numbering ("2. Discuss…") when it has one, else list order. */
function briefOrder(requirements: Requirement[]): [number, Requirement][] {
  const numbered = requirements.map((r, i): [number, Requirement] => [Number(/^(\d+)\./.exec(r.text)?.[1] ?? i + 1), r]);
  return numbered.sort((a, b) => a[0] - b[0]);
}

function RequirementRow({ r, n }: { r: Requirement; n: number }) {
  const byResource = new Map<string, MaterialMapping[]>();
  for (const m of r.materials) byResource.set(m.resource_id, [...(byResource.get(m.resource_id) ?? []), m]);
  return (
    <li className="panel px-6 py-5" data-testid="requirement">
      <div className="flex gap-4">
        <span className="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-md bg-paper text-[12.5px] font-semibold text-muted ring-1 ring-line tabular-nums">{n}</span>
        <div className="min-w-0 flex-1 space-y-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <p className="max-w-prose text-[16px] leading-relaxed text-ink">{r.text.replace(/^\d+\.\s*/, "")}</p>
            <div className="flex items-center gap-2">
              <StatusBadge status={r.status} />
              <EvidenceButton label="Brief" request={{ kind: "Requirement evidence", claim: r.text, items: r.evidence.map(fromEvidence) }} />
            </div>
          </div>
          {byResource.size === 0 ? (
            <p className="text-[14px] text-faint">No teaching material in this course covers this requirement.</p>
          ) : (
            [...byResource.values()].map((group) => <MaterialGroup key={group[0].resource_id} group={group} requirement={r} />)
          )}
        </div>
      </div>
    </li>
  );
}

/** One teaching resource mapped to a requirement. Each slide, page or
 * section is listed with the start of its own stored text and opens the
 * exact excerpt; the list keeps the source's order and hides nothing. */
function MaterialGroup({ group, requirement }: { group: MaterialMapping[]; requirement: Requirement }) {
  const open = useEvidence();
  const places = new Map<string, MaterialMapping[]>();
  for (const m of group) places.set(locator(m), [...(places.get(locator(m)) ?? []), m]);
  const first = group[0];
  const requirementText = requirement.text.replace(/^\d+\.\s*/, "");
  return (
    <div className="rounded-xl border border-line bg-canvas/50 px-4 py-3.5" data-testid="material-group">
      <p className="flex flex-wrap items-baseline gap-x-2 text-[14.5px]">
        <span className="label">Study</span>
        <span className="font-semibold text-ink" data-testid="material-title">{first.resource_title}</span>
        <span className="text-[13px] text-faint">{RESOURCE_LABELS[first.resource_type] ?? first.resource_type}, version {first.version_number}</span>
      </p>
      <ul className="mt-2.5 space-y-1.5">
        {[...places.entries()].map(([where, mappings]) => {
          // A slide's title and body are separate spans: preview the first line
          // of the longer one, as stored.
          const preview = mappings.map((m) => m.quote).sort((a, b) => b.length - a.length)[0].trim().split("\n")[0];
          return (
            <li key={where}>
              <button type="button" data-testid="material-location"
                onClick={() => open({
                  kind: "Mapped teaching material",
                  claim: `${where} of “${first.resource_title}” covers: ${requirementText}`,
                  items: mappings.map(fromMapping),
                })}
                className="group flex w-full items-center gap-3 rounded-lg border border-transparent px-2 py-1.5 text-left text-[14.5px] transition-colors hover:border-accent-line hover:bg-accent-soft/60">
                <span className="shrink-0 rounded-md bg-accent-soft px-2 py-0.5 text-[13px] font-semibold text-link ring-1 ring-accent-line">{where}</span>
                <span className="min-w-0 flex-1 truncate font-serif text-ink-soft" data-testid="material-preview">{preview}</span>
                <Icon name="chevronRight" size={14} className="text-faint group-hover:text-link" />
              </button>
            </li>
          );
        })}
      </ul>
      <p className="mt-2.5 text-[12.5px] text-faint" data-testid="material-rationale">
        Why here: the verifier ({first.verifier_prompt_version}) confirmed {places.size === 1 ? "this place" : `these ${places.size} places`} in
        the course&apos;s own teaching material cover this requirement, choosing only from what {first.retrieval_mode} retrieval found.
      </p>
    </div>
  );
}
