/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

import { useState } from "react";

import { Icon } from "@/components/Icon";
import { api, ApiError } from "@/lib/api";
import type { LiveRequirements } from "@/lib/types";

type State =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; result: LiveRequirements }
  | { kind: "failed"; message: string };

/** One real model call over the assessment's current brief, on request.
 * Shown beside the verified requirements, never mixed into them: the
 * answer is not saved, and a failure is shown as one, never replaced. */
export function LiveAnalysis({ assessmentId }: { assessmentId: string }) {
  const [state, setState] = useState<State>({ kind: "idle" });

  async function analyze() {
    setState({ kind: "running" });
    try {
      setState({ kind: "done", result: await api.liveRequirements(assessmentId) });
    } catch (error) {
      setState({ kind: "failed", message: error instanceof ApiError ? error.message : "The live analysis failed." });
    }
  }

  return (
    <section aria-labelledby="live-analysis-heading" className="panel space-y-4 px-6 py-5" data-testid="live-analysis">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
        <div className="min-w-0 flex-1">
          <h2 id="live-analysis-heading" className="flex flex-wrap items-center gap-2 text-[17px] font-semibold tracking-tight">
            <span className="badge badge-found"><Icon name="sparkles" size={11} />Live analysis</span>
            NVIDIA Nemotron 3.5 Lightning
          </h2>
          <p className="mt-1 text-[13.5px] text-muted">
            via Nebius Token Factory. A real model call over the current brief; the answer is shown here only and is not saved.
          </p>
        </div>
        <button type="button" className="btn btn-primary btn-sm" disabled={state.kind === "running"} onClick={analyze}>
          {state.kind === "running" ? "Analyzing with Nemotron…" : "Analyze live with Nemotron"}
        </button>
      </div>

      {state.kind === "running" && <p className="text-[14px] text-muted" role="status">Analyzing with Nemotron…</p>}
      {state.kind === "failed" && (
        <p className="flex items-center gap-2 text-[14px] text-conflict" role="alert">
          <Icon name="alert" size={14} />Live analysis failed: {state.message}
        </p>
      )}
      {state.kind === "done" && <LiveResult result={state.result} />}
    </section>
  );
}

function LiveResult({ result }: { result: LiveRequirements }) {
  const { prompt_tokens, completion_tokens } = result.usage;
  return (
    <div className="space-y-3">
      {result.requirements.length === 0 ? (
        <p className="text-[14px] text-muted">Nemotron reported no requirements in this brief.</p>
      ) : (
        <ol className="space-y-2.5">
          {result.requirements.map((r, i) => (
            <li key={i} className="rounded-xl border border-line bg-canvas/50 px-4 py-3">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <p className="max-w-prose text-[15px] text-ink">{r.requirement}</p>
                <span className={`badge ${r.quote_found ? "badge-neutral" : "badge-review"}`}>
                  {r.quote_found ? "Quote found in brief" : "Quote not found in brief"}
                </span>
              </div>
              <p className="source-quote mt-1.5 text-[13.5px] text-muted">{r.quote}</p>
            </li>
          ))}
        </ol>
      )}
      <p className="text-[12.5px] text-faint tabular-nums">
        {result.provider} · {result.model} · {(result.latency_ms / 1000).toFixed(1)} s
        {prompt_tokens !== null && completion_tokens !== null && ` · ${prompt_tokens} in / ${completion_tokens} out tokens`}
        {` · ${result.resource_title}, version ${result.version_number}`}
      </p>
    </div>
  );
}
