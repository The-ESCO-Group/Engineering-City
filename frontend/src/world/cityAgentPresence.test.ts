import { describe, expect, it } from "vitest";
import type { AgentInstanceRead } from "../api/types";
import { toAgentId, toEventId, toLocationId, toTaskId } from "./ids";
import {
  buildCityAgentPresence,
  cityAgentPatrolOffset,
  nameCityAgents
} from "./cityAgentPresence";
import type { WorldAgent, WorldTask } from "./types";

const stamp = {
  eventId: toEventId("event-1")!,
  sequence: 8,
  occurredAt: "2026-08-04T12:00:00Z"
};

function task(
  id: string,
  agentKind: string,
  status: WorldTask["status"],
  locationId: string
): WorldTask {
  return {
    id: toTaskId(id)!,
    taskKind: id,
    title: `${agentKind} assignment`,
    status,
    sourceStatus: status,
    agentKind,
    agentId: null,
    locationId: toLocationId(locationId),
    attempts: 0,
    detail: "",
    lastEvent: stamp
  };
}

function agent(id: string, kind = "plc_logic"): WorldAgent {
  return {
    id: toAgentId(id)!,
    agentKind: kind,
    displayName: "Generic Agent",
    status: "idle",
    sourceStatus: "idle",
    currentActivity: "",
    taskId: null,
    locationId: toLocationId("automation_core"),
    targetLocationId: null,
    previousLocationId: null,
    lastEvent: stamp
  };
}

describe("city agent presence", () => {
  it("assigns stable unique names even when backend labels are identical", () => {
    const named = nameCityAgents([agent("agent-b"), agent("agent-a")]);
    expect(new Set(named.map((item) => item.displayName)).size).toBe(2);
    expect(nameCityAgents([agent("agent-b"), agent("agent-a")])).toEqual(named);
  });

  it("preserves a unique non-generic backend callsign", () => {
    const source = { ...agent("agent-atlas"), displayName: "Atlas Mercer" };
    expect(nameCityAgents([source])[0].displayName).toBe("Atlas Mercer");
  });

  it("merges live agent instances that have not emitted a replay event", () => {
    const instance: AgentInstanceRead = {
      id: "live-agent-1",
      project_id: "project-1",
      definition_id: "definition-1",
      kind: "senior_review",
      display_name: "Senior Review",
      status: "blocked",
      location: "approval_plaza",
      current_activity: "Waiting for the engineer",
      current_task_id: null,
      updated_at: "2026-08-04T12:02:00Z"
    };
    const result = buildCityAgentPresence({
      replayAgents: [],
      tasks: [],
      liveInstances: [instance],
      projectId: "project-1",
      projectActive: true,
      includeLivePresence: true,
      throughSequence: 12
    });
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      id: "live-agent-1",
      agentKind: "senior_review",
      status: "blocked",
      locationId: "approval_plaza",
      currentActivity: "Waiting for the engineer"
    });
    expect(result[0].displayName).not.toBe("Senior Review");
  });

  it("deploys one travelling worker per queued discipline during an active run", () => {
    const result = buildCityAgentPresence({
      replayAgents: [],
      tasks: [
        task("task-plc", "plc_logic", "queued", "ladder_logic_lab"),
        task("task-hmi", "hmi_engineering", "assigned", "hmi_district"),
        task("task-done", "senior_review", "completed", "qa_campus")
      ],
      projectId: "project-1",
      projectActive: true,
      includeLivePresence: true,
      throughSequence: 8
    });
    expect(result).toHaveLength(2);
    expect(result.map((item) => item.agentKind)).toEqual(["hmi_engineering", "plc_logic"]);
    expect(result.every((item) => item.status === "walking")).toBe(true);
    expect(result.every((item) => item.targetLocationId !== null)).toBe(true);
    expect(new Set(result.map((item) => item.displayName)).size).toBe(2);
  });

  it("does not invent queued workers for history playback or a finished project", () => {
    const queued = [task("task-plc", "plc_logic", "queued", "ladder_logic_lab")];
    const base = {
      replayAgents: [] as WorldAgent[],
      tasks: queued,
      liveInstances: [] as AgentInstanceRead[],
      projectId: "project-1",
      throughSequence: 8
    };
    expect(buildCityAgentPresence({ ...base, projectActive: true, includeLivePresence: false })).toEqual([]);
    expect(buildCityAgentPresence({ ...base, projectActive: false, includeLivePresence: true })).toEqual([]);
  });

  it("keeps patrol motion deterministic and disables it for reduced motion", () => {
    const id = toAgentId("agent-plc")!;
    expect(cityAgentPatrolOffset(id, "working", 12, true)).toEqual(
      cityAgentPatrolOffset(id, "working", 12, true)
    );
    expect(cityAgentPatrolOffset(id, "working", 12, false)).toEqual([0, 0]);
    expect(cityAgentPatrolOffset(id, "blocked", 12, true)).toEqual([0, 0]);
    expect(cityAgentPatrolOffset(id, "working", 12, true)).not.toEqual([0, 0]);
  });
});
