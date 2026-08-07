import { describe, expect, it } from "vitest";
import type { EventEnvelope, EventType } from "../generated";
import { demoEvents } from "../api/demo";
import {
  createWorldStateAdapter,
  createWorldState,
  deriveWorldReplayView,
  normalizeWorldEvent,
  reduceWorldEvent,
  reduceWorldEvents,
  replayWorldEvents,
  restoreWorldStateSnapshot,
  selectWorldPackages,
  serializeWorldStateSnapshot,
  worldResyncRequest,
  worldSyncStatus
} from ".";

const PROJECT_ID = "project-1";
const AGENT_ID = "agent-1";
const TASK_ID = "task-1";

function event(
  sequence: number,
  eventType: EventType,
  payload: Record<string, unknown> = {},
  overrides: Partial<EventEnvelope> = {}
): EventEnvelope {
  return {
    id: `event-${sequence}`,
    schema_version: 1,
    seq: sequence,
    event_type: eventType,
    project_id: PROJECT_ID,
    agent_id: null,
    task_id: null,
    actor_user_id: null,
    occurred_at: `2026-07-31T12:00:${String(sequence).padStart(2, "0")}Z`,
    payload,
    ...overrides
  };
}

function taskEvent(
  sequence: number,
  eventType: Extract<
    EventType,
    "task.created" | "task.started" | "task.completed" | "task.failed" | "task.blocked"
  >,
  status: string,
  extra: Record<string, unknown> = {}
) {
  return event(
    sequence,
    eventType,
    {
      task_id: TASK_ID,
      kind: "plc_review",
      title: "Review PLC logic",
      status,
      ...extra
    },
    { task_id: TASK_ID }
  );
}

function agentStatusEvent(
  sequence: number,
  agentId = AGENT_ID,
  taskId: string | null = TASK_ID
) {
  return event(
    sequence,
    "agent.status_changed",
    {
      agent_id: agentId,
      agent_kind: "plc_review",
      display_name: "PLC Review",
      status: "working",
      current_activity: "Reviewing interlocks",
      task_id: taskId
    },
    { agent_id: agentId, task_id: taskId }
  );
}

function agentLocationEvent(sequence: number, location: string, agentId = AGENT_ID) {
  return event(
    sequence,
    "agent.location_changed",
    {
      agent_id: agentId,
      agent_kind: "plc_review",
      location,
      previous_location: ""
    },
    { agent_id: agentId }
  );
}

function taskAssignedEvent(sequence: number, location: string, agentId = AGENT_ID) {
  return event(
    sequence,
    "task.assigned",
    {
      task_id: TASK_ID,
      kind: "validate_cross_discipline",
      title: "Validate cross-discipline package",
      agent_kind: "senior_review",
      agent_id: agentId,
      location,
      attempt: 1
    },
    { agent_id: agentId, task_id: TASK_ID }
  );
}

describe("world event normalization", () => {
  it("rejects correlation mismatches and non-semantic locations", () => {
    const mismatch = agentStatusEvent(1);
    const normalizedMismatch = normalizeWorldEvent({ ...mismatch, task_id: "another-task" });
    expect(normalizedMismatch).toEqual({ ok: false, reason: "invalid_payload" });

    const coordinateLocation = normalizeWorldEvent(agentLocationEvent(1, "12.5,40.1"));
    expect(coordinateLocation).toEqual({ ok: false, reason: "invalid_payload" });
  });

  it("accepts change-set abandonment without inventing project state", () => {
    // The backend event is newer than the generated contract artifacts. The
    // assertion is local to this compatibility test until the contract owner
    // publishes the updated EventType union.
    const envelope = event(
      1,
      "changeset.abandoned" as EventType,
      {
        changeset_id: "changeset-1",
        title: "Superseded motor template",
        status: "abandoned",
        reason: "Superseded by the revised motor template."
      }
    );

    const normalized = normalizeWorldEvent(envelope);
    expect(normalized).toMatchObject({
      ok: true,
      event: { kind: "noop", sourceType: "changeset.abandoned" }
    });

    const initial = createWorldState(PROJECT_ID);
    const state = reduceWorldEvent(initial, envelope);
    expect(state.resyncRequired).toBeNull();
    expect(state.lastAppliedSequence).toBe(1);
    expect(state.packages).toEqual(initial.packages);
  });
});

describe("world reducer", () => {
  it("buffers out-of-order envelopes and joins agents, tasks, and locations by semantic ID", () => {
    const envelopes = [
      taskEvent(1, "task.created", "queued", { agent_kind: "plc_review" }),
      agentStatusEvent(2),
      agentLocationEvent(3, "qa_campus"),
      taskEvent(4, "task.started", "running", { attempts: 1 })
    ];

    const state = reduceWorldEvents(createWorldState(PROJECT_ID), [
      envelopes[2],
      envelopes[0],
      envelopes[3],
      envelopes[1]
    ]);

    expect(state.lastAppliedSequence).toBe(4);
    expect(state.pendingEvents).toEqual([]);
    expect(state.agents[AGENT_ID]).toMatchObject({
      id: AGENT_ID,
      taskId: TASK_ID,
      locationId: "qa_campus",
      status: "working"
    });
    expect(state.tasks[TASK_ID]).toMatchObject({
      id: TASK_ID,
      agentId: AGENT_ID,
      locationId: "qa_campus",
      status: "running",
      attempts: 1
    });
    expect(state.locations.qa_campus).toMatchObject({
      agentIds: [AGENT_ID],
      activeTaskIds: [TASK_ID]
    });
    expect(worldSyncStatus(state)).toEqual({ status: "ready", nextSequence: 5 });
  });

  it("moves linked work between semantic locations without retaining stale occupancy", () => {
    const state = reduceWorldEvents(createWorldState(PROJECT_ID), [
      taskEvent(1, "task.created", "queued"),
      agentStatusEvent(2),
      agentLocationEvent(3, "qa_campus"),
      agentLocationEvent(4, "approval_plaza")
    ]);

    expect(state.locations.qa_campus.agentIds).toEqual([]);
    expect(state.locations.qa_campus.activeTaskIds).toEqual([]);
    expect(state.locations.approval_plaza.agentIds).toEqual([AGENT_ID]);
    expect(state.locations.approval_plaza.activeTaskIds).toEqual([TASK_ID]);
    expect(state.tasks[TASK_ID].locationId).toBe("approval_plaza");
  });

  it("makes an exact duplicate an identity-preserving no-op", () => {
    const envelope = taskEvent(1, "task.created", "queued");
    const applied = reduceWorldEvent(createWorldState(PROJECT_ID), envelope);
    expect(reduceWorldEvent(applied, envelope)).toBe(applied);
  });

  it("rejects a same-ID, same-sequence event whose normalized payload changed", () => {
    const envelope = taskEvent(1, "task.created", "queued");
    const applied = reduceWorldEvent(createWorldState(PROJECT_ID), envelope);
    const altered = {
      ...envelope,
      payload: { ...envelope.payload, status: "running" }
    };
    const state = reduceWorldEvent(applied, altered);

    expect(state.resyncRequired?.code).toBe("conflicting_sequence");
    expect(worldResyncRequest(state)).toMatchObject({
      projectId: PROJECT_ID,
      afterSequence: 1,
      reason: { code: "conflicting_sequence" }
    });
  });

  it("requests resync for a contradictory event at an applied sequence", () => {
    const applied = reduceWorldEvent(
      createWorldState(PROJECT_ID),
      taskEvent(1, "task.created", "queued")
    );
    const conflicting = event(1, "project.updated", {}, { id: "conflicting-event" });
    const state = reduceWorldEvent(applied, conflicting);

    expect(state.resyncRequired?.code).toBe("conflicting_sequence");
    expect(state.lastAppliedSequence).toBe(1);
  });

  it("advances the contiguous cursor for unrelated contract events", () => {
    const state = reduceWorldEvent(
      createWorldState(PROJECT_ID),
      event(1, "project.updated", { project_id: PROJECT_ID, name: "ESCO", status: "active" })
    );
    expect(state.lastAppliedSequence).toBe(1);
    expect(state.agents).toEqual({});
    expect(state.tasks).toEqual({});
  });

  it("preserves fields omitted by additive task updates", () => {
    const state = reduceWorldEvents(createWorldState(PROJECT_ID), [
      taskEvent(1, "task.created", "queued", {
        agent_kind: "plc_review",
        attempts: 2,
        detail: "Waiting for an input file"
      }),
      taskEvent(2, "task.started", "running")
    ]);
    expect(state.tasks[TASK_ID]).toMatchObject({
      agentKind: "plc_review",
      attempts: 2,
      detail: "Waiting for an input file",
      status: "running"
    });
  });

  it("uses payload status for blocked tasks", () => {
    const state = reduceWorldEvents(createWorldState(PROJECT_ID), [
      taskEvent(1, "task.created", "queued"),
      taskEvent(2, "task.blocked", "awaiting_approval", {
        detail: "Human approval required"
      })
    ]);
    expect(state.tasks[TASK_ID].status).toBe("awaiting_approval");
    expect(state.tasks[TASK_ID].detail).toBe("Human approval required");
  });

  it("bounds gaps and entity growth by requiring an authoritative resync", () => {
    const gapState = reduceWorldEvent(
      createWorldState(PROJECT_ID, { limits: { maxPendingEvents: 2 } }),
      taskEvent(3, "task.created", "queued")
    );
    expect(gapState.resyncRequired?.code).toBe("pending_overflow");
    expect(gapState.pendingEvents).toHaveLength(0);

    const capacityState = reduceWorldEvents(
      createWorldState(PROJECT_ID, { limits: { maxAgents: 1 } }),
      [agentStatusEvent(1, "agent-1", null), agentStatusEvent(2, "agent-2", null)]
    );
    expect(capacityState.resyncRequired?.code).toBe("agent_capacity");
    expect(Object.keys(capacityState.agents)).toEqual(["agent-1"]);
    expect(capacityState.lastAppliedSequence).toBe(1);
  });

  it("reports and drains a bounded event gap", () => {
    const waiting = reduceWorldEvent(
      createWorldState(PROJECT_ID),
      agentStatusEvent(2, AGENT_ID, null)
    );
    expect(worldSyncStatus(waiting)).toEqual({
      status: "waiting_for_gap",
      nextSequence: 1,
      firstBufferedSequence: 2,
      bufferedCount: 1
    });

    const ready = reduceWorldEvent(
      waiting,
      event(1, "project.updated", {
        project_id: PROJECT_ID,
        name: "ESCO",
        status: "active"
      })
    );
    expect(ready.lastAppliedSequence).toBe(2);
    expect(worldSyncStatus(ready)).toEqual({ status: "ready", nextSequence: 3 });
  });

  it("turns task assignment and arrival into deterministic agent and package movement", () => {
    const assigned = reduceWorldEvents(createWorldState(PROJECT_ID), [
      agentStatusEvent(1, AGENT_ID, null),
      taskAssignedEvent(2, "qa_campus")
    ]);
    const projectPackage = selectWorldPackages(assigned)[0];

    expect(assigned.agents[AGENT_ID]).toMatchObject({
      status: "walking",
      taskId: TASK_ID,
      locationId: null,
      targetLocationId: "qa_campus"
    });
    expect(assigned.tasks[TASK_ID]).toMatchObject({
      status: "assigned",
      locationId: "qa_campus",
      attempts: 1
    });
    expect(projectPackage).toMatchObject({
      state: "validation",
      taskId: TASK_ID,
      locationId: "qa_campus"
    });
    expect(assigned.locations.qa_campus.packageIds).toEqual([projectPackage.id]);

    const arrived = reduceWorldEvent(assigned, agentLocationEvent(3, "qa_campus"));
    expect(arrived.agents[AGENT_ID]).toMatchObject({
      status: "working",
      locationId: "qa_campus",
      targetLocationId: null
    });
  });

  it("tracks release package state and artifact counts without discarding release events", () => {
    const releaseCandidateId = "candidate-1";
    const state = reduceWorldEvents(createWorldState(PROJECT_ID), [
      event(1, "project.created", { name: "Line 4", status: "intake" }),
      event(2, "export.started", {
        release_candidate_id: releaseCandidateId,
        candidate_number: 1,
        revision_number: 4,
        location: "release_logistics",
        expected_artifact_count: 2
      }),
      event(3, "export.artifact_created", {
        release_candidate_id: releaseCandidateId,
        artifact_id: "artifact-1",
        filename: "controller.L5X",
        kind: "controller_project_l5x",
        location: "release_logistics",
        validation_level: "round_trip_parsed",
        warning_count: 1
      }),
      event(4, "export.artifact_created", {
        release_candidate_id: releaseCandidateId,
        artifact_id: "artifact-2",
        filename: "manifest.json",
        kind: "manifest",
        location: "release_logistics",
        validation_level: "schema_validated",
        warning_count: 0
      }),
      event(5, "release.candidate_created", {
        release_candidate_id: releaseCandidateId,
        candidate_number: 1,
        revision_number: 4,
        location: "release_logistics",
        status: "generated",
        validation_level: "round_trip_parsed",
        artifact_count: 2
      }),
      event(6, "release.ready", {
        release_candidate_id: releaseCandidateId,
        candidate_number: 1,
        revision_number: 4,
        location: "release_logistics",
        status: "released",
        validation_level: "human_approved",
        import_verification_status: "not_attempted",
        artifact_count: 2
      })
    ]);

    expect(selectWorldPackages(state)[0]).toMatchObject({
      state: "released",
      releaseCandidateId,
      revisionNumber: 4,
      expectedArtifactCount: 2,
      artifactCount: 2,
      locationId: "release_logistics",
      sourceStatus: "released"
    });
    expect(state.lastAppliedSequence).toBe(6);
  });
});

describe("world replay", () => {
  it("consumes the contract-shaped product demo without a resync", () => {
    const state = replayWorldEvents(PROJECT_ID, demoEvents.map((item) => ({
      ...item,
      project_id: PROJECT_ID
    })));

    expect(state.resyncRequired).toBeNull();
    expect(state.lastAppliedSequence).toBe(demoEvents.length);
    expect(Object.keys(state.agents)).toHaveLength(5);
  });

  it("produces the same serializable state for ordered, shuffled, and duplicate delivery", () => {
    const ordered = [
      event(1, "project.updated", {
        project_id: PROJECT_ID,
        name: "ESCO",
        status: "active"
      }),
      taskEvent(2, "task.created", "queued", { agent_kind: "plc_review" }),
      agentStatusEvent(3),
      agentLocationEvent(4, "testing_grounds"),
      taskEvent(5, "task.blocked", "awaiting_approval", {
        detail: "Reviewer decision pending"
      })
    ];
    const shuffled = [ordered[3], ordered[1], ordered[4], ordered[0], ordered[2], ordered[2]];
    const beforeReplay = shuffled.map((item) => item.id);

    const expected = replayWorldEvents(PROJECT_ID, ordered);
    const replayed = replayWorldEvents(PROJECT_ID, shuffled);
    const delivered = reduceWorldEvents(createWorldState(PROJECT_ID), shuffled);

    expect(replayed).toEqual(expected);
    expect(delivered).toEqual(expected);
    expect(shuffled.map((item) => item.id)).toEqual(beforeReplay);
  });

  it("selects a deterministic event prefix for timeline scrubbing", () => {
    const envelopes = [
      taskEvent(1, "task.created", "queued"),
      agentStatusEvent(2),
      agentLocationEvent(3, "testing_grounds")
    ];
    const prefix = deriveWorldReplayView(PROJECT_ID, envelopes, { throughSequence: 2 });
    const complete = deriveWorldReplayView(PROJECT_ID, envelopes, { throughSequence: 3 });

    expect(prefix.state.lastAppliedSequence).toBe(2);
    expect(prefix.agents[0].locationId).toBeNull();
    expect(complete.agents[0].locationId).toBe("testing_grounds");
    expect(complete.sync).toEqual({ status: "ready", nextSequence: 4 });
  });
});

describe("world snapshots and adapter", () => {
  it("round-trips a validated contiguous snapshot and rejects corrupted semantic state", () => {
    const state = reduceWorldEvents(createWorldState(PROJECT_ID), [
      agentStatusEvent(1, AGENT_ID, null),
      taskAssignedEvent(2, "qa_campus"),
      agentLocationEvent(3, "qa_campus")
    ]);
    const serialized = serializeWorldStateSnapshot(state);
    expect(restoreWorldStateSnapshot(serialized)).toEqual(state);

    const corrupted = JSON.parse(serialized) as {
      agents: Array<{ locationId: string }>;
    };
    corrupted.agents[0].locationId = "12.5,40.1";
    expect(restoreWorldStateSnapshot(JSON.stringify(corrupted))).toBeNull();
  });

  it("publishes once per changed batch and supports authoritative replay replacement", () => {
    const adapter = createWorldStateAdapter(PROJECT_ID);
    let notifications = 0;
    const unsubscribe = adapter.subscribe(() => {
      notifications += 1;
    });
    const envelopes = [
      taskEvent(1, "task.created", "queued"),
      agentStatusEvent(2)
    ];

    adapter.ingestMany(envelopes);
    expect(notifications).toBe(1);
    adapter.ingest(envelopes[1]);
    expect(notifications).toBe(1);
    expect(adapter.getView()).toMatchObject({
      agents: [{ id: AGENT_ID }],
      tasks: [{ id: TASK_ID }],
      sync: { status: "ready", nextSequence: 3 }
    });

    adapter.replaceFromReplay([...envelopes, agentLocationEvent(3, "testing_grounds")]);
    expect(notifications).toBe(2);
    expect(adapter.getState().agents[AGENT_ID].locationId).toBe("testing_grounds");
    expect(adapter.restoreSnapshot(adapter.serializeSnapshot())).toBe(true);
    expect(notifications).toBe(3);
    unsubscribe();
  });
});
