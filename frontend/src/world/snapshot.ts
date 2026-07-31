import {
  packageIdForProject,
  toAgentId,
  toEventId,
  toPackageId,
  toProjectId,
  toReleaseCandidateId,
  toSemanticLocationId,
  toTaskId
} from "./ids";
import { createWorldState } from "./reducer";
import type {
  AppliedWorldEvent,
  WorldAgent,
  WorldAgentStatus,
  WorldEventStamp,
  WorldLocation,
  WorldPackage,
  WorldPackageState,
  WorldState,
  WorldStateOptions,
  WorldTask,
  WorldTaskStatus
} from "./types";

const SNAPSHOT_FORMAT = "esco.world-state";
export const WORLD_SNAPSHOT_VERSION = 1;

export interface WorldStateSnapshot {
  readonly format: typeof SNAPSHOT_FORMAT;
  readonly version: typeof WORLD_SNAPSHOT_VERSION;
  readonly projectId: string;
  readonly schemaVersion: number | null;
  readonly lastAppliedSequence: number;
  readonly agents: readonly WorldAgent[];
  readonly tasks: readonly WorldTask[];
  readonly packages: readonly WorldPackage[];
  readonly locations: readonly WorldLocation[];
  readonly recentEvents: readonly AppliedWorldEvent[];
}

const AGENT_STATUSES = new Set<WorldAgentStatus>([
  "idle",
  "walking",
  "working",
  "waiting",
  "blocked",
  "reviewing",
  "awaiting_approval",
  "completed",
  "offline",
  "unknown"
]);
const TASK_STATUSES = new Set<WorldTaskStatus>([
  "queued",
  "assigned",
  "running",
  "blocked",
  "awaiting_approval",
  "completed",
  "failed",
  "cancelled",
  "unknown"
]);
const PACKAGE_STATES = new Set<WorldPackageState>([
  "intake",
  "parsing",
  "planning",
  "in_progress",
  "blocked",
  "review",
  "approval",
  "validation",
  "released"
]);

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textValue(value: unknown, maximum = 1_024): string | null {
  return typeof value === "string" && value.length <= maximum ? value : null;
}

function integerValue(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function nullableId<Id>(value: unknown, parser: (candidate: unknown) => Id | null): Id | null | false {
  if (value === null) return null;
  return parser(value) ?? false;
}

function parseStamp(value: unknown, lastSequence: number): WorldEventStamp | null {
  if (!isRecord(value)) return null;
  const eventId = toEventId(value.eventId);
  const sequence = integerValue(value.sequence);
  const occurredAt = textValue(value.occurredAt, 64);
  if (eventId === null || sequence === null || sequence === 0 || sequence > lastSequence || occurredAt === null) {
    return null;
  }
  return { eventId, sequence, occurredAt };
}

function parseAgent(value: unknown, lastSequence: number): WorldAgent | null {
  if (!isRecord(value)) return null;
  const id = toAgentId(value.id);
  const agentKind = textValue(value.agentKind, 96);
  const displayName = textValue(value.displayName, 256);
  const sourceStatus = textValue(value.sourceStatus, 96);
  const currentActivity = textValue(value.currentActivity, 512);
  const taskId = nullableId(value.taskId, toTaskId);
  const locationId = nullableId(value.locationId, toSemanticLocationId);
  const targetLocationId = nullableId(value.targetLocationId, toSemanticLocationId);
  const previousLocationId = nullableId(value.previousLocationId, toSemanticLocationId);
  const lastEvent = parseStamp(value.lastEvent, lastSequence);
  if (
    id === null ||
    agentKind === null ||
    displayName === null ||
    sourceStatus === null ||
    currentActivity === null ||
    typeof value.status !== "string" ||
    !AGENT_STATUSES.has(value.status as WorldAgentStatus) ||
    taskId === false ||
    locationId === false ||
    targetLocationId === false ||
    previousLocationId === false ||
    lastEvent === null
  ) {
    return null;
  }
  return {
    id,
    agentKind,
    displayName,
    status: value.status as WorldAgentStatus,
    sourceStatus,
    currentActivity,
    taskId,
    locationId,
    targetLocationId,
    previousLocationId,
    lastEvent
  };
}

function parseTask(value: unknown, lastSequence: number): WorldTask | null {
  if (!isRecord(value)) return null;
  const id = toTaskId(value.id);
  const taskKind = textValue(value.taskKind, 96);
  const title = textValue(value.title, 256);
  const sourceStatus = textValue(value.sourceStatus, 96);
  const agentKind = textValue(value.agentKind, 96);
  const agentId = nullableId(value.agentId, toAgentId);
  const locationId = nullableId(value.locationId, toSemanticLocationId);
  const attempts = integerValue(value.attempts);
  const detail = textValue(value.detail, 1_024);
  const lastEvent = parseStamp(value.lastEvent, lastSequence);
  if (
    id === null ||
    taskKind === null ||
    title === null ||
    sourceStatus === null ||
    agentKind === null ||
    typeof value.status !== "string" ||
    !TASK_STATUSES.has(value.status as WorldTaskStatus) ||
    agentId === false ||
    locationId === false ||
    attempts === null ||
    detail === null ||
    lastEvent === null
  ) {
    return null;
  }
  return {
    id,
    taskKind,
    title,
    status: value.status as WorldTaskStatus,
    sourceStatus,
    agentKind,
    agentId,
    locationId,
    attempts,
    detail,
    lastEvent
  };
}

function parsePackage(value: unknown, projectId: string, lastSequence: number): WorldPackage | null {
  if (!isRecord(value)) return null;
  const id = toPackageId(value.id);
  const parsedProjectId = toProjectId(value.projectId);
  const sourceStatus = textValue(value.sourceStatus, 96);
  const title = textValue(value.title, 256);
  const taskId = nullableId(value.taskId, toTaskId);
  const releaseCandidateId = nullableId(value.releaseCandidateId, toReleaseCandidateId);
  const locationId = nullableId(value.locationId, toSemanticLocationId);
  const previousLocationId = nullableId(value.previousLocationId, toSemanticLocationId);
  const revisionNumber = value.revisionNumber === null ? null : integerValue(value.revisionNumber);
  const expectedArtifactCount = integerValue(value.expectedArtifactCount);
  const artifactCount = integerValue(value.artifactCount);
  const warningCount = integerValue(value.warningCount);
  const lastEvent = value.lastEvent === null ? null : parseStamp(value.lastEvent, lastSequence);
  if (
    id === null ||
    parsedProjectId === null ||
    parsedProjectId !== projectId ||
    id !== packageIdForProject(parsedProjectId) ||
    typeof value.state !== "string" ||
    !PACKAGE_STATES.has(value.state as WorldPackageState) ||
    sourceStatus === null ||
    title === null ||
    taskId === false ||
    revisionNumber === null && value.revisionNumber !== null ||
    releaseCandidateId === false ||
    locationId === false ||
    previousLocationId === false ||
    expectedArtifactCount === null ||
    artifactCount === null ||
    warningCount === null ||
    (value.lastEvent !== null && lastEvent === null)
  ) {
    return null;
  }
  return {
    id,
    projectId: parsedProjectId,
    state: value.state as WorldPackageState,
    sourceStatus,
    title,
    taskId,
    revisionNumber,
    releaseCandidateId,
    locationId,
    previousLocationId,
    expectedArtifactCount,
    artifactCount,
    warningCount,
    lastEvent
  };
}

function parseIdArray<Id>(
  value: unknown,
  parser: (candidate: unknown) => Id | null,
  maximum: number
): readonly Id[] | null {
  if (!Array.isArray(value) || value.length > maximum) return null;
  const parsed = value.map(parser);
  if (parsed.some((item) => item === null)) return null;
  const ids = parsed as Id[];
  return new Set(ids).size === ids.length ? ids : null;
}

function parseLocation(value: unknown, lastSequence: number, limits: WorldState["limits"]): WorldLocation | null {
  if (!isRecord(value)) return null;
  const id = toSemanticLocationId(value.id);
  const agentIds = parseIdArray(value.agentIds, toAgentId, limits.maxAgents);
  const activeTaskIds = parseIdArray(value.activeTaskIds, toTaskId, limits.maxTasks);
  const packageIds = parseIdArray(value.packageIds, toPackageId, 1);
  const updatedSequence = integerValue(value.updatedSequence);
  if (
    id === null ||
    agentIds === null ||
    activeTaskIds === null ||
    packageIds === null ||
    updatedSequence === null ||
    updatedSequence > lastSequence
  ) {
    return null;
  }
  return { id, agentIds, activeTaskIds, packageIds, updatedSequence };
}

function parseRecent(value: unknown, lastSequence: number, maximum: number): readonly AppliedWorldEvent[] | null {
  if (!Array.isArray(value) || value.length > maximum) return null;
  const recent: AppliedWorldEvent[] = [];
  for (const item of value) {
    if (!isRecord(item)) return null;
    const sequence = integerValue(item.sequence);
    const eventId = toEventId(item.eventId);
    const signature = textValue(item.signature, 8_192);
    if (sequence === null || sequence === 0 || sequence > lastSequence || eventId === null || signature === null) {
      return null;
    }
    recent.push({ sequence, eventId, signature });
  }
  const ordered = [...recent].sort((left, right) => left.sequence - right.sequence);
  if (
    ordered.some((item, index) => item !== recent[index]) ||
    new Set(recent.map((item) => item.sequence)).size !== recent.length ||
    new Set(recent.map((item) => item.eventId)).size !== recent.length
  ) {
    return null;
  }
  return recent;
}

function recordById<T extends { readonly id: string }>(items: readonly T[]): Readonly<Record<string, T>> | null {
  const result: Record<string, T> = {};
  for (const item of items) {
    if (result[item.id]) return null;
    result[item.id] = item;
  }
  return result;
}

export function createWorldStateSnapshot(state: WorldState): WorldStateSnapshot {
  const byId = <T extends { readonly id: string }>(record: Readonly<Record<string, T>>) =>
    Object.values(record).sort((left, right) => left.id.localeCompare(right.id));
  return {
    format: SNAPSHOT_FORMAT,
    version: WORLD_SNAPSHOT_VERSION,
    projectId: state.projectId,
    schemaVersion: state.schemaVersion,
    lastAppliedSequence: state.lastAppliedSequence,
    agents: byId(state.agents),
    tasks: byId(state.tasks),
    packages: byId(state.packages),
    locations: byId(state.locations),
    recentEvents: [...state.recentEvents]
  };
}

export function serializeWorldStateSnapshot(state: WorldState): string {
  return JSON.stringify(createWorldStateSnapshot(state));
}

/** Restores only a validated contiguous snapshot; buffered gap events are deliberately not persisted. */
export function restoreWorldStateSnapshot(
  serialized: string,
  options: Pick<WorldStateOptions, "limits"> = {}
): WorldState | null {
  let value: unknown;
  try {
    value = JSON.parse(serialized);
  } catch {
    return null;
  }
  if (
    !isRecord(value) ||
    value.format !== SNAPSHOT_FORMAT ||
    value.version !== WORLD_SNAPSHOT_VERSION
  ) {
    return null;
  }
  const projectId = toProjectId(value.projectId);
  const lastAppliedSequence = integerValue(value.lastAppliedSequence);
  const schemaVersion = value.schemaVersion === null ? null : integerValue(value.schemaVersion);
  if (
    projectId === null ||
    lastAppliedSequence === null ||
    (value.schemaVersion !== null && (schemaVersion === null || schemaVersion === 0))
  ) {
    return null;
  }
  const base = createWorldState(projectId, {
    afterSequence: lastAppliedSequence,
    limits: options.limits
  });
  if (
    !Array.isArray(value.agents) ||
    value.agents.length > base.limits.maxAgents ||
    !Array.isArray(value.tasks) ||
    value.tasks.length > base.limits.maxTasks ||
    !Array.isArray(value.packages) ||
    value.packages.length !== 1 ||
    !Array.isArray(value.locations) ||
    value.locations.length > base.limits.maxLocations
  ) {
    return null;
  }
  const agents = value.agents.map((item) => parseAgent(item, lastAppliedSequence));
  const tasks = value.tasks.map((item) => parseTask(item, lastAppliedSequence));
  const packages = value.packages.map((item) => parsePackage(item, projectId, lastAppliedSequence));
  const locations = value.locations.map((item) =>
    parseLocation(item, lastAppliedSequence, base.limits)
  );
  const recentEvents = parseRecent(
    value.recentEvents,
    lastAppliedSequence,
    base.limits.maxRecentEvents
  );
  if (
    agents.some((item) => item === null) ||
    tasks.some((item) => item === null) ||
    packages.some((item) => item === null) ||
    locations.some((item) => item === null) ||
    recentEvents === null
  ) {
    return null;
  }
  const agentRecord = recordById(agents as WorldAgent[]);
  const taskRecord = recordById(tasks as WorldTask[]);
  const packageRecord = recordById(packages as WorldPackage[]);
  const locationRecord = recordById(locations as WorldLocation[]);
  if (agentRecord === null || taskRecord === null || packageRecord === null || locationRecord === null) {
    return null;
  }
  return {
    ...base,
    schemaVersion,
    agents: agentRecord,
    tasks: taskRecord,
    packages: packageRecord,
    locations: locationRecord,
    recentEvents
  };
}
