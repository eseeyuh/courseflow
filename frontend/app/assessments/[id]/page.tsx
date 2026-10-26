/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { useApi } from "@/components/DemoState";
import { EvidenceButton, fromEvidence, fromMapping, StatusBadge, useEvidence } from "@/components/Evidence";
import { ConflictCard, DeadlineEvidence, LoadState, deadlineText, sourcesOf } from "@/components/ui";
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
          <div className="space-y-10">
            <header className="space-y-2">
              <Link href={`/courses/${bundle.course.id}`} className="text-sm text-accent hover:underline">
                {courseName(bundle.course.title)}
              </Link>
              <div className="flex flex-wrap items-center gap-3">
                <h1 className="page-title">{assessment.title}</h1>
                <StatusBadge status={assessment.status} />
                {open.length > 0 && <span className="badge badge-conflict">Deadline conflict</span>}
              </div>
              <div className="flex flex-wrap items-center gap-x-8 gap-y-2 text-sm">
                <span>
                  <span className="text-muted">Weighting </span>
                  {assessment.weighting_percent !== null ? `${assessment.weighting_percent}%` : "not stated"}
                </span>
                <span data-testid="current-deadlines">
                  <span className="text-muted">{active.length > 1 ? "Stated deadlines " : "Deadline "}</span>
                  {active.map(deadlineText).join(" or ") || "no verified deadline"}
                </span>
                <EvidenceButton
                  label="Weighting and title source"
                  request={{ kind: "Assessment evidence", claim: assessment.title, items: assessment.evidence.map(fromEvidence) }}
                />
              </div>
            </header>

            {open.map((c) => <ConflictCard key={c.id} conflict={c} />)}
            {open.length === 0 && active.length === 1 && (
              <div className="panel flex flex-wrap items-center gap-4 px-5 py-4">
                <span className="text-muted">Deadline</span>
                <span className="font-semibold">{deadlineText(active[0])}</span>
                <span className="text-sm text-muted">{sourcesOf(active[0])}</span>
                <DeadlineEvidence deadline={active[0]} claim={assessment.title} />
              </div>
            )}
            {history.map((d) => (
              <p key={d.id} className="text-sm text-muted" data-testid="superseded-deadline">
                <span className="line-through">{deadlineText(d)}</span> ({sourcesOf(d)}) no longer counts: a newer
                source says <span className="source-quote mark">{d.superseded_by_quote}</span>.
              </p>
            ))}

            <section aria-labelledby="requirements-heading" className="space-y-3">
              <h2 id="requirements-heading" className="section-title">Requirements and where to study for them</h2>
              <p className="text-sm text-muted">
                Each requirement is quoted from the brief. The teaching material under it was retrieved
                from this course and selected by the verifier; every slide, page or section opens its exact text.
              </p>
              <ol className="space-y-3">
                {briefOrder(requirements).map(([n, r]) => <RequirementRow key={r.id} r={r} n={n} />)}
              </ol>
              {requirements.length === 0 && <p className="text-sm text-muted">No requirements were found in the brief.</p>}
            </section>

            {obligations.length > 0 && (
              <section aria-labelledby="assessment-obligations" className="space-y-3">
                <h2 id="assessment-obligations" className="section-title">Also required for this assessment</h2>
                {obligations.map((o) => (
                  <div key={o.id} className={`panel px-5 py-4 ${o.hidden ? "border-accent" : ""}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      {o.hidden && <span className="badge badge-found">Found by CourseFlow</span>}
                      <StatusBadge status={o.status} />
                    </div>
                    <p className="mt-2 font-medium">{o.canonical_action}</p>
                    <div className="mt-3">
                      <EvidenceButton
                        request={{ kind: "Obligation evidence", claim: o.canonical_action,
                          items: o.evidence.filter((e) => e.current_version).map(fromEvidence) }}
                      />
                    </div>
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
    <li className="panel px-5 py-4" data-testid="requirement">
      <div className="flex gap-4">
        <span className="mt-0.5 text-sm font-semibold text-muted">{n}</span>
        <div className="min-w-0 flex-1 space-y-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <p className="max-w-prose">{r.text.replace(/^\d+\.\s*/, "")}</p>
            <div className="flex items-center gap-2">
              <StatusBadge status={r.status} />
              <EvidenceButton label="Brief" request={{ kind: "Requirement evidence", claim: r.text, items: r.evidence.map(fromEvidence) }} />
            </div>
          </div>
          {byResource.size === 0 ? (
            <p className="text-sm text-muted">No teaching material in this course covers this requirement.</p>
          ) : (
            [...byResource.values()].map((group) => <MaterialGroup key={group[0].resource_id} group={group} requirement={r} />)
          )}
        </div>
      </div>
    </li>
  );
}

/** One teaching resource mapped to a requirement: each slide/page/section opens its exact text. */
function MaterialGroup({ group, requirement }: { group: MaterialMapping[]; requirement: Requirement }) {
  const open = useEvidence();
  const places = new Map<string, MaterialMapping[]>();
  for (const m of group) places.set(locator(m), [...(places.get(locator(m)) ?? []), m]);
  const first = group[0];
  const best = Math.min(...group.map((m) => m.retrieval_rank));
  return (
    <div className="rounded-lg bg-accent-soft/60 px-4 py-3" data-testid="material-group">
      <p className="text-sm">
        <span className="text-muted">Study: </span>
        <span className="font-medium" data-testid="material-title">{first.resource_title}</span>
        <span className="text-muted"> ({RESOURCE_LABELS[first.resource_type] ?? first.resource_type}, version {first.version_number})</span>
      </p>
      <p className="mt-1 text-xs text-muted" data-testid="material-rationale">
        Why here: {places.size === 1 ? "this place" : `these ${places.size} places`} in the course&apos;s own
        teaching material came back from {first.retrieval_mode} retrieval (best rank {best}), and the
        verifier ({first.verifier_prompt_version}) confirmed {places.size === 1 ? "it covers" : "they cover"} this requirement.
      </p>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {[...places.entries()].map(([where, mappings]) => (
          <button key={where} type="button" data-testid="material-location"
            onClick={() => open({
              kind: "Mapped teaching material",
              claim: `${where} of ${first.resource_title}, for: ${requirement.text}`,
              items: mappings.map(fromMapping),
            })}
            className="rounded-md border border-accent/30 bg-white px-2 py-0.5 text-sm text-accent hover:border-accent">
            {where}
          </button>
        ))}
      </div>
    </div>
  );
}
