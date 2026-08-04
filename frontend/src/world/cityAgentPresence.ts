import type { AgentInstanceRead } from "../api/types";
import {
  toAgentId,
  toEventId,
  toSemanticLocationId,
  toTaskId,
  type AgentId,
  type LocationId
} from "./ids";
import type { WorldAgent, WorldAgentStatus, WorldTask } from "./types";

const NON_TERMINAL_TASK_STATUSES = new Set<WorldTask["status"]>([
  "queued",
  "assigned",
  "running",
  "blocked",
  "awaiting_approval"
]);

const ROLE_PERSONAS: Readonly<Record<string, readonly string[]>> = Object.freeze({
  project_manager: ["Mara Voss", "Jonah Vale", "Priya North"],
  requirements_planning: ["Avery Quinn", "Lena Brooks", "Owen Hart"],
  interlock_review: ["Rina Solis", "Caleb Ward", "Zara Flint"],
  electrical_io: ["Eli Circuit", "Noah Ampere", "Sana Wells"],
  plc_review: ["Theo Mercer", "June Keller", "Marcus Reed"],
  plc_logic: ["Theo Mercer", "June Keller", "Marcus Reed"],
  tag_architecture: ["Nia Vector", "Arlo Finch", "Mei Rowan"],
  hmi_engineering: ["Mika Chen", "Cora Lane", "Dev Shah"],
  alarm_engineering: ["Iris Hale", "Ben Foster", "Amara Knox"],
  testing_commissioning: ["Tessa Forge", "Leo Grant", "Mina Park"],
  senior_review: ["Sol Reyes", "Ada Monroe", "Drew Lawson"],
  report_delivery: ["Dorian Ledger", "Esme Stone", "Kai Bennett"]
});

const FALLBACK_PERSONAS = [
  "Mara Voss",
  "Avery Quinn",
  "Rina Solis",
  "Theo Mercer",
  "Nia Vector",
  "Mika Chen",
  "Iris Hale",
  "Tessa Forge",
  "Sol Reyes",
  "Dorian Ledger",
  "Eli Circuit",
  "Priya North"
] as const;

const ROLE_DESTINATIONS: Readonly<Record<string, LocationId>> = Object.freeze({
  project_manager: location("project_core"),
  requirements_planning: location("requirements_hall"),
  interlock_review: location("electrical_works"),
  electrical_io: location("electrical_works"),
  plc_review: location("automation_core"),
  plc_logic: location("ladder_logic_lab"),
  tag_architecture: location("tag_foundry"),
  hmi_engineering: location("hmi_district"),
  alarm_engineering: location("alarm_center"),
  testing_commissioning: location("testing_grounds"),
  senior_review: location("qa_campus"),
  report_delivery: location("documentation_quarter")
});

const ROLE_ORIGINS: Readonly<Record<string, LocationId>> = Object.freeze({
  project_manager: location("project_intake"),
  requirements_planning: location("project_intake"),
  interlock_review: location("project_core"),
  electrical_io: location("project_core"),
  plc_review: location("project_core"),
  plc_logic: location("automation_core"),
  tag_architecture: location("template_library"),
  hmi_engineering: location("tag_foundry"),
  alarm_engineering: location("hmi_district"),
  testing_commissioning: location("qa_campus"),
  senior_review: location("testing_grounds"),
  report_delivery: location("qa_campus")
});

export interface CityAgentPresenceOptions {
  readonly replayAgents: readonly WorldAgent[];
  readonly tasks: readonly WorldTask[];
  readonly liveInstances?: readonly AgentInstanceRead[];
  readonly projectId: string;
  readonly projectActive: boolean;
  readonly includeLivePresence: boolean;
  readonly throughSequence: number;
  readonly occurredAt?: string;
}

function location(value: string): LocationId {
  const result = toSemanticLocationId(value);
  if (result === null) throw new TypeError(`Unknown city agent location: ${value}`);
  return result;
}

function normalizeKind(value: string) {
  return value.trim().toLowerCase().replaceAll(/[^a-z0-9]+/g, "_").replaceAll(/^_+|_+$/g, "");
}

function stableHash(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function preferredPersona(kind: string, ordinal: number, id: string) {
  const options = ROLE_PERSONAS[normalizeKind(kind)] ?? FALLBACK_PERSONAS;
  return options[(ordinal + stableHash(id)) % options.length];
}

const GENERIC_AGENT_LABELS = new Set([
  "project_manager",
  "requirements_and_planning",
  "requirements_agent",
  "electrical_agent",
  "plc_review",
  "plc_logic",
  "plc_logic_agent",
  "interlock_review",
  "senior_review",
  "qa_review_agent",
  "tag_architecture",
  "hmi_agent",
  "hmi_engineering_agent",
  "alarm_engineering_agent",
  "electrical_and_i_o_agent",
  "testing_and_commissioning_agent",
  "documentation_and_release_agent"
]);

function isGenericAgentLabel(displayName: string, agentKind: string) {
  const label = normalizeKind(displayName);
  const kind = normalizeKind(agentKind);
  return !label || label === kind || label === `${kind}_agent` || GENERIC_AGENT_LABELS.has(label);
}

/**
 * Gives every visible worker a stable human-readable call sign. Backend role
 * names remain available through `agentKind`; this field is presentation only.
 */
export function nameCityAgents(agents: readonly WorldAgent[]): WorldAgent[] {
  const ordered = [...agents].sort((left, right) => left.id.localeCompare(right.id));
  const used = new Set<string>();
  const ordinalByKind = new Map<string, number>();
  const sourceNameCounts = new Map<string, number>();
  ordered.forEach((agent) => {
    const key = agent.displayName.trim().toLocaleLowerCase();
    if (key) sourceNameCounts.set(key, (sourceNameCounts.get(key) ?? 0) + 1);
  });
  const result = ordered.map((agent) => {
    const kind = normalizeKind(agent.agentKind);
    const ordinal = ordinalByKind.get(kind) ?? 0;
    ordinalByKind.set(kind, ordinal + 1);
    const sourceName = agent.displayName.trim();
    const sourceNameIsUnique = sourceNameCounts.get(sourceName.toLocaleLowerCase()) === 1;
    let name = sourceName && sourceNameIsUnique && !isGenericAgentLabel(sourceName, kind)
      ? sourceName
      : preferredPersona(kind, ordinal, agent.id);
    if (used.has(name)) {
      const firstUnused = FALLBACK_PERSONAS.find((candidate) => !used.has(candidate));
      name = firstUnused ?? `${name} ${ordinal + 1}`;
    }
    used.add(name);
    return { ...agent, displayName: name };
  });
  return result;
}

function worldStatus(status: AgentInstanceRead["status"]): WorldAgentStatus {
  return status;
}

function eventIdFor(value: string) {
  const result = toEventId(`city-presence:${value}`);
  if (result === null) throw new TypeError(`Invalid city presence event id: ${value}`);
  return result;
}

function taskAgentId(projectId: string, agentKind: string): AgentId {
  const result = toAgentId(`city-crew:${projectId}:${normalizeKind(agentKind)}`);
  if (result === null) throw new TypeError(`Invalid city crew agent id: ${agentKind}`);
  return result;
}

function taskStatusToAgentStatus(status: WorldTask["status"]): WorldAgentStatus {
  if (status === "blocked") return "blocked";
  if (status === "awaiting_approval") return "awaiting_approval";
  if (status === "running") return "working";
  return "walking";
}

function taskActivity(task: WorldTask) {
  if (task.status === "blocked") return `Blocked · ${task.title}`;
  if (task.status === "awaiting_approval") return `Awaiting review · ${task.title}`;
  if (task.status === "running") return task.title;
  return `Queued · ${task.title}`;
}

function targetForTask(task: WorldTask): LocationId {
  return task.locationId ?? ROLE_DESTINATIONS[normalizeKind(task.agentKind)] ?? location("project_core");
}

function originForTask(task: WorldTask, target: LocationId): LocationId {
  const origin = ROLE_ORIGINS[normalizeKind(task.agentKind)] ?? location("project_intake");
  return origin === target ? location("project_core") : origin;
}

function mergeLiveInstance(
  current: WorldAgent | undefined,
  instance: AgentInstanceRead,
  throughSequence: number
): WorldAgent | null {
  const id = toAgentId(instance.id);
  if (id === null) return null;
  const locationId = toSemanticLocationId(instance.location || "");
  const taskId = toTaskId(instance.current_task_id);
  return {
    id,
    agentKind: instance.kind || current?.agentKind || "engineering_agent",
    displayName: instance.display_name || current?.displayName || instance.kind,
    status: worldStatus(instance.status),
    sourceStatus: instance.status,
    currentActivity: instance.current_activity || current?.currentActivity || "",
    taskId: taskId ?? current?.taskId ?? null,
    locationId: locationId ?? current?.locationId ?? null,
    targetLocationId: current?.targetLocationId ?? null,
    previousLocationId: current?.previousLocationId ?? null,
    lastEvent: current?.lastEvent ?? {
      eventId: eventIdFor(instance.id),
      sequence: throughSequence,
      occurredAt: instance.updated_at
    }
  };
}

/**
 * Builds the city-only workforce view. Live project instances fill event-stream
 * gaps, while non-terminal queued tasks get a deterministic travelling crew
 * marker so an active workflow is visible before an agent emits its first
 * status event. No proxy workers are created for finished projects or history
 * playback.
 */
export function buildCityAgentPresence(options: CityAgentPresenceOptions): WorldAgent[] {
  const agents = new Map<AgentId, WorldAgent>(
    options.replayAgents.map((agent) => [agent.id, agent])
  );

  if (options.includeLivePresence) {
    for (const instance of options.liveInstances ?? []) {
      const id = toAgentId(instance.id);
      if (id === null) continue;
      const merged = mergeLiveInstance(agents.get(id), instance, options.throughSequence);
      if (merged) agents.set(id, merged);
    }
  }

  if (options.includeLivePresence && options.projectActive) {
    const taskByKind = new Map<string, WorldTask>();
    for (const task of [...options.tasks].sort((left, right) => {
      const sequence = right.lastEvent.sequence - left.lastEvent.sequence;
      return sequence || left.id.localeCompare(right.id);
    })) {
      const kind = normalizeKind(task.agentKind);
      if (!kind || taskByKind.has(kind) || !NON_TERMINAL_TASK_STATUSES.has(task.status)) continue;
      taskByKind.set(kind, task);
    }

    for (const [kind, task] of taskByKind) {
      const existing = [...agents.values()].find(
        (agent) => normalizeKind(agent.agentKind) === kind
      );
      if (existing && existing.status !== "idle" && existing.status !== "completed") continue;
      const targetLocationId = targetForTask(task);
      const originLocationId = originForTask(task, targetLocationId);
      const next: WorldAgent = {
        id: existing?.id ?? taskAgentId(options.projectId, kind),
        agentKind: task.agentKind,
        displayName: existing?.displayName ?? task.agentKind,
        status: taskStatusToAgentStatus(task.status),
        sourceStatus: `scheduled:${task.sourceStatus || task.status}`,
        currentActivity: taskActivity(task),
        taskId: task.id,
        locationId: originLocationId,
        targetLocationId,
        previousLocationId: originLocationId,
        lastEvent: task.lastEvent
      };
      agents.set(next.id, next);
    }
  }

  return nameCityAgents([...agents.values()]);
}

const PATROLLING_STATUSES = new Set<WorldAgentStatus>(["walking", "working", "reviewing"]);

/** A deterministic local work circuit used only after an event-driven route completes. */
export function cityAgentPatrolOffset(
  agentId: AgentId,
  status: WorldAgentStatus,
  elapsedSeconds: number,
  motionEnabled: boolean
): readonly [number, number] {
  if (!motionEnabled || !PATROLLING_STATUSES.has(status)) return [0, 0];
  const hash = stableHash(agentId);
  const phase = ((hash % 360) * Math.PI) / 180;
  const speed = 0.36 + ((hash >>> 9) % 15) / 100;
  const radius = 1.45 + ((hash >>> 17) % 55) / 100;
  const angle = elapsedSeconds * speed + phase;
  return [
    Math.cos(angle) * radius,
    Math.sin(angle * 2) * radius * 0.46
  ];
}
