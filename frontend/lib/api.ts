/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

import { apiBaseUrl } from "@/lib/config";
import type {
  Assessment,
  BriefText,
  Conflict,
  Course,
  DemoSyncResult,
  DemoWorkflowStarted,
  DependencyEdge,
  LiveRequirements,
  Obligation,
  Requirement,
  StudyPlan,
  StudyState,
  Sync,
  WorkflowRun,
} from "@/lib/types";

// --- the CourseFlow API -------------------------------------------------------------------

/** A failed API request: an HTTP status (0 when nothing answered) and the
 * server's detail when it sent one. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const READ_TIMEOUT_MS = 20_000;
// The demo workflow and sync run the offline pipeline: about ten seconds.
const DEMO_TIMEOUT_MS = 180_000;
// One live model call, with the backend's own bounded retries behind it.
const LIVE_TIMEOUT_MS = 180_000;

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = READ_TIMEOUT_MS): Promise<T> {
  if (apiBaseUrl === null) {
    throw new ApiError(0, "NEXT_PUBLIC_API_BASE_URL is missing or invalid (see frontend/.env.example).");
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}/api/v1${path}`, {
      ...init,
      signal: controller.signal,
      cache: "no-store",
      headers: { Accept: "application/json", ...(init.body ? { "Content-Type": "application/json" } : {}) },
    });
  } catch {
    throw new ApiError(0, controller.signal.aborted
      ? `The API did not answer within ${timeoutMs / 1000} s.`
      : "The API is unreachable. Is it running?");
  } finally {
    clearTimeout(timer);
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof body === "object" && body !== null && "detail" in body ? (body as { detail: unknown }).detail : null;
    throw new ApiError(response.status, typeof detail === "string" ? detail : `HTTP ${response.status}`);
  }
  return body as T;
}

export const api = {
  courses: () => request<Course[]>("/courses"),
  assessments: (courseId: string) => request<Assessment[]>(`/courses/${courseId}/assessments`),
  assessment: (id: string) => request<Assessment>(`/assessments/${id}`),
  assessmentRequirements: (id: string) => request<Requirement[]>(`/assessments/${id}/requirements`),
  assessmentBrief: (id: string) => request<BriefText>(`/assessments/${id}/brief`),
  assessmentConflicts: (id: string) => request<Conflict[]>(`/assessments/${id}/conflicts`),
  obligations: (courseId: string) => request<Obligation[]>(`/courses/${courseId}/obligations`),
  dependencies: (courseId: string) => request<DependencyEdge[]>(`/courses/${courseId}/dependencies`),
  conflicts: (courseId: string) => request<Conflict[]>(`/courses/${courseId}/conflicts`),
  workflowRuns: (courseId: string, limit = 1) =>
    request<WorkflowRun[]>(`/courses/${courseId}/workflow-runs?limit=${limit}`),
  studyPlan: () => request<StudyPlan>("/study-plan"),
  changes: () => request<Sync[]>("/changes"),
  setStudyState: (spanIds: string[], state: StudyState) =>
    request<{ changed: number }>("/study-states", {
      method: "PUT",
      body: JSON.stringify({ source_span_ids: spanIds, state }),
    }),
  resetDemo: () =>
    request<DemoWorkflowStarted>("/demo/workflow-runs", { method: "POST", body: JSON.stringify({ reset: true }) },
      DEMO_TIMEOUT_MS),
  syncDemo: () => request<DemoSyncResult>("/demo/sync", { method: "POST" }, DEMO_TIMEOUT_MS),
  liveRequirements: (assessmentId: string) =>
    request<LiveRequirements>(`/assessments/${assessmentId}/live-requirements`, { method: "POST" }, LIVE_TIMEOUT_MS),
};
