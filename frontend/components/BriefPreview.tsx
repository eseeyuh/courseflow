/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// The assignment brief as a document: its file card, a centred preview and
// a download. Presentation only. The text is the brief's own stored text as
// the API returns it, page by page (or, when no brief is stored, its cited
// excerpts); nothing is written here, and the file is built from that same
// text in the browser (no file storage, no parsing).

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { Icon } from "@/components/Icon";
import type { Assessment, BriefText, Evidence, Requirement } from "@/lib/types";

type Block = { key: string; text: string; page: number | null; heading?: boolean };

export type BriefDocument = {
  title: string;
  versionNumber: number;
  pages: { page: number | null; blocks: Block[] }[];
  full: boolean; // the whole stored text, not only the cited excerpts
};

const TERMINAL = /[.!?:;)]$/;
const FIELD = /^[A-Z][\w -]{0,30}: /; // "Weighting: 40% …"
const headingLike = (line: string) => !TERMINAL.test(line) && line.length <= 60;

/** A stored page's lines (one per PDF line) back into headings and
 * paragraphs: a wrapped line continues its paragraph; a short line that
 * ends a sentence, a "Field: value" line or a heading starts a new block. */
function paragraphs(text: string): { text: string; heading: boolean }[] {
  const blocks: { text: string; heading: boolean }[] = [];
  let previous = "";
  for (const line of text.split("\n").map((l) => l.trim()).filter(Boolean)) {
    const last = blocks.at(-1);
    const ended = TERMINAL.test(previous) && (previous.length < 70 || headingLike(line));
    if (last && !last.heading && !FIELD.test(line) && !ended) last.text += ` ${line}`;
    else blocks.push({ text: line, heading: headingLike(line) });
    previous = line;
  }
  return blocks;
}

/** The brief: its whole stored text when the API has it, else the stored
 * excerpts of the one resource the requirements cite. */
export function briefDocument(assessment: Assessment, requirements: Requirement[], text?: BriefText | null): BriefDocument | null {
  if (text) {
    return {
      title: text.resource_title,
      versionNumber: text.version_number,
      pages: text.pages.map((p, n) => ({
        page: p.page_number,
        blocks: paragraphs(p.text).map((b, i) => ({ key: `${n}-${i}`, text: b.text, page: p.page_number, heading: b.heading })),
      })),
      full: true,
    };
  }
  const cited = [
    ...assessment.evidence,
    ...assessment.deadlines.filter((d) => d.active).flatMap((d) => d.evidence),
    ...requirements.flatMap((r) => r.evidence),
  ];
  const source = requirements.flatMap((r) => r.evidence)[0] ?? assessment.evidence[0];
  if (!source) return null;
  const own = cited.filter((e) => e.resource_id === source.resource_id && e.resource_version_id === source.resource_version_id);
  // One block per distinct span; a quote inside a longer one is shown there.
  const unique: Evidence[] = [];
  for (const e of own) if (!unique.some((u) => u.quote === e.quote)) unique.push(e);
  const kept = unique.filter((e) => !unique.some((u) => u !== e && u.quote.includes(e.quote)));
  const order = (e: Evidence) => e.page_number ?? Number.MAX_SAFE_INTEGER;
  const blocks = kept
    .map((e, i) => ({ e, i }))
    .sort((a, b) => order(a.e) - order(b.e) || a.i - b.i)
    .map(({ e }) => ({ key: e.id, text: e.quote.trim(), page: e.page_number }));
  const pages: BriefDocument["pages"] = [];
  for (const block of blocks) {
    const last = pages.at(-1);
    if (last && last.page === block.page) last.blocks.push(block);
    else pages.push({ page: block.page, blocks: [block] });
  }
  return {
    title: source.resource_title,
    versionNumber: source.version_number,
    pages,
    full: false,
  };
}

function fileName(doc: BriefDocument): string {
  return `${doc.title.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_|_$/g, "")}.pdf`;
}

/** A one-font PDF of the document's text: valid, small, made in the browser. */
function briefPdf(doc: BriefDocument): Blob {
  const ascii = (s: string) => s.replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/[–—]/g, "-")
    .replace(/…/g, "...").replace(/[^\x20-\x7e\n]/g, "");
  const escape = (s: string) => s.replace(/[\\()]/g, (c) => `\\${c}`);
  const wrap = (text: string, width: number) => text.split("\n").flatMap((line) => {
    const out: string[] = [];
    let current = "";
    for (const word of line.split(/\s+/).filter(Boolean)) {
      if (current && current.length + word.length + 1 > width) { out.push(current); current = word; }
      else current = current ? `${current} ${word}` : word;
    }
    return [...out, current];
  });
  const lines: { text: string; font: "F1" | "F2"; size: number; gap: number }[] = [
    // The full text opens with its own title.
    ...(doc.full ? [] : [
      ...wrap(ascii(doc.title), 52).map((text) => ({ text, font: "F2" as const, size: 17, gap: 22 })),
      { text: "Assignment brief", font: "F1" as const, size: 10, gap: 26 },
    ]),
    ...doc.pages.flatMap((p) => p.blocks.flatMap((b) => [
      ...wrap(ascii(b.text), 92).map((text) => ({ text, font: b.heading ? "F2" as const : "F1" as const, size: 11, gap: 15 })),
      { text: "", font: "F1" as const, size: 11, gap: 9 },
    ])),
  ];
  const streams: string[] = [];
  let ops: string[] = [];
  let y = 780;
  for (const line of lines) {
    if (y - line.gap < 56) { streams.push(ops.join("\n")); ops = []; y = 780; }
    y -= line.gap;
    if (line.text) ops.push(`BT /${line.font} ${line.size} Tf 56 ${y} Td (${escape(line.text)}) Tj ET`);
  }
  streams.push(ops.join("\n"));
  const pageIds = streams.map((_, i) => 5 + i * 2);
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    `<< /Type /Pages /Kids [${pageIds.map((id) => `${id} 0 R`).join(" ")}] /Count ${streams.length} >>`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ...streams.flatMap((stream, i) => [
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents ${6 + i * 2} 0 R >>`,
      `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
    ]),
  ];
  let body = "%PDF-1.4\n";
  const offsets: number[] = [];
  objects.forEach((object, i) => {
    offsets.push(body.length);
    body += `${i + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = body.length;
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  body += offsets.map((o) => `${String(o).padStart(10, "0")} 00000 n \n`).join("");
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return new Blob([body], { type: "application/pdf" });
}

function size(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

function download(doc: BriefDocument) {
  const url = URL.createObjectURL(briefPdf(doc));
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName(doc);
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** The brief as a downloadable file. */
export function BriefFile({ doc }: { doc: BriefDocument }) {
  const bytes = useMemo(() => briefPdf(doc).size, [doc]);
  return (
    <div className="panel flex flex-wrap items-center gap-x-5 gap-y-3 px-6 py-4" data-testid="brief-file">
      <span className="relative grid h-14 w-11 shrink-0 place-items-center rounded-md border border-line bg-canvas text-conflict">
        <Icon name="file" size={20} />
        <span className="absolute -bottom-1.5 rounded bg-conflict px-1 text-[9.5px] font-bold tracking-wide text-white">PDF</span>
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-[16px] font-semibold tracking-tight">{doc.title}</p>
        <p className="mt-0.5 text-[13.5px] text-muted">
          Assignment brief · PDF · {size(bytes)} · version {doc.versionNumber}
        </p>
      </div>
      <button type="button" onClick={() => download(doc)} className="btn btn-quiet">
        <Icon name="download" size={15} />Download brief
      </button>
    </div>
  );
}

/** The quotes, marked wherever they occur in the text (a quote's line
 * breaks match the reflowed paragraph's spaces). */
function marked(text: string, quotes: string[]): ReactNode[] {
  const hits = quotes
    .map((q) => q.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .flatMap((q) => (text.includes(q) ? [{ at: text.indexOf(q), end: text.indexOf(q) + q.length }] : []))
    .sort((a, b) => a.at - b.at);
  const out: ReactNode[] = [];
  let from = 0;
  for (const hit of hits) {
    if (hit.at < from) continue;
    out.push(text.slice(from, hit.at));
    out.push(<mark key={hit.at} data-testid="brief-highlight"
      className="rounded-[3px] bg-amber-300/70 px-0.5 text-[#1c1917] box-decoration-clone">{text.slice(hit.at, hit.end)}</mark>);
    from = hit.end;
  }
  out.push(text.slice(from));
  return out;
}

/** The "Brief" button: opens the brief, centred, at the quoted passage. */
export function BriefButton({ doc, quotes, label = "Brief" }: { doc: BriefDocument; quotes: string[]; label?: string }) {
  const [open, setOpen] = useState(false);
  const close = useRef<HTMLButtonElement>(null);
  const sheet = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    close.current?.focus();
    sheet.current?.querySelector("mark")?.scrollIntoView({ block: "center" });
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      opener?.focus();
    };
  }, [open]);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} className="evidence-button">
        <Icon name="quote" size={12} className="text-link/80" />{label}
      </button>
      {open && createPortal(
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-6 backdrop-blur-[2px]" onClick={() => setOpen(false)}>
          <div role="dialog" aria-modal="true" aria-label="Brief preview" onClick={(event) => event.stopPropagation()}
            className="flex h-[min(88vh,960px)] w-full max-w-[920px] flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-lift">
            <header className="flex items-center gap-4 border-b border-line px-6 py-4">
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-conflict-soft text-conflict ring-1 ring-conflict-line">
                <Icon name="file" size={18} />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-[16.5px] font-semibold tracking-tight">{doc.title}</p>
                <p className="text-[13px] text-muted">Assignment brief · PDF · version {doc.versionNumber}</p>
              </div>
              <button type="button" onClick={() => download(doc)} className="btn btn-quiet btn-sm">
                <Icon name="download" size={14} />Download
              </button>
              <button ref={close} type="button" onClick={() => setOpen(false)} className="btn btn-quiet btn-sm">Close</button>
            </header>
            <div ref={sheet} className="flex-1 space-y-6 overflow-y-auto bg-canvas px-6 py-8">
              {doc.pages.map((p, i) => (
                <article key={`${p.page}-${i}`}
                  className="mx-auto max-w-[680px] rounded-sm bg-[#f6f3ec] px-14 pt-14 pb-10 font-serif text-[16px] leading-[1.7] text-[#292524] shadow-[0_12px_40px_rgb(0_0_0/0.45)]">
                  {i === 0 && !doc.full && (
                    <header className="mb-8 border-b border-[#d6d3cb] pb-5">
                      <p className="font-sans text-[11px] font-semibold tracking-[0.14em] text-[#78716c] uppercase">Assignment brief</p>
                      <h2 className="mt-2 text-[27px] leading-tight text-[#1c1917]">{doc.title}</h2>
                    </header>
                  )}
                  <div className="space-y-4">
                    {p.blocks.map((b) => b.heading
                      ? <p key={b.key} className="pt-2 font-sans text-[14px] font-semibold text-[#1c1917]">{marked(b.text, quotes)}</p>
                      : <p key={b.key} className="whitespace-pre-line">{marked(b.text, quotes)}</p>)}
                  </div>
                  {p.page !== null && <p className="mt-10 text-center font-sans text-[11px] text-[#a8a29e]">Page {p.page}</p>}
                </article>
              ))}
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
