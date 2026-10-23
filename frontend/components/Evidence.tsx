/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// The evidence viewer: stored provenance only. Every field shown comes from
// the API (source title, version, slide/page/section, the exact excerpt,
// verification status); nothing is composed into a citation here.

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { locator, RESOURCE_LABELS } from "@/lib/format";
import type { Evidence, MaterialMapping, SourceQuote, StudyTarget, Verification } from "@/lib/types";

export type EvidenceView = {
  key: string;
  resourceTitle: string;
  resourceType?: string;
  versionNumber: number;
  currentVersion?: boolean;
  page_number: number | null;
  slide_number: number | null;
  section_path: string | null;
  quote: string;
  status?: Verification | null;
  note?: string;
};

export type EvidenceRequest = { claim: string; kind: string; items: EvidenceView[] };

export function fromEvidence(e: Evidence): EvidenceView {
  return {
    key: e.id,
    resourceTitle: e.resource_title,
    resourceType: e.resource_type,
    versionNumber: e.version_number,
    currentVersion: e.current_version,
    page_number: e.page_number,
    slide_number: e.slide_number,
    section_path: e.section_path,
    quote: e.quote,
    status: e.verification_status,
    note: e.model ? `Extracted by ${e.prompt_version ?? "a recorded prompt"}; checked by the evidence gate.` : undefined,
  };
}

export function fromMapping(m: MaterialMapping): EvidenceView {
  return {
    key: m.id,
    resourceTitle: m.resource_title,
    resourceType: m.resource_type,
    versionNumber: m.version_number,
    currentVersion: m.current_version,
    page_number: m.page_number,
    slide_number: m.slide_number,
    section_path: m.section_path,
    quote: m.quote,
    status: null,
    note: `Retrieved (${m.retrieval_mode}, rank ${m.retrieval_rank}) and selected by the verifier (${m.verifier_prompt_version}). ${m.rationale}.`,
  };
}

export function fromTarget(t: StudyTarget): EvidenceView {
  return {
    key: t.source_span_id,
    resourceTitle: t.resource_title,
    versionNumber: t.version_number,
    currentVersion: t.current_version,
    page_number: t.page_number,
    slide_number: t.slide_number,
    section_path: t.section_path,
    quote: t.quote,
    status: null,
  };
}

export function fromSource(s: SourceQuote): EvidenceView {
  return {
    key: `${s.source_span_id}:${s.quote}`,
    resourceTitle: s.resource_title,
    versionNumber: s.version_number,
    page_number: s.page_number,
    slide_number: s.slide_number,
    section_path: s.section_path,
    quote: s.quote,
    status: null,
  };
}

const Context = createContext<(request: EvidenceRequest) => void>(() => {});

export function useEvidence(): (request: EvidenceRequest) => void {
  return useContext(Context);
}

export function EvidenceProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<EvidenceRequest | null>(null);
  const close = useCallback(() => setRequest(null), []);
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!request) return;
    closeButton.current?.focus();
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [request, close]);
  return (
    <Context.Provider value={setRequest}>
      {children}
      {request && (
        <div className="fixed inset-0 z-40 flex justify-end bg-ink/30" onClick={close}>
          <aside
            role="dialog"
            aria-modal="true"
            aria-label="Evidence"
            onClick={(event) => event.stopPropagation()}
            className="flex h-full w-full max-w-xl flex-col overflow-y-auto bg-white shadow-2xl"
          >
            <header className="sticky top-0 border-b border-line bg-white px-6 py-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="text-sm text-muted">{request.kind}</p>
                  <h2 className="mt-1 text-lg font-semibold leading-snug">{request.claim}</h2>
                </div>
                <button ref={closeButton} type="button" onClick={close}
                  className="rounded-md border border-line px-3 py-1 text-sm hover:bg-paper">
                  Close
                </button>
              </div>
            </header>
            <ol className="space-y-5 px-6 py-5">
              {request.items.map((item) => (
                <li key={item.key} data-testid="evidence-item" className="space-y-2">
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <span className="font-medium">{item.resourceTitle}</span>
                    {item.status && <StatusBadge status={item.status} />}
                  </div>
                  <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-sm text-muted">
                    {item.resourceType && (<><dt>Source type</dt><dd>{RESOURCE_LABELS[item.resourceType] ?? item.resourceType}</dd></>)}
                    <dt>Version</dt>
                    <dd>
                      {item.versionNumber}
                      {item.currentVersion === false && " (superseded by a newer version)"}
                    </dd>
                    <dt>Location</dt>
                    <dd data-testid="evidence-locator">{locator(item)}</dd>
                    {item.section_path && (<><dt>Section</dt><dd>{item.section_path}</dd></>)}
                  </dl>
                  <blockquote className="source-quote" data-testid="evidence-quote">{item.quote}</blockquote>
                  {item.note && <p className="text-xs text-muted">{item.note}</p>}
                </li>
              ))}
            </ol>
          </aside>
        </div>
      )}
    </Context.Provider>
  );
}

export function StatusBadge({ status }: { status: Verification }) {
  return status === "verified" ? (
    <span className="badge badge-verified">Verified</span>
  ) : (
    <span className="badge badge-review">Needs review</span>
  );
}

/** A button that opens the evidence viewer. */
export function EvidenceButton({ request, label = "View evidence" }: { request: EvidenceRequest; label?: string }) {
  const open = useEvidence();
  if (request.items.length === 0) return null;
  return (
    <button type="button" onClick={() => open(request)} className="evidence-button">
      {label}
    </button>
  );
}
