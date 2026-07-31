import type { EventEnvelope } from "../generated";
import {
  normalizeWorldEvent,
  worldEventSignature,
  type WorldEvent
} from "./events";
import {
  packageIdForProject,
  toProjectId,
  type AgentId,
  type LocationId,
  type PackageId,
  type ProjectId,
  type TaskId
} from "./ids";
import type {
  AppliedWorldEvent,
  WorldAgent,
  WorldAgentStatus,
  WorldEventStamp,
  WorldLocation,
  WorldPackage,
  WorldPackageState,
  WorldReducerLimits,
  WorldResyncRequest,
  WorldState,
  WorldStateOptions,
  WorldSyncIssueCode,
  WorldSyncStatus,
  WorldTask,
  WorldTaskStatus
} from "./types";

export const SUPPORTED_WORLD_EVENT_SCHEMA_VERSION = 1;

export const DEFAULT_WORLD_REDUCER_LIMITS: WorldReducerLimits = {
  maxPendingEvents: 128,
  maxRecentEvents: 256,
  maxAgents: 256,
  maxTasks: 2_048,
  maxLocations: 128
};

const TERMINAL_TASK_STATUSES = new Set<WorldTaskStatus>([
  "cancelled",
  "completed",
  "failed"
]);

const SYNC_MESSAGES: Readonly<Record<WorldSyncIssueCode, string>> = {
  schema_mismatch: "World event schema changed. Refresh project state.",
  invalid_envelope: "A malformed world event was received. Refresh project state.",
  invalid_payload: "A world event payload did not match its contract. Refresh project state.",
  conflicting_sequence: "Two different world events share a sequence. Refresh project state.",
  duplicate_event_id: "A world event ID was reused at another sequence. Refresh project state.",
  pending_overflow: "The event gap exceeded the local replay window. Refresh project state.",
  agent_capacity: "Agent state exceeded the local safety limit. Refresh project state.",
  task_capacity: "Task state exceeded the local safety limit. Refresh project state.",
  location_capacity: "Location state exceeded the local safety limit. Refresh project state."
};

function positiveLimit(value: number | undefined, fallback: number) {
  return value === undefined
    ? fallback
    : Math.max(1, Math.min(10_000, Math.floor(Number.isFinite(value) ? value : fallback)));
}

export function worldReducerLimits(
  overrides: Partial<WorldReducerLimits> | undefined
): WorldReducerLimits {
  return {
    maxPendingEvents: positiveLimit(
      overrides?.maxPendingEvents,
      DEFAULT_WORLD_REDUCER_LIMITS.maxPendingEvents
    ),
    maxRecentEvents: positiveLimit(
      overrides?.maxRecentEvents,
      DEFAULT_WORLD_REDUCER_LIMITS.maxRecentEvents
    ),
    maxAgents: positiveLimit(overrides?.maxAgents, DEFAULT_WORLD_REDUCER_LIMITS.maxAgents),
    maxTasks: positiveLimit(overrides?.maxTasks, DEFAULT_WORLD_REDUCER_LIMITS.maxTasks),
    maxLocations: positiveLimit(
      overrides?.maxLocations,
      DEFAULT_WORLD_REDUCER_LIMITS.maxLocations
    )
  };
}

function initialPackage(projectId: ProjectId): WorldPackage {
  return {
    id: packageIdForProject(projectId),
    projectId,
    state: "intake",
    sourceStatus: "",
    title: "Project package",
    taskId: null,
    revisionNumber: null,
    releaseCandidateId: null,
    locationId: null,
    previousLocationId: null,
    expectedArtifactCount: 0,
    artifactCount: 0,
    warningCount: 0,
    lastEvent: null
  };
}

export function createWorldState(projectIdValue: string, options: WorldStateOptions = {}): WorldState {
  const projectId = toProjectId(projectIdValue);
  const afterSequence = options.afterSequence ?? 0;
  if (projectId === null) throw new TypeError("A non-empty semantic project ID is required.");
  if (!Number.isSafeInteger(afterSequence) || afterSequence < 0) {
    throw new TypeError("afterSequence must be a non-negative safe integer.");
  }
  const projectPackage = initialPackage(projectId);
  return {
    projectId,
    schemaVersion: null,
    lastAppliedSequence: afterSequence,
    agents: {},
    tasks: {},
    packages: { [projectPackage.id]: projectPackage },
    locations: {},
    pendingEvents: [],
    recentEvents: [],
    resyncRequired: null,
    limits: worldReducerLimits(options.limits)
  };
}

function issueDetails(envelope: EventEnvelope) {
  const sequence = Number.isSafeInteger(envelope.seq) && envelope.seq > 0 ? envelope.seq : 0;
  const eventId = typeof envelope.id === "string" ? envelope.id.slice(0, 128) : "";
  return { sequence, eventId };
}

function requireResync(
  state: WorldState,
  code: WorldSyncIssueCode,
  event: Pick<WorldEvent, "sequence" | "eventId"> | EventEnvelope
): WorldState {
  const details =
    "sequence" in event
      ? { sequence: event.sequence, eventId: String(event.eventId) }
      : issueDetails(event);
  return {
    ...state,
    pendingEvents: [],
    resyncRequired: { code, ...details, message: SYNC_MESSAGES[code] }
  };
}

function eventStamp(event: WorldEvent): WorldEventStamp {
  return {
    eventId: event.eventId,
    sequence: event.sequence,
    occurredAt: event.occurredAt
  };
}

function normalizeAgentStatus(status: string): WorldAgentStatus {
  const value = status.trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_");
  if (["idle", "ready"].includes(value)) return "idle";
  if (["walking", "moving", "travelling", "traveling"].includes(value)) return "walking";
  if (["working", "running", "active", "executing"].includes(value)) return "working";
  if (["waiting", "queued", "pending"].includes(value)) return "waiting";
  if (["blocked", "failed", "error"].includes(value)) return "blocked";
  if (["review", "reviewing", "in_review"].includes(value)) return "reviewing";
  if (["awaiting_approval", "approval_required"].includes(value)) return "awaiting_approval";
  if (["completed", "complete", "succeeded", "success"].includes(value)) return "completed";
  if (["offline", "disconnected", "unavailable"].includes(value)) return "offline";
  return "unknown";
}

function normalizeTaskStatus(status: string): WorldTaskStatus {
  const value = status.trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_");
  if (["queued", "pending", "created"].includes(value)) return "queued";
  if (["assigned", "dispatched"].includes(value)) return "assigned";
  if (["running", "working", "started", "active"].includes(value)) return "running";
  if (["blocked", "waiting_on_input"].includes(value)) return "blocked";
  if (["awaiting_approval", "approval_required"].includes(value)) return "awaiting_approval";
  if (["completed", "complete", "succeeded", "success"].includes(value)) return "completed";
  if (["failed", "error"].includes(value)) return "failed";
  if (["cancelled", "canceled"].includes(value)) return "cancelled";
  return "unknown";
}

function packageStateForTask(taskKind: string, status: WorldTaskStatus): WorldPackageState {
  if (status === "blocked" || status === "failed" || status === "cancelled") return "blocked";
  if (status === "awaiting_approval") return "approval";
  const kind = taskKind.toLowerCase();
  if (["intake", "inventory_files"].includes(kind)) return "intake";
  if (["parse_file", "build_evidence_graph"].includes(kind)) return "parsing";
  if (["plan_project", "extract_requirements", "resolve_clarifications"].includes(kind)) {
    return "planning";
  }
  if (["review_plc", "review_interlocks", "senior_review"].includes(kind)) return "review";
  if (["request_approvals", "evaluate_approval_gate"].includes(kind)) return "approval";
  if (["validate_cross_discipline", "verify_import"].includes(kind)) return "validation";
  if (["generate_release_candidate", "assemble_package", "delivery", "finalize_project"].includes(kind)) {
    return "validation";
  }
  return "in_progress";
}

function sameIds(left: readonly string[], right: readonly string[]) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function rebuildLocations(
  locations: Readonly<Record<string, WorldLocation>>,
  agents: Readonly<Record<string, WorldAgent>>,
  tasks: Readonly<Record<string, WorldTask>>,
  packages: Readonly<Record<string, WorldPackage>>,
  sequence: number
): Readonly<Record<string, WorldLocation>> {
  const next: Record<string, WorldLocation> = {};
  for (const locationKey of Object.keys(locations).sort()) {
    const location = locations[locationKey];
    const agentIds = Object.values(agents)
      .filter((agent) => agent.locationId === location.id)
      .map((agent) => agent.id)
      .sort((left, right) => left.localeCompare(right));
    const activeTaskIds = Object.values(tasks)
      .filter(
        (task) => task.locationId === location.id && !TERMINAL_TASK_STATUSES.has(task.status)
      )
      .map((task) => task.id)
      .sort((left, right) => left.localeCompare(right));
    const packageIds = Object.values(packages)
      .filter((projectPackage) => projectPackage.locationId === location.id)
      .map((projectPackage) => projectPackage.id)
      .sort((left, right) => left.localeCompare(right));
    const changed =
      !sameIds(location.agentIds, agentIds) ||
      !sameIds(location.activeTaskIds, activeTaskIds) ||
      !sameIds(location.packageIds, packageIds);
    next[locationKey] = changed
      ? { ...location, agentIds, activeTaskIds, packageIds, updatedSequence: sequence }
      : location;
  }
  return next;
}

type MutationResult =
  | { readonly ok: true; readonly state: WorldState }
  | {
      readonly ok: false;
      readonly code: "agent_capacity" | "task_capacity" | "location_capacity";
    };

function hasCapacity(record: Readonly<Record<string, unknown>>, key: string, maximum: number) {
  return key in record || Object.keys(record).length < maximum;
}

function ensureLocation(
  state: WorldState,
  locationId: LocationId,
  sequence: number
): { readonly ok: true; readonly locations: Readonly<Record<string, WorldLocation>> } | {
  readonly ok: false;
  readonly code: "location_capacity";
} {
  if (!hasCapacity(state.locations, locationId, state.limits.maxLocations)) {
    return { ok: false, code: "location_capacity" };
  }
  if (state.locations[locationId]) return { ok: true, locations: state.locations };
  return {
    ok: true,
    locations: {
      ...state.locations,
      [locationId]: {
        id: locationId,
        agentIds: [],
        activeTaskIds: [],
        packageIds: [],
        updatedSequence: sequence
      }
    }
  };
}

function currentPackage(state: WorldState): WorldPackage {
  return state.packages[packageIdForProject(state.projectId)];
}

function updateProjectPackage(
  state: WorldState,
  event: WorldEvent,
  update: Partial<Omit<WorldPackage, "id" | "projectId" | "lastEvent">>
): Readonly<Record<string, WorldPackage>> {
  const current = currentPackage(state);
  const next = { ...current, ...update, lastEvent: eventStamp(event) };
  return { ...state.packages, [next.id]: next };
}

function applyTaskEvent(
  state: WorldState,
  event: Extract<WorldEvent, { kind: "task" }>
): MutationResult {
  if (!hasCapacity(state.tasks, event.taskId, state.limits.maxTasks)) {
    return { ok: false, code: "task_capacity" };
  }
  const current = state.tasks[event.taskId];
  const correlatedAgent = event.agentId === null ? undefined : state.agents[event.agentId];
  const status = normalizeTaskStatus(event.status);
  const task: WorldTask = {
    id: event.taskId,
    taskKind: event.taskKind,
    title: event.title,
    status,
    sourceStatus: event.status,
    agentKind: event.agentKind ?? current?.agentKind ?? correlatedAgent?.agentKind ?? "",
    agentId: event.agentId ?? current?.agentId ?? null,
    locationId: correlatedAgent?.locationId ?? current?.locationId ?? null,
    attempts: event.attempts ?? current?.attempts ?? 0,
    detail: event.detail ?? current?.detail ?? "",
    lastEvent: eventStamp(event)
  };
  const tasks = { ...state.tasks, [event.taskId]: task };
  const projectPackage = currentPackage(state);
  const packageLocation = task.locationId ?? projectPackage.locationId;
  let locations = state.locations;
  if (packageLocation !== null) {
    const ensured = ensureLocation(state, packageLocation, event.sequence);
    if (!ensured.ok) return ensured;
    locations = ensured.locations;
  }
  const packages = updateProjectPackage(state, event, {
    state: packageStateForTask(task.taskKind, task.status),
    sourceStatus: task.sourceStatus,
    title: task.title,
    taskId: task.id,
    previousLocationId:
      packageLocation !== projectPackage.locationId ? projectPackage.locationId : projectPackage.previousLocationId,
    locationId: packageLocation
  });
  return {
    ok: true,
    state: {
      ...state,
      tasks,
      packages,
      locations: rebuildLocations(locations, state.agents, tasks, packages, event.sequence)
    }
  };
}

function placeholderAgent(
  agentId: AgentId,
  agentKind: string,
  event: WorldEvent
): WorldAgent {
  return {
    id: agentId,
    agentKind,
    displayName: agentKind || String(agentId),
    status: "unknown",
    sourceStatus: "",
    currentActivity: "",
    taskId: null,
    locationId: null,
    targetLocationId: null,
    previousLocationId: null,
    lastEvent: eventStamp(event)
  };
}

function placeholderTask(taskId: TaskId, agent: WorldAgent, event: WorldEvent): WorldTask {
  return {
    id: taskId,
    taskKind: "",
    title: "",
    status: "unknown",
    sourceStatus: "",
    agentKind: agent.agentKind,
    agentId: agent.id,
    locationId: agent.locationId,
    attempts: 0,
    detail: "",
    lastEvent: eventStamp(event)
  };
}

function applyTaskAssignmentEvent(
  state: WorldState,
  event: Extract<WorldEvent, { kind: "task_assignment" }>
): MutationResult {
  if (!hasCapacity(state.tasks, event.taskId, state.limits.maxTasks)) {
    return { ok: false, code: "task_capacity" };
  }
  if (
    event.assignedAgentId !== null &&
    !hasCapacity(state.agents, event.assignedAgentId, state.limits.maxAgents)
  ) {
    return { ok: false, code: "agent_capacity" };
  }
  const ensured = ensureLocation(state, event.locationId, event.sequence);
  if (!ensured.ok) return ensured;

  const currentTask = state.tasks[event.taskId];
  const task: WorldTask = {
    id: event.taskId,
    taskKind: event.taskKind,
    title: event.title,
    status: "assigned",
    sourceStatus: "assigned",
    agentKind: event.agentKind,
    agentId: event.assignedAgentId,
    locationId: event.locationId,
    attempts: event.attempt,
    detail: currentTask?.detail ?? "",
    lastEvent: eventStamp(event)
  };
  const tasks = { ...state.tasks, [task.id]: task };
  let agents = state.agents;
  if (event.assignedAgentId !== null) {
    const currentAgent =
      state.agents[event.assignedAgentId] ??
      placeholderAgent(event.assignedAgentId, event.agentKind, event);
    const agent: WorldAgent = {
      ...currentAgent,
      agentKind: event.agentKind,
      status: currentAgent.locationId === event.locationId ? "working" : "walking",
      sourceStatus: "assigned",
      currentActivity: event.title,
      taskId: task.id,
      targetLocationId:
        currentAgent.locationId === event.locationId ? null : event.locationId,
      lastEvent: eventStamp(event)
    };
    agents = { ...agents, [agent.id]: agent };
  }
  const projectPackage = currentPackage(state);
  const packages = updateProjectPackage(state, event, {
    state: packageStateForTask(task.taskKind, task.status),
    sourceStatus: task.sourceStatus,
    title: task.title,
    taskId: task.id,
    previousLocationId:
      projectPackage.locationId === event.locationId
        ? projectPackage.previousLocationId
        : projectPackage.locationId,
    locationId: event.locationId
  });
  return {
    ok: true,
    state: {
      ...state,
      agents,
      tasks,
      packages,
      locations: rebuildLocations(ensured.locations, agents, tasks, packages, event.sequence)
    }
  };
}

function applyAgentStatusEvent(
  state: WorldState,
  event: Extract<WorldEvent, { kind: "agent_status" }>
): MutationResult {
  if (!hasCapacity(state.agents, event.agentId, state.limits.maxAgents)) {
    return { ok: false, code: "agent_capacity" };
  }
  if (event.taskId !== null && !hasCapacity(state.tasks, event.taskId, state.limits.maxTasks)) {
    return { ok: false, code: "task_capacity" };
  }

  const current = state.agents[event.agentId];
  const agent: WorldAgent = {
    id: event.agentId,
    agentKind: event.agentKind,
    displayName: event.displayName,
    status: normalizeAgentStatus(event.status),
    sourceStatus: event.status,
    currentActivity: event.currentActivity ?? current?.currentActivity ?? "",
    taskId: event.taskId,
    locationId: current?.locationId ?? null,
    targetLocationId: current?.targetLocationId ?? null,
    previousLocationId: current?.previousLocationId ?? null,
    lastEvent: eventStamp(event)
  };
  const agents = { ...state.agents, [event.agentId]: agent };
  let tasks = state.tasks;
  if (event.taskId !== null) {
    const currentTask = state.tasks[event.taskId] ?? placeholderTask(event.taskId, agent, event);
    tasks = {
      ...state.tasks,
      [event.taskId]: {
        ...currentTask,
        agentKind: currentTask.agentKind || agent.agentKind,
        agentId: agent.id,
        locationId: agent.locationId ?? currentTask.locationId,
        lastEvent: eventStamp(event)
      }
    };
  }
  return {
    ok: true,
    state: {
      ...state,
      agents,
      tasks,
      locations: rebuildLocations(
        state.locations,
        agents,
        tasks,
        state.packages,
        event.sequence
      )
    }
  };
}

function applyAgentLocationEvent(
  state: WorldState,
  event: Extract<WorldEvent, { kind: "agent_location" }>
): MutationResult {
  if (!hasCapacity(state.agents, event.agentId, state.limits.maxAgents)) {
    return { ok: false, code: "agent_capacity" };
  }
  if (event.taskId !== null && !hasCapacity(state.tasks, event.taskId, state.limits.maxTasks)) {
    return { ok: false, code: "task_capacity" };
  }
  const ensured = ensureLocation(state, event.locationId, event.sequence);
  if (!ensured.ok) return ensured;
  const current =
    state.agents[event.agentId] ?? placeholderAgent(event.agentId, event.agentKind, event);
  const previousLocationId = event.previousLocationId ?? current.locationId;
  const agent: WorldAgent = {
    ...current,
    agentKind: event.agentKind,
    displayName: current.displayName || event.agentKind || String(event.agentId),
    status: current.status === "walking" || current.status === "unknown" ? "working" : current.status,
    sourceStatus: current.sourceStatus || "location_changed",
    taskId: current.taskId ?? event.taskId,
    locationId: event.locationId,
    targetLocationId: null,
    previousLocationId,
    lastEvent: eventStamp(event)
  };
  const agents = { ...state.agents, [event.agentId]: agent };
  let tasks = state.tasks;
  if (agent.taskId !== null) {
    const task = state.tasks[agent.taskId] ?? placeholderTask(agent.taskId, agent, event);
    tasks = {
      ...state.tasks,
      [agent.taskId]: {
        ...task,
        agentId: agent.id,
        locationId: event.locationId,
        lastEvent: eventStamp(event)
      }
    };
  }
  let packages = state.packages;
  const projectPackage = currentPackage(state);
  if (
    agent.taskId !== null &&
    (projectPackage.taskId === null || projectPackage.taskId === agent.taskId)
  ) {
    packages = updateProjectPackage(state, event, {
      taskId: agent.taskId,
      previousLocationId:
        event.locationId === projectPackage.locationId
          ? projectPackage.previousLocationId
          : previousLocationId ?? projectPackage.locationId,
      locationId: event.locationId
    });
  }
  return {
    ok: true,
    state: {
      ...state,
      agents,
      tasks,
      packages,
      locations: rebuildLocations(ensured.locations, agents, tasks, packages, event.sequence)
    }
  };
}

function applyPackageEvent(
  state: WorldState,
  event: Extract<WorldEvent, { kind: "package" }>
): MutationResult {
  const projectPackage = currentPackage(state);
  let locations = state.locations;
  if (event.locationId !== null) {
    const ensured = ensureLocation(state, event.locationId, event.sequence);
    if (!ensured.ok) return ensured;
    locations = ensured.locations;
  }
  const locationId = event.locationId ?? projectPackage.locationId;
  const packages = updateProjectPackage(state, event, {
    state: event.packageState,
    sourceStatus: event.status,
    title: event.title ?? projectPackage.title,
    taskId: event.taskId ?? projectPackage.taskId,
    revisionNumber: event.revisionNumber ?? projectPackage.revisionNumber,
    releaseCandidateId: event.releaseCandidateId ?? projectPackage.releaseCandidateId,
    previousLocationId:
      locationId !== projectPackage.locationId
        ? projectPackage.locationId
        : projectPackage.previousLocationId,
    locationId,
    expectedArtifactCount:
      event.expectedArtifactCount ?? projectPackage.expectedArtifactCount,
    artifactCount:
      event.artifactCount ?? projectPackage.artifactCount + event.artifactDelta,
    warningCount:
      event.sourceType === "export.artifact_created"
        ? projectPackage.warningCount + (event.warningCount ?? 0)
        : event.warningCount ?? projectPackage.warningCount
  });
  return {
    ok: true,
    state: {
      ...state,
      packages,
      locations: rebuildLocations(locations, state.agents, state.tasks, packages, event.sequence)
    }
  };
}

function applyWorldEvent(state: WorldState, event: WorldEvent): MutationResult {
  if (event.kind === "task") return applyTaskEvent(state, event);
  if (event.kind === "task_assignment") return applyTaskAssignmentEvent(state, event);
  if (event.kind === "agent_status") return applyAgentStatusEvent(state, event);
  if (event.kind === "agent_location") return applyAgentLocationEvent(state, event);
  if (event.kind === "package") return applyPackageEvent(state, event);
  return { ok: true, state };
}

function appendRecent(state: WorldState, event: WorldEvent): readonly AppliedWorldEvent[] {
  const recent = [
    ...state.recentEvents,
    {
      sequence: event.sequence,
      eventId: event.eventId,
      signature: worldEventSignature(event)
    }
  ];
  return recent.slice(-state.limits.maxRecentEvents);
}

function drainPending(initial: WorldState): WorldState {
  let state = initial;
  while (
    state.pendingEvents.length > 0 &&
    state.pendingEvents[0].sequence === state.lastAppliedSequence + 1
  ) {
    const event = state.pendingEvents[0];
    const withoutEvent = { ...state, pendingEvents: state.pendingEvents.slice(1) };
    const mutation = applyWorldEvent(withoutEvent, event);
    if (!mutation.ok) return requireResync(withoutEvent, mutation.code, event);
    state = {
      ...mutation.state,
      schemaVersion: mutation.state.schemaVersion ?? event.schemaVersion,
      lastAppliedSequence: event.sequence,
      recentEvents: appendRecent(mutation.state, event)
    };
  }
  return state;
}

/**
 * Pure single-envelope reducer. Future envelopes are buffered until their gap
 * closes; exact duplicates are no-ops; contradictions or bounded-window
 * overflow request an authoritative resync instead of guessing.
 */
export function reduceWorldEvent(state: WorldState, envelope: EventEnvelope): WorldState {
  if (state.resyncRequired !== null || envelope.project_id !== state.projectId) return state;

  const normalized = normalizeWorldEvent(envelope);
  if (!normalized.ok) return requireResync(state, normalized.reason, envelope);
  const event = normalized.event;
  const signature = worldEventSignature(event);
  if (
    event.schemaVersion !== SUPPORTED_WORLD_EVENT_SCHEMA_VERSION ||
    (state.schemaVersion !== null && event.schemaVersion !== state.schemaVersion)
  ) {
    return requireResync(state, "schema_mismatch", event);
  }

  const appliedAtSequence = state.recentEvents.find(
    (candidate) => candidate.sequence === event.sequence
  );
  const appliedWithId = state.recentEvents.find(
    (candidate) => candidate.eventId === event.eventId
  );
  if (event.sequence <= state.lastAppliedSequence) {
    if (
      appliedAtSequence &&
      (appliedAtSequence.eventId !== event.eventId || appliedAtSequence.signature !== signature)
    ) {
      return requireResync(state, "conflicting_sequence", event);
    }
    if (appliedWithId && appliedWithId.sequence !== event.sequence) {
      return requireResync(state, "duplicate_event_id", event);
    }
    return state;
  }
  if (appliedWithId) return requireResync(state, "duplicate_event_id", event);

  const bufferedAtSequence = state.pendingEvents.find(
    (candidate) => candidate.sequence === event.sequence
  );
  if (bufferedAtSequence) {
    return bufferedAtSequence.eventId === event.eventId &&
      worldEventSignature(bufferedAtSequence) === signature
      ? state
      : requireResync(state, "conflicting_sequence", event);
  }
  const bufferedWithId = state.pendingEvents.find(
    (candidate) => candidate.eventId === event.eventId
  );
  if (bufferedWithId) return requireResync(state, "duplicate_event_id", event);

  const nextSequence = state.lastAppliedSequence + 1;
  if (
    event.sequence > state.lastAppliedSequence + state.limits.maxPendingEvents ||
    (event.sequence !== nextSequence && state.pendingEvents.length >= state.limits.maxPendingEvents)
  ) {
    return requireResync(state, "pending_overflow", event);
  }

  const pendingEvents = [...state.pendingEvents, event].sort(
    (left, right) => left.sequence - right.sequence || left.eventId.localeCompare(right.eventId)
  );
  return drainPending({ ...state, pendingEvents });
}

export function reduceWorldEvents(
  state: WorldState,
  envelopes: readonly EventEnvelope[]
): WorldState {
  return envelopes.reduce(reduceWorldEvent, state);
}

export function worldSyncStatus(state: WorldState): WorldSyncStatus {
  const nextSequence = state.lastAppliedSequence + 1;
  if (state.resyncRequired !== null) {
    return { status: "resync_required", nextSequence, issue: state.resyncRequired };
  }
  const firstBuffered = state.pendingEvents[0];
  return firstBuffered
    ? {
        status: "waiting_for_gap",
        nextSequence,
        firstBufferedSequence: firstBuffered.sequence,
        bufferedCount: state.pendingEvents.length
      }
    : { status: "ready", nextSequence };
}

export function worldResyncRequest(state: WorldState): WorldResyncRequest | null {
  return state.resyncRequired === null
    ? null
    : {
        projectId: state.projectId,
        afterSequence: state.lastAppliedSequence,
        reason: state.resyncRequired
      };
}

export const selectWorldAgents = (state: WorldState) =>
  Object.values(state.agents).sort((left, right) => left.id.localeCompare(right.id));

export const selectWorldTasks = (state: WorldState) =>
  Object.values(state.tasks).sort((left, right) => left.id.localeCompare(right.id));

export const selectWorldPackages = (state: WorldState) =>
  Object.values(state.packages).sort((left, right) => left.id.localeCompare(right.id));

export const selectWorldLocations = (state: WorldState) =>
  Object.values(state.locations).sort((left, right) => left.id.localeCompare(right.id));

export function worldAgentAt(state: WorldState, agentId: AgentId): WorldAgent | undefined {
  return state.agents[agentId];
}

export function worldTaskAt(state: WorldState, taskId: TaskId): WorldTask | undefined {
  return state.tasks[taskId];
}

export function worldPackageAt(
  state: WorldState,
  packageId: PackageId
): WorldPackage | undefined {
  return state.packages[packageId];
}

export function worldLocationAt(
  state: WorldState,
  locationId: LocationId
): WorldLocation | undefined {
  return state.locations[locationId];
}
