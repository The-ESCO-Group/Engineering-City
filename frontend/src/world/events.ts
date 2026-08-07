import type { EventEnvelope, EventType } from "../generated";
import {
  toAgentId,
  toEventId,
  toProjectId,
  toReleaseCandidateId,
  toSemanticLocationId,
  toTaskId,
  type AgentId,
  type EventId,
  type LocationId,
  type ProjectId,
  type ReleaseCandidateId,
  type TaskId
} from "./ids";
import type { WorldPackageState } from "./types";

const MAX_KIND_LENGTH = 96;
const MAX_LABEL_LENGTH = 256;
const MAX_ACTIVITY_LENGTH = 512;
const MAX_DETAIL_LENGTH = 1_024;

// The backend can emit this event before the generated contract artifacts are
// refreshed by their owner. Keep the runtime decoder forward-compatible while
// preserving the generated files and contracts as read-only inputs here.
const CHANGESET_ABANDONED_EVENT_TYPE = "changeset.abandoned";

const EVENT_TYPES = new Set<string>([
  "project.created",
  "project.updated",
  "project.completed",
  "project.failed",
  "file.uploaded",
  "file.parsing_started",
  "file.parsed",
  "file.parse_failed",
  "file.deleted",
  "workflow.started",
  "workflow.paused",
  "workflow.resumed",
  "workflow.cancelled",
  "task.created",
  "task.started",
  "task.completed",
  "task.failed",
  "task.blocked",
  "agent.status_changed",
  "agent.location_changed",
  "agent.message_created",
  "finding.created",
  "finding.reviewed",
  "finding.updated",
  "approval.requested",
  "approval.decided",
  "report.created",
  "project.question_created",
  "project.question_resolved",
  "requirement.created",
  "workflow.planned",
  "workflow.failed",
  "task.assigned",
  "changeset.created",
  "changeset.validation_failed",
  "changeset.submitted",
  "changeset.reviewed",
  CHANGESET_ABANDONED_EVENT_TYPE,
  "changeset.merged",
  "revision.created",
  "gate.status_changed",
  "validation.started",
  "validation.completed",
  "export.started",
  "export.artifact_created",
  "release.candidate_created",
  "release.ready"
]);

interface WorldEventBase {
  readonly eventId: EventId;
  readonly projectId: ProjectId;
  readonly sequence: number;
  readonly schemaVersion: number;
  readonly sourceType: EventType;
  readonly occurredAt: string;
  readonly agentId: AgentId | null;
  readonly taskId: TaskId | null;
}

export interface WorldTaskEvent extends WorldEventBase {
  readonly kind: "task";
  readonly taskId: TaskId;
  readonly taskKind: string;
  readonly title: string;
  readonly status: string;
  readonly agentKind: string | undefined;
  readonly attempts: number | undefined;
  readonly detail: string | undefined;
}

export interface WorldTaskAssignmentEvent extends WorldEventBase {
  readonly kind: "task_assignment";
  readonly taskId: TaskId;
  readonly taskKind: string;
  readonly title: string;
  readonly agentKind: string;
  readonly assignedAgentId: AgentId | null;
  readonly locationId: LocationId;
  readonly attempt: number;
}

export interface WorldAgentStatusEvent extends WorldEventBase {
  readonly kind: "agent_status";
  readonly agentId: AgentId;
  readonly agentKind: string;
  readonly displayName: string;
  readonly status: string;
  readonly currentActivity: string | undefined;
  readonly taskId: TaskId | null;
}

export interface WorldAgentLocationEvent extends WorldEventBase {
  readonly kind: "agent_location";
  readonly agentId: AgentId;
  readonly agentKind: string;
  readonly locationId: LocationId;
  readonly previousLocationId: LocationId | null;
}

export interface WorldPackageEvent extends WorldEventBase {
  readonly kind: "package";
  readonly packageState: WorldPackageState;
  readonly status: string;
  readonly title: string | undefined;
  readonly locationId: LocationId | null;
  readonly revisionNumber: number | null;
  readonly releaseCandidateId: ReleaseCandidateId | null;
  readonly expectedArtifactCount: number | undefined;
  readonly artifactCount: number | undefined;
  readonly artifactDelta: number;
  readonly warningCount: number | undefined;
}

export interface WorldNoopEvent extends WorldEventBase {
  readonly kind: "noop";
}

export type WorldEvent =
  | WorldTaskEvent
  | WorldTaskAssignmentEvent
  | WorldAgentStatusEvent
  | WorldAgentLocationEvent
  | WorldPackageEvent
  | WorldNoopEvent;

export type WorldEventNormalization =
  | { readonly ok: true; readonly event: WorldEvent }
  | { readonly ok: false; readonly reason: "invalid_envelope" | "invalid_payload" };

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function boundedString(value: unknown, limit: number): string | null {
  if (typeof value !== "string" || value.length === 0) return null;
  return value.length <= limit ? value : value.slice(0, limit);
}

function optionalString(
  payload: Readonly<Record<string, unknown>>,
  key: string,
  limit: number
): { readonly ok: true; readonly value: string | undefined } | { readonly ok: false } {
  const value = payload[key];
  if (value === undefined || value === null || value === "") {
    return { ok: true, value: undefined };
  }
  const parsed = boundedString(value, limit);
  return parsed === null ? { ok: false } : { ok: true, value: parsed };
}

function optionalInteger(
  payload: Readonly<Record<string, unknown>>,
  key: string
): { readonly ok: true; readonly value: number | undefined } | { readonly ok: false } {
  const value = payload[key];
  if (value === undefined || value === null) return { ok: true, value: undefined };
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? { ok: true, value }
    : { ok: false };
}

function nullableId<Id>(
  value: unknown,
  parser: (candidate: unknown) => Id | null
): { readonly ok: true; readonly value: Id | null } | { readonly ok: false } {
  if (value === undefined || value === null || value === "") return { ok: true, value: null };
  const parsed = parser(value);
  return parsed === null ? { ok: false } : { ok: true, value: parsed };
}

function sameNullableId(left: string | null, right: string | null) {
  return left === null || left === right;
}

function packageEvent(
  base: WorldEventBase,
  packageState: WorldPackageState,
  status: string,
  options: Partial<Omit<WorldPackageEvent, keyof WorldEventBase | "kind" | "packageState" | "status">> = {}
): WorldPackageEvent {
  return {
    ...base,
    kind: "package",
    packageState,
    status,
    title: options.title,
    locationId: options.locationId ?? null,
    revisionNumber: options.revisionNumber ?? null,
    releaseCandidateId: options.releaseCandidateId ?? null,
    expectedArtifactCount: options.expectedArtifactCount,
    artifactCount: options.artifactCount,
    artifactDelta: options.artifactDelta ?? 0,
    warningCount: options.warningCount
  };
}

function normalizeSimplePackageEvent(
  base: WorldEventBase,
  payload: Readonly<Record<string, unknown>>
): WorldEventNormalization | null {
  const status = optionalString(payload, "status", MAX_KIND_LENGTH);
  const title = optionalString(payload, "name", MAX_LABEL_LENGTH);
  if (!status.ok || !title.ok) return { ok: false, reason: "invalid_payload" };
  const sourceStatus = status.value ?? base.sourceType;

  switch (base.sourceType) {
    case "project.created":
    case "file.uploaded":
      return {
        ok: true,
        event: packageEvent(base, "intake", sourceStatus, { title: title.value })
      };
    case "file.parsing_started":
      return { ok: true, event: packageEvent(base, "parsing", sourceStatus) };
    case "file.parsed":
    case "workflow.started":
    case "workflow.planned":
      return { ok: true, event: packageEvent(base, "planning", sourceStatus) };
    case "project.failed":
    case "file.parse_failed":
    case "workflow.failed":
    case "workflow.cancelled":
      return { ok: true, event: packageEvent(base, "blocked", sourceStatus) };
    case "workflow.paused":
      return { ok: true, event: packageEvent(base, "review", sourceStatus) };
    case "workflow.resumed":
      return { ok: true, event: packageEvent(base, "in_progress", sourceStatus) };
    case "approval.requested":
    case "approval.decided":
    case "gate.status_changed": {
      const normalizedStatus = sourceStatus.toLowerCase();
      const packageState: WorldPackageState = ["failed", "rejected", "denied"].includes(
        normalizedStatus
      )
        ? "blocked"
        : "approval";
      return { ok: true, event: packageEvent(base, packageState, sourceStatus) };
    }
    case "finding.created":
    case "finding.reviewed":
    case "finding.updated":
      return { ok: true, event: packageEvent(base, "review", sourceStatus) };
    default:
      return null;
  }
}

function normalizeLocatedPackageEvent(
  base: WorldEventBase,
  payload: Readonly<Record<string, unknown>>
): WorldEventNormalization | null {
  const locatedTypes: readonly EventType[] = [
    "changeset.created",
    "changeset.validation_failed",
    "changeset.submitted",
    "changeset.reviewed",
    "changeset.merged",
    "revision.created",
    "validation.started",
    "validation.completed",
    "export.started",
    "export.artifact_created",
    "release.candidate_created",
    "release.ready"
  ];
  if (!locatedTypes.includes(base.sourceType)) return null;

  const locationId = toSemanticLocationId(payload.location);
  if (locationId === null) return { ok: false, reason: "invalid_payload" };
  const revisionKey =
    base.sourceType === "revision.created"
      ? "number"
      : base.sourceType === "changeset.merged"
        ? "resulting_revision_number"
        : "revision_number";
  const revision = optionalInteger(payload, revisionKey);
  const expected = optionalInteger(payload, "expected_artifact_count");
  const artifacts = optionalInteger(payload, "artifact_count");
  const warnings = optionalInteger(payload, "warning_count");
  const releaseCandidate = nullableId(payload.release_candidate_id, toReleaseCandidateId);
  const status = optionalString(payload, "status", MAX_KIND_LENGTH);
  const title = optionalString(payload, "name", MAX_LABEL_LENGTH);
  if (
    !revision.ok ||
    !expected.ok ||
    !artifacts.ok ||
    !warnings.ok ||
    !releaseCandidate.ok ||
    !status.ok ||
    !title.ok
  ) {
    return { ok: false, reason: "invalid_payload" };
  }
  const candidateRequired = [
    "export.started",
    "export.artifact_created",
    "release.candidate_created",
    "release.ready"
  ].includes(base.sourceType);
  const revisionRequired = [
    "revision.created",
    "validation.started",
    "validation.completed",
    "export.started",
    "release.candidate_created",
    "release.ready"
  ].includes(base.sourceType);
  if (
    (candidateRequired && releaseCandidate.value === null) ||
    (revisionRequired && revision.value === undefined)
  ) {
    return { ok: false, reason: "invalid_payload" };
  }

  let packageState: WorldPackageState = "in_progress";
  if (base.sourceType === "changeset.validation_failed") packageState = "blocked";
  else if (base.sourceType === "changeset.reviewed" || base.sourceType === "revision.created") {
    packageState = "review";
  } else if (base.sourceType.startsWith("validation.") || base.sourceType.startsWith("export.")) {
    packageState = "validation";
  } else if (base.sourceType === "release.candidate_created") {
    packageState = status.value === "validation_failed" || status.value === "failed"
      ? "blocked"
      : status.value === "awaiting_approval"
        ? "approval"
        : "validation";
  } else if (base.sourceType === "release.ready") packageState = "released";

  if (
    base.sourceType === "validation.completed" &&
    (payload.is_blocking === true || status.value === "failed")
  ) {
    packageState = "blocked";
  }

  return {
    ok: true,
    event: packageEvent(base, packageState, status.value ?? base.sourceType, {
      title: title.value,
      locationId,
      revisionNumber: revision.value ?? null,
      releaseCandidateId: releaseCandidate.value,
      expectedArtifactCount: expected.value,
      artifactCount: artifacts.value,
      artifactDelta: base.sourceType === "export.artifact_created" ? 1 : 0,
      warningCount: warnings.value
    })
  };
}

/** Converts a generated backend envelope into the small deterministic world union. */
export function normalizeWorldEvent(envelope: EventEnvelope): WorldEventNormalization {
  const eventId = toEventId(envelope.id);
  const projectId = toProjectId(envelope.project_id);
  const envelopeAgentId = nullableId(envelope.agent_id, toAgentId);
  const envelopeTaskId = nullableId(envelope.task_id, toTaskId);
  if (
    eventId === null ||
    projectId === null ||
    !envelopeAgentId.ok ||
    !envelopeTaskId.ok ||
    !EVENT_TYPES.has(envelope.event_type) ||
    !Number.isSafeInteger(envelope.seq) ||
    envelope.seq <= 0 ||
    !Number.isSafeInteger(envelope.schema_version) ||
    envelope.schema_version <= 0 ||
    typeof envelope.occurred_at !== "string" ||
    envelope.occurred_at.length === 0 ||
    envelope.occurred_at.length > 64
  ) {
    return { ok: false, reason: "invalid_envelope" };
  }

  const base: WorldEventBase = {
    eventId,
    projectId,
    sequence: envelope.seq,
    schemaVersion: envelope.schema_version,
    sourceType: envelope.event_type,
    occurredAt: envelope.occurred_at,
    agentId: envelopeAgentId.value,
    taskId: envelopeTaskId.value
  };
  const payload = isRecord(envelope.payload) ? envelope.payload : {};

  if (
    envelope.event_type === "task.created" ||
    envelope.event_type === "task.started" ||
    envelope.event_type === "task.completed" ||
    envelope.event_type === "task.failed" ||
    envelope.event_type === "task.blocked"
  ) {
    const taskId = toTaskId(payload.task_id);
    const taskKind = boundedString(payload.kind, MAX_KIND_LENGTH);
    const title = boundedString(payload.title, MAX_LABEL_LENGTH);
    const status = boundedString(payload.status, MAX_KIND_LENGTH);
    const agentKind = optionalString(payload, "agent_kind", MAX_KIND_LENGTH);
    const attempts = optionalInteger(payload, "attempts");
    const detail = optionalString(payload, "detail", MAX_DETAIL_LENGTH);
    if (
      taskId === null ||
      taskKind === null ||
      title === null ||
      status === null ||
      !agentKind.ok ||
      !attempts.ok ||
      !detail.ok ||
      !sameNullableId(base.taskId, taskId)
    ) {
      return { ok: false, reason: "invalid_payload" };
    }
    return {
      ok: true,
      event: {
        ...base,
        kind: "task",
        taskId,
        taskKind,
        title,
        status,
        agentKind: agentKind.value,
        attempts: attempts.value,
        detail: detail.value
      }
    };
  }

  if (envelope.event_type === "task.assigned") {
    const taskId = toTaskId(payload.task_id);
    const taskKind = boundedString(payload.kind, MAX_KIND_LENGTH);
    const title = boundedString(payload.title, MAX_LABEL_LENGTH);
    const agentKind = boundedString(payload.agent_kind, MAX_KIND_LENGTH);
    const assignedAgentId = nullableId(payload.agent_id, toAgentId);
    const locationId = toSemanticLocationId(payload.location);
    const attempt = optionalInteger(payload, "attempt");
    if (
      taskId === null ||
      taskKind === null ||
      title === null ||
      agentKind === null ||
      !assignedAgentId.ok ||
      locationId === null ||
      !attempt.ok ||
      !sameNullableId(base.taskId, taskId) ||
      !sameNullableId(base.agentId, assignedAgentId.value)
    ) {
      return { ok: false, reason: "invalid_payload" };
    }
    return {
      ok: true,
      event: {
        ...base,
        kind: "task_assignment",
        taskId,
        taskKind,
        title,
        agentKind,
        assignedAgentId: assignedAgentId.value,
        locationId,
        attempt: attempt.value ?? 1
      }
    };
  }

  if (envelope.event_type === "agent.status_changed") {
    const agentId = toAgentId(payload.agent_id);
    const agentKind = boundedString(payload.agent_kind, MAX_KIND_LENGTH);
    const displayName = boundedString(payload.display_name, MAX_LABEL_LENGTH);
    const status = boundedString(payload.status, MAX_KIND_LENGTH);
    const currentActivity = optionalString(payload, "current_activity", MAX_ACTIVITY_LENGTH);
    const taskId = nullableId(payload.task_id, toTaskId);
    if (
      agentId === null ||
      agentKind === null ||
      displayName === null ||
      status === null ||
      !currentActivity.ok ||
      !taskId.ok ||
      !sameNullableId(base.agentId, agentId) ||
      !sameNullableId(base.taskId, taskId.value)
    ) {
      return { ok: false, reason: "invalid_payload" };
    }
    return {
      ok: true,
      event: {
        ...base,
        kind: "agent_status",
        agentId,
        agentKind,
        displayName,
        status,
        currentActivity: currentActivity.value,
        taskId: taskId.value
      }
    };
  }

  if (envelope.event_type === "agent.location_changed") {
    const agentId = toAgentId(payload.agent_id);
    const agentKind = boundedString(payload.agent_kind, MAX_KIND_LENGTH);
    const locationId = toSemanticLocationId(payload.location);
    const previousLocationId = toSemanticLocationId(payload.previous_location);
    if (
      agentId === null ||
      agentKind === null ||
      locationId === null ||
      !sameNullableId(base.agentId, agentId)
    ) {
      return { ok: false, reason: "invalid_payload" };
    }
    return {
      ok: true,
      event: {
        ...base,
        kind: "agent_location",
        agentId,
        agentKind,
        locationId,
        previousLocationId
      }
    };
  }

  const locatedPackage = normalizeLocatedPackageEvent(base, payload);
  if (locatedPackage !== null) return locatedPackage;
  const simplePackage = normalizeSimplePackageEvent(base, payload);
  if (simplePackage !== null) return simplePackage;
  return { ok: true, event: { ...base, kind: "noop" } };
}

/** Stable, bounded signature for exact duplicate/conflict checks. */
export function worldEventSignature(event: WorldEvent): string {
  return JSON.stringify(event);
}
