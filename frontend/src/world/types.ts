import type { WorldEvent } from "./events";
import type {
  AgentId,
  EventId,
  LocationId,
  PackageId,
  ProjectId,
  ReleaseCandidateId,
  TaskId
} from "./ids";

export interface WorldEventStamp {
  readonly eventId: EventId;
  readonly sequence: number;
  readonly occurredAt: string;
}

export type WorldAgentStatus =
  | "idle"
  | "walking"
  | "working"
  | "waiting"
  | "blocked"
  | "reviewing"
  | "awaiting_approval"
  | "completed"
  | "offline"
  | "unknown";

export type WorldTaskStatus =
  | "queued"
  | "assigned"
  | "running"
  | "blocked"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled"
  | "unknown";

export type WorldPackageState =
  | "intake"
  | "parsing"
  | "planning"
  | "in_progress"
  | "blocked"
  | "review"
  | "approval"
  | "validation"
  | "released";

export interface WorldAgent {
  readonly id: AgentId;
  readonly agentKind: string;
  readonly displayName: string;
  /** Stable UI state derived from contract events. */
  readonly status: WorldAgentStatus;
  /** Original backend status, kept for inspectors without inventing detail. */
  readonly sourceStatus: string;
  readonly currentActivity: string;
  readonly taskId: TaskId | null;
  readonly locationId: LocationId | null;
  /** Destination set by task.assigned and cleared by the matching location event. */
  readonly targetLocationId: LocationId | null;
  readonly previousLocationId: LocationId | null;
  readonly lastEvent: WorldEventStamp;
}

export interface WorldTask {
  readonly id: TaskId;
  readonly taskKind: string;
  readonly title: string;
  readonly status: WorldTaskStatus;
  readonly sourceStatus: string;
  readonly agentKind: string;
  readonly agentId: AgentId | null;
  readonly locationId: LocationId | null;
  readonly attempts: number;
  readonly detail: string;
  readonly lastEvent: WorldEventStamp;
}

/** One event-driven project capsule that can be rendered travelling between districts. */
export interface WorldPackage {
  readonly id: PackageId;
  readonly projectId: ProjectId;
  readonly state: WorldPackageState;
  readonly sourceStatus: string;
  readonly title: string;
  readonly taskId: TaskId | null;
  readonly revisionNumber: number | null;
  readonly releaseCandidateId: ReleaseCandidateId | null;
  readonly locationId: LocationId | null;
  readonly previousLocationId: LocationId | null;
  readonly expectedArtifactCount: number;
  readonly artifactCount: number;
  readonly warningCount: number;
  readonly lastEvent: WorldEventStamp | null;
}

export interface WorldLocation {
  readonly id: LocationId;
  readonly agentIds: readonly AgentId[];
  readonly activeTaskIds: readonly TaskId[];
  readonly packageIds: readonly PackageId[];
  readonly updatedSequence: number;
}

export interface WorldReducerLimits {
  readonly maxPendingEvents: number;
  readonly maxRecentEvents: number;
  readonly maxAgents: number;
  readonly maxTasks: number;
  readonly maxLocations: number;
}

export type WorldSyncIssueCode =
  | "schema_mismatch"
  | "invalid_envelope"
  | "invalid_payload"
  | "conflicting_sequence"
  | "duplicate_event_id"
  | "pending_overflow"
  | "agent_capacity"
  | "task_capacity"
  | "location_capacity";

export interface WorldSyncIssue {
  readonly code: WorldSyncIssueCode;
  readonly sequence: number;
  readonly eventId: string;
  /** Concise operator-facing warning. Never contains model reasoning. */
  readonly message: string;
}

export interface AppliedWorldEvent {
  readonly sequence: number;
  readonly eventId: EventId;
  /** Canonical normalized event signature used for exact duplicate checks. */
  readonly signature: string;
}

export interface WorldState {
  readonly projectId: ProjectId;
  readonly schemaVersion: number | null;
  readonly lastAppliedSequence: number;
  readonly agents: Readonly<Record<string, WorldAgent>>;
  readonly tasks: Readonly<Record<string, WorldTask>>;
  readonly packages: Readonly<Record<string, WorldPackage>>;
  readonly locations: Readonly<Record<string, WorldLocation>>;
  readonly pendingEvents: readonly WorldEvent[];
  readonly recentEvents: readonly AppliedWorldEvent[];
  readonly resyncRequired: WorldSyncIssue | null;
  readonly limits: WorldReducerLimits;
}

export interface WorldStateOptions {
  readonly afterSequence?: number;
  readonly limits?: Partial<WorldReducerLimits>;
}

export type WorldSyncStatus =
  | { readonly status: "ready"; readonly nextSequence: number }
  | {
      readonly status: "waiting_for_gap";
      readonly nextSequence: number;
      readonly firstBufferedSequence: number;
      readonly bufferedCount: number;
    }
  | {
      readonly status: "resync_required";
      readonly nextSequence: number;
      readonly issue: WorldSyncIssue;
    };

export interface WorldResyncRequest {
  readonly projectId: ProjectId;
  readonly afterSequence: number;
  readonly reason: WorldSyncIssue;
}
