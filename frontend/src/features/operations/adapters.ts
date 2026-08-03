import type { AgentMessageRead, EventEnvelope, TaskRead } from "../../api/types";
import type {
  AuditSignal,
  OperationsAgentRecord,
  OperationsAuditRecord,
  OperationsModel,
  OperationsSource
} from "./types";

export function humanizeOperationsCode(value: string): string {
  return value
    .replaceAll(".", " ")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return "[unserializable value]";
  }
}

function payloadString(payload: Readonly<Record<string, unknown>>, key: string): string {
  const value = payload[key];
  return typeof value === "string" ? value.trim() : "";
}

function auditSummary(event: EventEnvelope): string {
  const payload = event.payload;
  for (const key of ["title", "summary", "detail", "body", "name", "original_filename", "filename", "reason"]) {
    const value = payloadString(payload, key);
    if (value) return value;
  }
  return humanizeOperationsCode(event.event_type);
}

function auditCategory(eventType: string): string {
  const [prefix] = eventType.split(".");
  if (prefix === "file") return "Files";
  if (prefix === "task") return "Tasks";
  if (prefix === "agent") return "Agents";
  if (["finding", "approval", "recommendation"].includes(prefix)) return "Review";
  if (["changeset", "revision", "clarification"].includes(prefix)) return "Changes";
  if (["validation", "export", "release", "report"].includes(prefix)) return "Release";
  if (prefix === "workflow") return "Workflow";
  if (prefix === "project") return "Project";
  return humanizeOperationsCode(prefix || "other");
}

function auditSignal(eventType: string): AuditSignal {
  if (eventType.endsWith(".failed") || eventType.endsWith(".validation_failed")) return "danger";
  if (
    eventType.endsWith(".blocked") ||
    eventType === "approval.requested" ||
    eventType === "finding.created"
  ) return "attention";
  if (
    eventType.endsWith(".completed") ||
    eventType.endsWith(".approved") ||
    eventType.endsWith(".merged") ||
    eventType === "release.ready"
  ) return "success";
  return "info";
}

function messageMatchesAgent(message: AgentMessageRead, agentId: string, agentKind: string): boolean {
  const contextAgentId = message.context.agent_id;
  return contextAgentId === agentId || message.agent_kind === agentKind;
}

function eventMatchesAgent(event: EventEnvelope, agentId: string): boolean {
  return event.agent_id === agentId || event.payload.agent_id === agentId;
}

function taskMatchesAgent(task: TaskRead, agentId: string, agentKind: string): boolean {
  return task.agent_instance_id === agentId || task.agent_kind === agentKind;
}

function buildAgentRecords(source: OperationsSource): OperationsAgentRecord[] {
  return source.agents
    .map((agent) => {
      const currentTask = agent.current_task_id
        ? source.tasks.find((task) => task.id === agent.current_task_id) ?? null
        : source.tasks.find((task) => task.agent_instance_id === agent.id && !task.finished_at) ?? null;
      const messages = source.messages
        .filter((message) => messageMatchesAgent(message, agent.id, agent.kind))
        .sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at));
      const recentEvents = source.events
        .filter((event) => eventMatchesAgent(event, agent.id))
        .sort((left, right) => right.seq - left.seq)
        .slice(0, 8);
      return {
        id: agent.id,
        projectId: agent.project_id,
        kind: agent.kind,
        name: agent.display_name,
        status: agent.status,
        location: agent.location,
        currentActivity: agent.current_activity,
        updatedAt: agent.updated_at,
        currentTask,
        relatedTaskCount: source.tasks.filter((task) => taskMatchesAgent(task, agent.id, agent.kind)).length,
        messages,
        recentEvents
      };
    })
    .sort((left, right) => left.name.localeCompare(right.name));
}

function buildAuditRecords(events: EventEnvelope[]): OperationsAuditRecord[] {
  const ordered = [...events].sort((left, right) => left.seq - right.seq);
  return ordered.map((event, index) => {
    const previous = ordered[index - 1];
    const payload = event.payload;
    const summary = auditSummary(event);
    return {
      id: event.id,
      sequence: event.seq,
      schemaVersion: event.schema_version,
      eventType: event.event_type,
      category: auditCategory(event.event_type),
      summary,
      signal: auditSignal(event.event_type),
      occurredAt: event.occurred_at,
      projectId: event.project_id,
      agentId: event.agent_id ?? null,
      taskId: event.task_id ?? null,
      actorUserId: event.actor_user_id ?? null,
      payload,
      searchableText: [
        event.seq,
        event.event_type,
        summary,
        event.project_id,
        event.agent_id,
        event.task_id,
        event.actor_user_id,
        safeJson(payload)
      ].filter(Boolean).join(" ").toLowerCase(),
      gapBefore: Boolean(previous && event.seq !== previous.seq + 1)
    };
  });
}

export function buildOperationsModel(source: OperationsSource): OperationsModel {
  const audit = buildAuditRecords(source.events);
  return {
    mode: source.dataMode,
    unavailableReason: source.fallbackReason,
    project: source.project,
    agents: buildAgentRecords(source),
    audit,
    firstSequence: audit[0]?.sequence ?? null,
    latestSequence: audit.at(-1)?.sequence ?? null,
    sequenceGapCount: audit.filter((event) => event.gapBefore).length
  };
}
