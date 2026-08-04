import type { components, EventEnvelope as GeneratedEventEnvelope } from "../generated";
import type {
  ExportArtifactKind,
  ImportVerificationStatus,
  ReleaseCandidateStatus,
  ValidationLevel
} from "../generated";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface ProjectRead {
  id: string;
  name: string;
  description: string;
  client_reference: string;
  status: string;
  is_paused: boolean;
  event_seq: number;
  created_at: string;
  updated_at: string;
  attributes: Record<string, unknown>;
}

export interface ProjectDetail extends ProjectRead {
  counts: {
    files: number;
    files_parsed: number;
    tasks: number;
    tasks_completed: number;
    findings: number;
    findings_open: number;
    approvals_pending: number;
    reports: number;
  };
}

export interface ProjectQuestionRead {
  id: string;
  project_id: string;
  reference: string;
  category: string;
  question: string;
  why_it_matters: string;
  status: string;
  is_mandatory: boolean;
  is_blocking: boolean;
  blocks_task_kinds: string[];
  answer: string;
  answered_by_id: string | null;
  answered_at: string | null;
  asked_by_agent: string;
  raised_in_task_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ClarificationRead {
  id: string;
  project_id: string;
  requirement_id: string | null;
  object_type: string | null;
  object_id: string | null;
  question: string;
  ambiguity: string;
  status: string;
  resolution: string;
  resolved_by_id: string | null;
  resolved_at: string | null;
  blocks_generation: boolean;
  created_at: string;
  updated_at: string;
}

export interface ClarificationInbox {
  project_questions: ProjectQuestionRead[];
  clarifications: ClarificationRead[];
  blocking_count: number;
}

export interface ClarificationResolutionRead {
  kind: "project_question" | "clarification";
  id: string;
  project_id: string;
  status: string;
  decision_id: string;
  answered_by_id: string;
  answered_at: string;
  unblocked_task_count: number;
}

export interface RequirementSourceRead {
  id: string;
  file_id: string | null;
  anchor_id: string | null;
  quoted_text: string;
  location_label: string;
  is_primary: boolean;
}

export interface AcceptanceCriterionRead {
  id: string;
  reference: string;
  statement: string;
  ordinal: number;
  is_testable: boolean;
  test_notes: string;
  status: string;
  approval_state: string;
}

export interface RequirementRead {
  id: string;
  project_id: string;
  reference: string;
  title: string;
  statement: string;
  source_text: string;
  kind: string;
  priority: string;
  status: string;
  approval_state: string;
  is_inferred: boolean;
  confidence: number;
  rationale: string;
  verification_method: string;
  sources: RequirementSourceRead[];
  acceptance_criteria: AcceptanceCriterionRead[];
  created_at: string;
  updated_at: string;
}

export interface RequirementDecisionRead {
  requirement: RequirementRead;
  decision_id: string;
  decided_by_id: string;
  decided_at: string;
  unblocked_task_count: number;
}

export interface ReleaseBlockRead {
  code: string;
  summary: string;
  detail: string;
}

export interface ReleaseReadinessRead {
  revision_number: number;
  is_ready: boolean;
  blocking: ReleaseBlockRead[];
  outstanding_gates: string[];
  validation_error_count: number;
  highest_attainable_validation_level: ValidationLevel;
  import_verification_possible: boolean;
  import_verification_detail: string;
}

export interface ReleaseActionRead {
  available: boolean;
  path: string;
  reason: string;
}

export interface ReleaseActionsRead {
  generate: ReleaseActionRead;
  finalize: ReleaseActionRead;
  download: ReleaseActionRead;
}

export interface ExportArtifactRead {
  id: string;
  release_candidate_id: string;
  kind: ExportArtifactKind;
  relative_path: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  validation_level: ValidationLevel;
  generator_name: string;
  generator_version: string;
  import_order: number;
  is_produced_by_plugin: boolean;
  plugin_name: string;
  warnings: unknown[];
  created_at: string;
}

export interface ReleaseManifestRead {
  id: string;
  release_candidate_id: string;
  manifest_version: string;
  content_sha256: string;
  artifact_count: number;
  checksum_file_sha256: string;
  evidence_index_sha256: string;
  frozen_at: string | null;
  frozen_by_id: string | null;
  approved_by_id: string | null;
  approval_summary: Record<string, unknown>;
}

export interface ReleaseArchiveRead {
  id: string;
  project_id: string;
  release_candidate_id: string;
  release_manifest_id: string;
  approved_by_id: string | null;
  approved_by_display_name: string;
  finalized_by_id: string | null;
  relative_path: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  file_count: number;
  source_revision: number;
  validation_level: ValidationLevel;
  import_verification_status: ImportVerificationStatus;
  import_verification_detail: string;
  rockwell_import_verified: boolean;
  manifest_sha256: string;
  checksum_file_sha256: string;
  frozen_at: string;
  finalized_at: string;
  download_path: string;
}

export interface ReleaseCandidateRead {
  id: string;
  project_id: string;
  revision_number: number;
  candidate_number: number;
  name: string;
  status: ReleaseCandidateStatus;
  approval_state: "draft" | "proposed" | "approved" | "rejected" | "superseded";
  export_profile_id: string | null;
  validation_run_id: string | null;
  validation_level: ValidationLevel;
  import_verification_status: ImportVerificationStatus;
  import_verification_detail: string;
  generator_version: string;
  package_path: string;
  package_sha256: string;
  package_bytes: number;
  limitations: unknown[];
  unsupported_items: unknown[];
  approved_by_id: string | null;
  approved_at: string | null;
  released_at: string | null;
  created_at: string;
  updated_at: string;
  artifact_count: number;
  manifest: ReleaseManifestRead | null;
  archive: ReleaseArchiveRead | null;
  actions: ReleaseActionsRead;
}

export type AgentInstanceRead = components["schemas"]["AgentInstanceRead"];

export interface CapabilityStatus {
  name: string;
  available: boolean;
  provider: string;
  version: string;
  detail: string;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  app_name: string;
  version: string;
  contract_version: string;
  environment: string;
  database: string;
  enabled_providers: string[];
  capabilities: CapabilityStatus[];
}

export interface UserSummary {
  id: string;
  email: string;
  display_name: string;
  is_active: boolean;
  roles: Array<"engineer" | "reviewer" | "administrator">;
}

export interface CurrentUserResponse {
  user: UserSummary;
  project_roles: Record<string, string>;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
  user: UserSummary;
}

export type AgentDefinitionRead = components["schemas"]["AgentDefinitionRead"];
export type AgentMessageRead = components["schemas"]["AgentMessageRead"];
export type AgentRunRead = components["schemas"]["AgentRunRead"];
export type AgentRunDetail = components["schemas"]["AgentRunDetail"];

export interface FindingRead {
  id: string;
  project_id: string;
  title: string;
  category: string;
  severity: "informational" | "low" | "medium" | "high" | "critical";
  status: "open" | "accepted" | "rejected" | "resolved";
  fact: string;
  inference: string;
  suggested_review_action: string;
  limitations: string;
  confidence: number;
  requires_approval: boolean;
  is_reviewed: boolean;
  created_by_agent_kind: string;
  evidence_count: number;
  decision_notes: string;
  created_at: string;
  updated_at: string;
}

export interface EvidenceAnchorRead {
  id: string;
  source_label: string;
  display_location: string;
  excerpt: string;
  expectation: string;
}

export interface FindingEvidenceRead {
  id: string;
  anchor_id: string;
  evidence_kind: string;
  role: string;
  note: string;
  anchor: EvidenceAnchorRead | null;
}

export interface FindingDetail extends FindingRead {
  evidence: FindingEvidenceRead[];
  details: Record<string, unknown>;
  warnings: unknown[];
}

export interface ApprovalRead {
  id: string;
  project_id: string;
  finding_id: string | null;
  kind: string;
  status: "pending" | "approved" | "rejected" | "cancelled";
  title: string;
  question: string;
  context: Record<string, unknown>;
  requested_by_agent_kind: string;
  requested_at: string | null;
  decision_notes: string;
  created_at: string;
}

export interface ReportRead {
  id: string;
  report_type: string;
  export_format: string;
  provider_name: string;
  provider_version?: string;
  title: string;
  status: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  finding_ids: string[];
  summary: Record<string, unknown>;
  error_message: string;
  download_path: string;
  created_at: string;
}

export interface UploadedFileRead {
  id: string;
  project_id: string;
  original_filename: string;
  file_kind: string;
  document_role: string;
  media_type: string;
  extension: string;
  size_bytes: number;
  sha256: string;
  status: string;
  status_detail: string;
  parent_file_id: string | null;
  archive_member_path: string;
  created_at: string;
  updated_at: string;
}

export interface UploadResultItem {
  accepted: boolean;
  original_filename: string;
  file: UploadedFileRead | null;
  extracted: UploadedFileRead[];
  rejection_code: string;
  rejection_reason: string;
}

export interface UploadResponse {
  project_id: string;
  accepted_count: number;
  rejected_count: number;
  results: UploadResultItem[];
}

export interface TaskRead {
  id: string;
  project_id: string;
  run_id: string | null;
  kind: string;
  title: string;
  description: string;
  status: string;
  priority: number;
  agent_kind: string | null;
  agent_instance_id: string | null;
  attempts: number;
  max_attempts: number;
  started_at: string | null;
  finished_at: string | null;
  error_code: string;
  error_message: string;
  blocked_reason: string;
  created_at: string;
  updated_at: string;
}

export interface WorkflowStateResponse {
  project_id: string;
  project_status: string;
  is_paused: boolean;
  active_run_id: string | null;
  queued: number;
  running: number;
  blocked: number;
  awaiting_approval: number;
  completed: number;
  failed: number;
  cancelled: number;
  message: string;
}

export interface RunRead {
  run_id: string;
  project_id: string;
  status: string;
  started_at: string;
  task_count: number;
  tasks: TaskRead[];
}

export interface ParserCapability {
  file_kind: string;
  available: boolean;
  parser: string;
  version: string;
  detail: string;
}

export interface ProviderCapability {
  name: string;
  available: boolean;
  provider: string;
  version: string;
  detail: string;
}

export type EventEnvelope = GeneratedEventEnvelope & {
  readonly payload: Readonly<Record<string, unknown>>;
};

export interface WebSocketHello {
  type: "hello";
  schema_version: number;
  project_id: string;
  current_seq: number;
  replay_truncated: boolean;
  replayed: number;
}

export interface WebSocketPing {
  type: "ping";
}

export interface ApiErrorBody {
  error?: { code?: string; message?: string; details?: Record<string, unknown> };
}
