/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

/** Mirrors of the backend read schemas the UI uses (app/api/schemas, app/api/v1). */

export type Uuid = string;
export type Verification = "verified" | "needs_review" | "rejected";
export type ResourceType =
  | "lecture"
  | "lab"
  | "reading"
  | "workshop"
  | "handbook"
  | "brief"
  | "announcement"
  | "page";

export type Course = {
  id: Uuid;
  title: string;
  external_id: string | null;
  status: string;
};

export type Evidence = {
  id: Uuid;
  field: string;
  verification_status: Verification;
  review_reason: string | null;
  quote: string;
  source_span_id: Uuid;
  page_number: number | null;
  slide_number: number | null;
  section_path: string | null;
  resource_id: Uuid;
  resource_title: string;
  resource_type: ResourceType;
  resource_version_id: Uuid;
  version_number: number;
  current_version: boolean;
  model: string | null;
  prompt_version: string | null;
  model_call_id: Uuid | null;
  workflow_run_id: Uuid | null;
};

export type Deadline = {
  id: Uuid;
  due_date: string;
  due_time: string | null;
  timezone: string | null;
  timezone_source: string;
  precision: "date" | "datetime";
  due_at: string | null;
  status: Verification;
  review_reason: string | null;
  supported_by_current_version: boolean;
  evidence: Evidence[];
  active: boolean;
  superseded_by_deadline_id: Uuid | null;
  superseded_by_quote: string | null;
  superseded_by_source_span_id: Uuid | null;
};

export type Assessment = {
  id: Uuid;
  course_id: Uuid;
  title: string;
  canonical_key: string;
  weighting_percent: number | null;
  status: Verification;
  review_reason: string | null;
  evidence: Evidence[];
  deadlines: Deadline[];
};

export type MaterialMapping = {
  id: Uuid;
  relation: "supported_by";
  rationale: string;
  quote: string;
  source_span_id: Uuid;
  page_number: number | null;
  slide_number: number | null;
  section_path: string | null;
  resource_id: Uuid;
  resource_title: string;
  resource_type: ResourceType;
  resource_version_id: Uuid;
  version_number: number;
  current_version: boolean;
  retrieval_mode: string;
  retrieval_rank: number;
  retrieval_score: number;
  verifier_model: string;
  verifier_prompt_version: string;
  verifier_model_call_id: Uuid;
  workflow_run_id: Uuid;
};

export type Requirement = {
  id: Uuid;
  assessment_id: Uuid;
  assessment_title: string;
  text: string;
  status: Verification;
  review_reason: string | null;
  supported_by_current_version: boolean;
  evidence: Evidence[];
  materials: MaterialMapping[];
};

export type Obligation = {
  id: Uuid;
  course_id: Uuid;
  assessment_id: Uuid | null;
  canonical_action: string;
  category: string;
  modality: "mandatory" | "recommended" | null;
  status: Verification;
  review_reason: string | null;
  supported_by_current_version: boolean;
  evidence: Evidence[];
  hidden: boolean;
};

export type DependencyEnd = {
  kind: "assessment" | "obligation" | "milestone";
  id: Uuid | null;
  milestone: string | null;
  label: string;
  status: Verification | null;
};

export type DependencyEdge = {
  id: Uuid;
  course_id: Uuid;
  relation: "before" | "required_for";
  prerequisite: DependencyEnd;
  dependent: DependencyEnd;
  rule: string;
  derived_from_obligation_id: Uuid;
  evidence: Evidence[];
};

export type Conflict = {
  id: Uuid;
  course_id: Uuid;
  assessment_id: Uuid;
  assessment_title: string;
  field: "deadline";
  status: "open" | "acknowledged" | "resolved_by_source";
  sides: Deadline[];
};

export type StudyState = "not_started" | "studying" | "studied";
export type TaskType =
  | "study_material"
  | "obligation"
  | "submit_assessment"
  | "check_conflict"
  | "ethics_approval"
  | "data_collection";

export type StudyTarget = {
  source_span_id: Uuid;
  quote: string;
  resource_id: Uuid;
  resource_title: string;
  version_number: number;
  current_version: boolean;
  page_number: number | null;
  slide_number: number | null;
  section_path: string | null;
  study_state: StudyState;
};

export type Reason = { code: string } & Record<string, unknown>;

export type StudyTask = {
  position: number;
  task_key: string;
  task_type: TaskType;
  status: "ready" | "blocked" | "completed";
  title: string;
  course: { id: Uuid; code: string | null; title: string };
  assessment: { id: Uuid; title: string } | null;
  obligation_id: Uuid | null;
  conflict_id: Uuid | null;
  mandatory: boolean;
  anchor_at: string | null;
  anchor_basis: "stated_deadline" | "earliest_conflicting_deadline" | null;
  estimated_minutes: number;
  why_now: string;
  reasons: Reason[];
  prerequisite_keys: string[];
  study_state: StudyState | null;
  targets: StudyTarget[];
  deadlines: Deadline[];
  conflict_values: Deadline[];
  evidence: Evidence[];
};

export type StudyPlan = {
  plan_version: string;
  learner: string;
  as_of: string;
  latest_snapshot_id: Uuid | null;
  tasks: StudyTask[];
};

export type SourceQuote = {
  source_span_id: Uuid;
  quote: string;
  resource_title: string;
  version_number: number;
  page_number: number | null;
  slide_number: number | null;
  section_path: string | null;
};

export type DeadlineValue = { deadline_id: Uuid; date: string; time: string | null; timezone: string | null };

export type Affected = {
  kind: "deadline_added" | "deadline_superseded" | "obligation_wording_changed" | "conflict_changed";
  assessment_id: Uuid | null;
  assessment_title: string | null;
  obligation_id: Uuid | null;
  old_value: Record<string, unknown> | null;
  new_value: Record<string, unknown> | null;
  sources: SourceQuote[];
};

export type Change = {
  id: Uuid;
  observed_at: string;
  change_type: "added" | "updated";
  course_id: Uuid;
  course_code: string | null;
  course_title: string;
  resource_id: Uuid;
  resource_title: string;
  resource_type: ResourceType;
  old_version: { id: Uuid; version_number: number; content_hash: string } | null;
  new_version: { id: Uuid; version_number: number; content_hash: string };
  workflow_run_id: Uuid | null;
  affected: Affected[];
};

export type TaskChange = { task_key: string; title: string; fields: Record<string, [unknown, unknown]> };

export type PlanDiff = {
  before_plan_id: Uuid;
  after_plan_id: Uuid;
  before_as_of: string;
  after_as_of: string;
  added: TaskChange[];
  removed: TaskChange[];
  changed: TaskChange[];
  unchanged_count: number;
};

export type Sync = { observed_at: string; changes: Change[]; plan_diff: PlanDiff | null };

export type WorkflowRun = {
  id: Uuid;
  course_id: Uuid;
  workflow_version: string;
  status: "pending" | "running" | "succeeded" | "failed";
  started_at: string | null;
  finished_at: string | null;
  summary: Record<string, unknown> | null;
};

export type DemoWorkflowStarted = { succeeded: boolean; runs: WorkflowRun[] };

export type DemoSyncResult = {
  succeeded: boolean;
  observed_at: string;
  changes: {
    change_event_id: Uuid;
    course_id: Uuid;
    resource_id: Uuid;
    resource_title: string;
    old_version: number | null;
    new_version: number;
  }[];
  affected_course_ids: Uuid[];
  runs: WorkflowRun[];
};

/** One live, unpersisted model answer over an assessment's current brief.
 * `quote_found`: the quote occurs in the brief; not a verified claim. */
export type LiveRequirements = {
  assessment_id: Uuid;
  resource_id: Uuid;
  resource_title: string;
  resource_version_id: Uuid;
  version_number: number;
  requirements: { requirement: string; quote: string; quote_found: boolean }[];
  live: true;
  provider: string;
  model: string;
  prompt_version: string;
  model_call_id: Uuid;
  latency_ms: number;
  usage: { prompt_tokens: number | null; completion_tokens: number | null; reasoning_tokens: number | null };
};

/** The current version of an assessment's brief as stored: one entry per
 * source span (a PDF page), in document order. */
export type BriefText = {
  assessment_id: Uuid;
  resource_id: Uuid;
  resource_title: string;
  resource_version_id: Uuid;
  version_number: number;
  pages: { page_number: number | null; text: string }[];
};
