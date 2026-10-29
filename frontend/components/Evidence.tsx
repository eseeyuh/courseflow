/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// The evidence viewer: stored provenance only. Every field shown comes from
// the API (source title, version, slide/page/section, the exact excerpt,
// verification status); nothing is composed into a citation here.

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { Icon } from "@/components/Icon";
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
  /** Shown instead of a verification status (e.g. a verifier-selected mapping). */
  origin?: string;
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
    origin: "Selected by the verifier",
    note: `Found by ${m.retrieval_mode} retrieval; the verifier (${m.verifier_prompt_version}) chose it from the retrieved candidates only.`,
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
    origin: "Mapped teaching material",
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
    origin: "Source text",
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
    // Focus the drawer, and give focus back to what opened it on close.
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButton.current?.focus();
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      opener?.focus();
    };
  }, [request, close]);
  return (
    <Context.Provider value={setRequest}>
      {children}
      {request && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/55 backdrop-blur-[2px]" onClick={close}>
          <aside
            role="dialog"
            aria-modal="true"
            aria-label="Evidence"
            onClick={(event) => event.stopPropagation()}
            className="flex h-full w-full max-w-[600px] flex-col overflow-y-auto border-l border-line bg-surface shadow-lift"
          >
            <header className="sticky top-0 z-10 flex items-center justify-between gap-4 border-b border-line bg-surface/95 px-7 py-3.5 backdrop-blur">
              <p className="eyebrow text-muted"><Icon name="quote" size={14} className="text-link" />{request.kind}</p>
              <button ref={closeButton} type="button" onClick={close} className="btn btn-quiet btn-sm">
                Close
              </button>
            </header>
            <section className="border-b border-line px-7 pt-6 pb-6" aria-label="Derived claim">
              <p className="label">What CourseFlow states</p>
              <p className="mt-2 text-[18px] leading-snug font-semibold tracking-tight" data-testid="evidence-claim">{request.claim}</p>
            </section>
            <section className="px-7 py-6" aria-label="Source evidence">
              <p className="label">
                {request.items.length === 1 ? "Exact source excerpt" : `Exact source excerpts (${request.items.length})`}
              </p>
              <ol className="mt-3 space-y-5">
                {request.items.map((item) => (
                  <li key={item.key} data-testid="evidence-item" className="overflow-hidden rounded-xl border border-line bg-canvas/40">
                    <blockquote className="source-quote border-l-[3px] border-mark-strong bg-mark/40 px-5 py-4 text-[16px] text-ink" data-testid="evidence-quote">
                      {item.quote}
                    </blockquote>
                    <div className="px-5 py-4">
                      <p className="flex items-start gap-2 text-[14px] font-medium">
                        <Icon name="file" size={15} className="mt-0.5 text-faint" />
                        {item.resourceTitle}
                      </p>
                      <dl className="mt-3 grid grid-cols-[6.5rem_1fr] gap-x-4 gap-y-1.5 text-[13px]">
                        {item.resourceType && (<><dt className="text-faint">Type</dt><dd className="text-ink-soft">{RESOURCE_LABELS[item.resourceType] ?? item.resourceType}</dd></>)}
                        <dt className="text-faint">Version</dt>
                        <dd className="text-ink-soft">
                          {item.versionNumber}
                          {item.currentVersion === false && <span className="text-review"> (a newer version exists)</span>}
                        </dd>
                        <dt className="text-faint">Location</dt>
                        <dd className="font-medium text-ink" data-testid="evidence-locator">{locator(item)}</dd>
                        {item.section_path && item.slide_number === null && item.page_number !== null && (
                          <><dt className="text-faint">Section</dt><dd className="text-ink-soft">{item.section_path}</dd></>
                        )}
                        <dt className="text-faint">Status</dt>
                        <dd>
                          {item.status ? <StatusBadge status={item.status} /> : <span className="badge badge-neutral">{item.origin ?? "Source text"}</span>}
                        </dd>
                      </dl>
                    </div>
                    {item.note && <p className="border-t border-line px-5 py-2.5 text-[12px] text-faint">{item.note}</p>}
                  </li>
                ))}
              </ol>
            </section>
          </aside>
        </div>
      )}
    </Context.Provider>
  );
}

export function StatusBadge({ status }: { status: Verification }) {
  return status === "verified" ? (
    <span className="badge badge-verified"><Icon name="check" size={11} strokeWidth={2.6} />Verified</span>
  ) : (
    <span className="badge badge-review">Needs review</span>
  );
}

/** A button that opens the evidence viewer. */
export function EvidenceButton({ request, label = "View evidence", primary = false }: {
  request: EvidenceRequest;
  label?: string;
  primary?: boolean;
}) {
  const open = useEvidence();
  if (request.items.length === 0) return null;
  return (
    <button type="button" onClick={() => open(request)} className={primary ? "btn btn-primary" : "evidence-button"}>
      <Icon name="quote" size={primary ? 14 : 12} className={primary ? "" : "text-link/80"} />
      {label}
    </button>
  );
}
