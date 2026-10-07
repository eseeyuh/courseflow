/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import { apiBaseUrl } from "@/lib/config";

/** Mirrors the backend HealthResponse schema (GET /api/v1/health). */
export type HealthResponse = {
  status: "ok" | "unavailable";
  version: string;
  environment: string;
  checks: Record<string, "ok" | "unavailable">;
};

/** Every outcome of a health check is a value; this never throws. */
export type HealthCheck =
  | { state: "healthy"; health: HealthResponse }
  // The CourseFlow API answered, but reports a dependency as unavailable.
  | { state: "degraded"; httpStatus: number; health: HealthResponse }
  // Something answered, but not with a CourseFlow health payload (wrong URL,
  // wrong server, proxy error page).
  | { state: "unexpected"; httpStatus: number; reason: string }
  | { state: "unreachable"; reason: string }
  | { state: "misconfigured"; reason: string };

const HEALTH_TIMEOUT_MS = 5000;

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    (candidate.status === "ok" || candidate.status === "unavailable") &&
    typeof candidate.version === "string" &&
    typeof candidate.environment === "string" &&
    typeof candidate.checks === "object" &&
    candidate.checks !== null
  );
}

/** Caller cancellation OR timeout, without relying on AbortSignal.any
 * (missing in some older browsers). */
function withTimeout(signal: AbortSignal, ms: number): { signal: AbortSignal; cancel: () => void } {
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(new DOMException("Health check timed out", "TimeoutError")),
    ms,
  );
  const onAbort = () => controller.abort(signal.reason);
  if (signal.aborted) onAbort();
  else signal.addEventListener("abort", onAbort, { once: true });
  return {
    signal: controller.signal,
    cancel: () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
    },
  };
}

/**
 * Calls the API health endpoint from the browser.
 *
 * Bounded by a timeout so the UI can never stay in a loading state. Network
 * errors and CORS rejections both surface as "unreachable": the browser
 * deliberately does not tell scripts which of the two happened.
 */
export async function checkHealth(signal: AbortSignal): Promise<HealthCheck> {
  if (apiBaseUrl === null) {
    return {
      state: "misconfigured",
      reason: "NEXT_PUBLIC_API_BASE_URL is missing or invalid (see frontend/.env.example).",
    };
  }

  const url = `${apiBaseUrl}/api/v1/health`;
  const bounded = withTimeout(signal, HEALTH_TIMEOUT_MS);
  try {
    let response: Response;
    try {
      response = await fetch(url, {
        signal: bounded.signal,
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
    } catch (error) {
      if (!signal.aborted) console.warn("[health] request failed", error);
      const timedOut = bounded.signal.reason instanceof DOMException &&
        bounded.signal.reason.name === "TimeoutError";
      return {
        state: "unreachable",
        reason: timedOut
          ? `No response within ${HEALTH_TIMEOUT_MS / 1000} s.`
          : "Network error or request blocked (API down, wrong URL, or CORS).",
      };
    }

    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null; // not JSON: handled as "unexpected" below
    }

    if (!isHealthResponse(body)) {
      return {
        state: "unexpected",
        httpStatus: response.status,
        reason: `${url} did not return a CourseFlow health response. Check NEXT_PUBLIC_API_BASE_URL.`,
      };
    }
    if (response.ok && body.status === "ok") {
      return { state: "healthy", health: body };
    }
    return { state: "degraded", httpStatus: response.status, health: body };
  } finally {
    bounded.cancel();
  }
}
