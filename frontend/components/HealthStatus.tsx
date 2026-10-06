/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

"use client";

// Client Component: it runs in the browser so that it exercises the real
// browser -> API path (CORS included) and can update as the check resolves.

import { useEffect, useState } from "react";

import { checkHealth, type HealthCheck } from "@/lib/api";
import { apiBaseUrl } from "@/lib/config";

export function HealthStatus() {
  const [attempt, setAttempt] = useState(0);
  // null means "checking".
  const [result, setResult] = useState<HealthCheck | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    checkHealth(controller.signal).then((outcome) => {
      if (!controller.signal.aborted) setResult(outcome);
    });
    // Cancel the in-flight request on unmount or before a re-check.
    return () => controller.abort();
  }, [attempt]);

  function recheck() {
    setResult(null);
    setAttempt((n) => n + 1);
  }

  const view = describe(result);

  return (
    <section
      aria-labelledby="api-status-heading"
      className="rounded-lg border border-neutral-300 p-4 dark:border-neutral-700"
    >
      <h2 id="api-status-heading" className="text-sm font-semibold uppercase tracking-wide">
        API status
      </h2>
      <p role="status" aria-live="polite" className="mt-2 flex items-center gap-2">
        <span aria-hidden="true" className={`inline-block h-2.5 w-2.5 rounded-full ${view.dot}`} />
        <span data-health-state={result?.state ?? "checking"}>{view.label}</span>
      </p>
      {view.detail && (
        <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">{view.detail}</p>
      )}
      <p className="mt-1 text-xs text-neutral-500">Endpoint: {apiBaseUrl ?? "not configured"}</p>
      <button
        type="button"
        onClick={recheck}
        disabled={result === null}
        className="mt-3 rounded border border-neutral-400 px-3 py-1 text-sm disabled:opacity-50"
      >
        Check again
      </button>
    </section>
  );
}

function describe(result: HealthCheck | null): { label: string; detail?: string; dot: string } {
  if (result === null) {
    return { label: "Checking…", dot: "bg-neutral-400" };
  }
  switch (result.state) {
    case "healthy":
      return {
        label: "Connected: API and database are healthy",
        detail: `Version ${result.health.version} · ${result.health.environment}`,
        dot: "bg-green-600",
      };
    case "degraded": {
      const failing = result.health
        ? Object.entries(result.health.checks)
            .filter(([, status]) => status !== "ok")
            .map(([name]) => name)
            .join(", ")
        : "";
      return {
        label: `API reachable but not ready (HTTP ${result.httpStatus})`,
        detail: failing ? `Unavailable: ${failing}` : undefined,
        dot: "bg-amber-500",
      };
    }
    case "unreachable":
      return { label: "API unreachable", detail: result.reason, dot: "bg-red-600" };
    case "misconfigured":
      return { label: "Frontend misconfigured", detail: result.reason, dot: "bg-red-600" };
  }
}
