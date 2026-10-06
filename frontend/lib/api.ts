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
  | { state: "degraded"; httpStatus: number; health: HealthResponse | null }
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

/**
 * Calls the API health endpoint from the browser.
 *
 * Bounded by a timeout so the UI can never stay in a loading state. Network
 * errors, CORS rejections and timeouts all surface as "unreachable": the
 * browser deliberately does not tell scripts which of these happened.
 */
export async function checkHealth(signal: AbortSignal): Promise<HealthCheck> {
  if (apiBaseUrl === null) {
    return {
      state: "misconfigured",
      reason: "NEXT_PUBLIC_API_BASE_URL is missing or invalid (see frontend/.env.example).",
    };
  }

  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}/api/v1/health`, {
      signal: AbortSignal.any([signal, AbortSignal.timeout(HEALTH_TIMEOUT_MS)]),
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
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
    body = null;
  }
  const health = isHealthResponse(body) ? body : null;

  if (response.ok && health?.status === "ok") {
    return { state: "healthy", health };
  }
  return { state: "degraded", httpStatus: response.status, health };
}
