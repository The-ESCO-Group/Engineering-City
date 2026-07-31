import type { EventEnvelope as GeneratedEventEnvelope } from "../generated";

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

export interface AgentInstanceRead {
  id: string;
  project_id: string;
  kind: string;
  display_name: string;
  status: "idle" | "walking" | "working" | "waiting" | "blocked" | "offline";
  location: string;
  current_activity: string;
  current_task_id: string | null;
  updated_at: string;
}

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

export interface AgentMessageRead {
  id: string;
  role: string;
  agent_kind: string;
  body: string;
  context: Record<string, unknown>;
  created_at: string;
}

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
