import type {
  AgentDefinitionRead,
  AgentInstanceRead,
  AgentMessageRead,
  AgentRunDetail,
  AgentRunRead,
  ApprovalRead,
  ApiErrorBody,
  CapabilityStatus,
  ClarificationInbox,
  ClarificationResolutionRead,
  CurrentUserResponse,
  EventEnvelope,
  FindingDetail,
  FindingRead,
  HealthResponse,
  LoginResponse,
  Page,
  ParserCapability,
  ProjectDetail,
  ProjectRead,
  ProviderCapability,
  ExportArtifactRead,
  ReleaseArchiveRead,
  ReleaseCandidateRead,
  ReleaseReadinessRead,
  RequirementDecisionRead,
  RequirementRead,
  ReportRead,
  RunRead,
  TaskRead,
  UploadResponse,
  UploadedFileRead,
  WorkflowStateResponse
} from "./types";
import { isTauri } from "@tauri-apps/api/core";
import { configuredApiBase } from "../config";

export const API_BASE = configuredApiBase();

const DESKTOP_RUNTIME = isTauri();
// The packaged WebView is cross-site to the loopback API, so its SameSite
// cookie cannot authenticate fetch/WebSocket traffic. Keep the token only for
// this renderer lifetime; browser sessions continue to use the httpOnly cookie.
let desktopAccessToken: string | null = null;

const PROJECT_PAGE_SIZE = 500;
const PROJECT_LIST_LIMIT = 10_000;
const EVENT_PAGE_SIZE = 1_000;
const EVENT_REPLAY_LIMIT = 10_000;
const MESSAGE_PAGE_SIZE = 500;
const MESSAGE_REPLAY_LIMIT = 10_000;

function apiUrl(path: string): string {
  return new URL(`${API_BASE}${path}`, window.location.origin).toString();
}

function serverBaseUrl(): URL {
  const api = new URL(API_BASE, window.location.origin);
  api.pathname = api.pathname.replace(/\/api\/?$/, "/");
  return api;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code = "request_failed"
  ) {
    super(message);
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
  includeDesktopAuthorization = true
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (
    DESKTOP_RUNTIME &&
    includeDesktopAuthorization &&
    desktopAccessToken &&
    !headers.has("Authorization")
  ) {
    headers.set("Authorization", `Bearer ${desktopAccessToken}`);
  }
  const response = await fetch(apiUrl(path), {
    credentials: "include",
    ...init,
    headers
  });
  if (DESKTOP_RUNTIME && response.status === 401) desktopAccessToken = null;
  if (!response.ok) {
    let body: ApiErrorBody = {};
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      // A proxy or stopped local backend may return a non-JSON page.
    }
    throw new ApiError(
      body.error?.message || `Local backend request failed (${response.status}).`,
      response.status,
      body.error?.code
    );
  }
  return (await response.json()) as T;
}

async function login(email: string, password: string): Promise<LoginResponse> {
  const response = await request<LoginResponse>(
    "/auth/login",
    {
      method: "POST",
      body: JSON.stringify({ email, password })
    },
    false
  );
  if (DESKTOP_RUNTIME) desktopAccessToken = response.access_token;
  return response;
}

async function logout(): Promise<{ ok: boolean }> {
  const response = await request<{ ok: boolean }>("/auth/logout", { method: "POST" });
  if (DESKTOP_RUNTIME) desktopAccessToken = null;
  return response;
}

async function listProjects(): Promise<Page<ProjectRead>> {
  const items: ProjectRead[] = [];
  let total = 0;

  for (let offset = 0; offset < PROJECT_LIST_LIMIT; offset += PROJECT_PAGE_SIZE) {
    const limit = Math.min(PROJECT_PAGE_SIZE, PROJECT_LIST_LIMIT - offset);
    const page = await request<Page<ProjectRead>>(`/projects?limit=${limit}&offset=${offset}`);
    total = page.total;
    items.push(...page.items.slice(0, limit));

    if (page.items.length < limit || offset + page.items.length >= page.total) break;
  }

  return {
    items,
    total,
    limit: PROJECT_LIST_LIMIT,
    offset: 0
  };
}

async function replayProjectEvents(projectId: string, sinceSeq = 0): Promise<EventEnvelope[]> {
  const events: EventEnvelope[] = [];

  for (let offset = 0; offset < EVENT_REPLAY_LIMIT; offset += EVENT_PAGE_SIZE) {
    const limit = Math.min(EVENT_PAGE_SIZE, EVENT_REPLAY_LIMIT - offset);
    const page = await request<EventEnvelope[]>(
      `/projects/${projectId}/events?since_seq=${sinceSeq}&limit=${limit}&offset=${offset}`
    );
    events.push(...page.slice(0, limit));
    if (page.length < limit) break;
  }

  return events;
}

async function replayProjectMessages(projectId: string): Promise<AgentMessageRead[]> {
  const messages: AgentMessageRead[] = [];
  let since: string | null = null;

  while (messages.length < MESSAGE_REPLAY_LIMIT) {
    const limit = Math.min(MESSAGE_PAGE_SIZE, MESSAGE_REPLAY_LIMIT - messages.length);
    const cursor: string = since ? `&since=${encodeURIComponent(since)}` : "";
    const page: AgentMessageRead[] = await request<AgentMessageRead[]>(
      `/projects/${projectId}/messages?limit=${limit}${cursor}`
    );
    messages.push(...page.slice(0, limit));
    if (page.length < limit) break;
    const next: string | undefined = page.at(-1)?.id;
    if (!next || next === since) break;
    since = next;
  }

  return messages;
}

export const engineeringApi = {
  health: () => request<HealthResponse>("/health"),
  capabilities: () => request<CapabilityStatus[]>("/system/capabilities"),
  login,
  logout,
  me: () => request<CurrentUserResponse>("/auth/me"),
  projects: listProjects,
  createProject: (input: {
    name: string;
    description?: string;
    client_reference?: string;
    attributes?: Record<string, unknown>;
    engineering_profile?: {
      project_name: string;
      customer_label: string;
      plant_or_site: string;
      project_description: string;
      project_type: string;
      units: string;
      locale: string;
      network_architecture: string;
      release_mode: string;
      restricted_domains: string[];
      target: {
        controller_family: string;
        controller_catalog_number: string;
        firmware_revision: string;
        studio_5000_version: string;
        safety_classification: string;
        hmi_product: string;
        hmi_version: string;
        hmi_terminal_model: string;
        hmi_resolution: string;
      };
      standards: {
        naming_standard_version: string;
        alarm_philosophy_version: string;
        controller_template: string;
        aoi_udt_library: string;
        hmi_library: string;
        test_template_set: string;
      };
    };
  }) => request<ProjectDetail>("/projects", { method: "POST", body: JSON.stringify(input) }),
  project: (projectId: string) => request<ProjectDetail>(`/projects/${projectId}`),
  clarifications: (projectId: string) =>
    request<ClarificationInbox>(`/projects/${projectId}/clarifications`),
  resolveProjectQuestion: (questionId: string, answer: string) =>
    request<ClarificationResolutionRead>(`/project-questions/${questionId}/resolve`, {
      method: "POST",
      body: JSON.stringify({ answer, resolution: "answered" })
    }),
  resolveClarification: (clarificationId: string, answer: string) =>
    request<ClarificationResolutionRead>(`/clarifications/${clarificationId}/resolve`, {
      method: "POST",
      body: JSON.stringify({ answer, resolution: "answered" })
    }),
  requirements: (projectId: string) =>
    request<RequirementRead[]>(`/projects/${projectId}/requirements`),
  decideRequirement: (requirementId: string, decision: "accept" | "reject", rationale: string) =>
    request<RequirementDecisionRead>(`/requirements/${requirementId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, rationale })
    }),
  files: (projectId: string) =>
    request<UploadedFileRead[]>(`/projects/${projectId}/files`),
  uploadFiles: (projectId: string, files: File[], documentRole?: string) => {
    const body = new FormData();
    files.forEach((file) => body.append("files", file));
    if (documentRole) body.append("document_role", documentRole);
    return request<UploadResponse>(`/projects/${projectId}/files`, { method: "POST", body });
  },
  tasks: (projectId: string) => request<TaskRead[]>(`/projects/${projectId}/tasks`),
  workflow: (projectId: string) =>
    request<WorkflowStateResponse>(`/projects/${projectId}/workflow`),
  startRun: (projectId: string, fileIds: string[] = []) =>
    request<RunRead>(`/projects/${projectId}/runs`, {
      method: "POST",
      body: JSON.stringify({ force_reparse: false, file_ids: fileIds, note: "" })
    }),
  pauseRun: (projectId: string, reason: string) =>
    request<WorkflowStateResponse>(`/projects/${projectId}/pause`, {
      method: "POST",
      body: JSON.stringify({ reason })
    }),
  resumeRun: (projectId: string, reason: string) =>
    request<WorkflowStateResponse>(`/projects/${projectId}/resume`, {
      method: "POST",
      body: JSON.stringify({ reason })
    }),
  cancelRun: (projectId: string, reason: string) =>
    request<WorkflowStateResponse>(`/projects/${projectId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason })
    }),
  events: replayProjectEvents,
  agentDefinitions: () => request<AgentDefinitionRead[]>("/agents"),
  agents: (projectId: string) => request<AgentInstanceRead[]>(`/projects/${projectId}/agents`),
  agentRuns: (projectId: string) =>
    request<AgentRunRead[]>(`/projects/${projectId}/agent-runs?limit=200`),
  agentRun: (runId: string) => request<AgentRunDetail>(`/agent-runs/${runId}`),
  messages: replayProjectMessages,
  findings: (projectId: string) =>
    request<Page<FindingRead>>(`/projects/${projectId}/findings?limit=500`),
  finding: (findingId: string) => request<FindingDetail>(`/findings/${findingId}`),
  approvals: (projectId: string) =>
    request<ApprovalRead[]>(`/projects/${projectId}/approvals`),
  decideApproval: (approvalId: string, approved: boolean, notes: string) =>
    request<ApprovalRead>(`/approvals/${approvalId}/decision`, {
      method: "POST",
      body: JSON.stringify({ approved, notes })
    }),
  reports: (projectId: string) => request<ReportRead[]>(`/projects/${projectId}/reports`),
  releaseReadiness: (projectId: string) =>
    request<ReleaseReadinessRead>(`/projects/${projectId}/release-readiness`),
  releaseCandidates: (projectId: string) =>
    request<Page<ReleaseCandidateRead>>(`/projects/${projectId}/release-candidates?limit=200&offset=0`),
  releaseArtifacts: (candidateId: string) =>
    request<Page<ExportArtifactRead>>(`/release-candidates/${candidateId}/artifacts?limit=500&offset=0`),
  createReleaseCandidate: (projectId: string) =>
    request<ReleaseCandidateRead>(`/projects/${projectId}/release-candidates`, {
      method: "POST",
      body: JSON.stringify({ force: false })
    }),
  generateReleaseArtifacts: (candidateId: string) =>
    request<ReleaseCandidateRead>(`/release-candidates/${candidateId}/artifacts`, {
      method: "POST"
    }),
  finalizeRelease: (candidateId: string) =>
    request<ReleaseArchiveRead>(`/release-candidates/${candidateId}/finalize`, {
      method: "POST",
      body: JSON.stringify({ confirm: true })
    }),
  parsers: (projectId: string) =>
    request<ParserCapability[]>(`/projects/${projectId}/parsers`),
  providers: () => request<ProviderCapability[]>("/model-configurations/providers")
};

export function reportDownloadUrl(path: string): string {
  const backend = serverBaseUrl();
  const candidate = new URL(path, backend);
  if (
    candidate.origin !== backend.origin ||
    !["http:", "https:"].includes(candidate.protocol) ||
    candidate.username ||
    candidate.password
  ) {
    throw new Error("Report downloads must remain on the configured API host.");
  }
  return candidate.toString();
}

export function projectSocketUrl(projectId: string, sinceSeq: number): string {
  const url = serverBaseUrl();
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `/ws/projects/${projectId}`;
  url.searchParams.set("since_seq", String(sinceSeq));
  if (DESKTOP_RUNTIME && desktopAccessToken) {
    url.searchParams.set("token", desktopAccessToken);
  }
  return url.toString();
}
