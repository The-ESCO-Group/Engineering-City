import type {
  AgentDefinitionRead,
  AgentInstanceRead,
  AgentMessageRead,
  AgentRunRead,
  EventEnvelope,
  ProjectDetail,
  TaskRead
} from "../../api/types";

export type OperationsMode = "demo" | "live" | "unavailable";
export type OperationsView = "agents" | "audit";

export interface OperationsSource {
  dataMode: OperationsMode;
  fallbackReason: string;
  project: ProjectDetail | null;
  agentDefinitions: AgentDefinitionRead[];
  agents: AgentInstanceRead[];
  agentRuns: AgentRunRead[];
  tasks: TaskRead[];
  messages: AgentMessageRead[];
  events: EventEnvelope[];
}

export interface OperationsAgentRecord {
  id: string;
  projectId: string;
  kind: string;
  definition: AgentDefinitionRead | null;
  name: string;
  status: AgentInstanceRead["status"];
  location: string;
  currentActivity: string;
  updatedAt: string;
  currentTask: TaskRead | null;
  relatedTaskCount: number;
  messages: AgentMessageRead[];
  recentRuns: AgentRunRead[];
  matchedRunCount: number;
  runWindowSize: number;
  recentEvents: EventEnvelope[];
}

export type AuditSignal = "info" | "success" | "attention" | "danger";

export interface OperationsAuditRecord {
  id: string;
  sequence: number;
  schemaVersion: number;
  eventType: string;
  category: string;
  summary: string;
  signal: AuditSignal;
  occurredAt: string;
  projectId: string;
  agentId: string | null;
  taskId: string | null;
  actorUserId: string | null;
  payload: Readonly<Record<string, unknown>>;
  searchableText: string;
  gapBefore: boolean;
}

export interface OperationsModel {
  mode: OperationsMode;
  unavailableReason: string;
  project: ProjectDetail | null;
  agents: OperationsAgentRecord[];
  audit: OperationsAuditRecord[];
  firstSequence: number | null;
  latestSequence: number | null;
  sequenceGapCount: number;
}
