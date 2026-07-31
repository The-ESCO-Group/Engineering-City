import type {
  AgentInstanceRead,
  AgentMessageRead,
  ApprovalRead,
  ApiErrorBody,
  CapabilityStatus,
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
  ReportRead,
  RunRead,
  TaskRead,
  UploadResponse,
  UploadedFileRead,
  WorkflowStateResponse
} from "./types";
import { configuredApiBase } from "../config";

export const API_BASE = configuredApiBase();

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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(apiUrl(path), {
    credentials: "include",
    ...init,
    headers
  });
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

export const engineeringApi = {
  health: () => request<HealthResponse>("/health"),
  capabilities: () => request<CapabilityStatus[]>("/system/capabilities"),
  login: (email: string, password: string) =>
    request<LoginResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password })
    }),
  logout: () => request<{ ok: boolean }>("/auth/logout", { method: "POST" }),
  me: () => request<CurrentUserResponse>("/auth/me"),
  projects: () => request<Page<ProjectRead>>("/projects?limit=50"),
  createProject: (input: {
    name: string;
    description?: string;
    client_reference?: string;
    attributes?: Record<string, unknown>;
  }) => request<ProjectDetail>("/projects", { method: "POST", body: JSON.stringify(input) }),
  project: (projectId: string) => request<ProjectDetail>(`/projects/${projectId}`),
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
  events: (projectId: string, sinceSeq = 0) =>
    request<EventEnvelope[]>(`/projects/${projectId}/events?since_seq=${sinceSeq}&limit=1000`),
  agents: (projectId: string) => request<AgentInstanceRead[]>(`/projects/${projectId}/agents`),
  messages: (projectId: string) =>
    request<AgentMessageRead[]>(`/projects/${projectId}/messages?limit=200`),
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
  parsers: (projectId: string) =>
    request<ParserCapability[]>(`/projects/${projectId}/parsers`),
  providers: () => request<ProviderCapability[]>("/model-configurations/providers")
};

export function reportDownloadUrl(path: string): string {
  return path.startsWith("http") ? path : new URL(path, serverBaseUrl()).toString();
}

export function projectSocketUrl(projectId: string, sinceSeq: number): string {
  const url = serverBaseUrl();
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `/ws/projects/${projectId}`;
  url.searchParams.set("since_seq", String(sinceSeq));
  return url.toString();
}
