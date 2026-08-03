import { describe, expect, it } from "vitest";
import { demoAgentInstances, demoEvents, demoProject, demoTasks } from "../../api/demo";
import type { AgentInstanceRead, AgentMessageRead, AgentRunRead, TaskRead } from "../../api/types";
import { buildOperationsModel } from "./adapters";
import type { OperationsSource } from "./types";

function demoSource(overrides: Partial<OperationsSource> = {}): OperationsSource {
  return {
    dataMode: "demo",
    fallbackReason: "Synthetic demo mode is active.",
    project: demoProject,
    agentDefinitions: [],
    agents: demoAgentInstances,
    agentRuns: [],
    tasks: demoTasks,
    messages: [],
    events: demoEvents,
    ...overrides
  };
}

describe("operations adapters", () => {
  it("links only published task, message, and event records to an agent snapshot", () => {
    const hmi = demoAgentInstances.find((agent) => agent.display_name === "HMI Agent");
    expect(hmi).toBeDefined();
    const message: AgentMessageRead = {
      id: "message-hmi",
      project_id: demoProject.id,
      role: "agent",
      agent_kind: hmi!.kind,
      agent_instance_id: hmi!.id,
      body: "Published HMI review summary.",
      context: {},
      created_at: "2026-07-31T15:20:00Z"
    };
    const model = buildOperationsModel(demoSource({ messages: [message] }));
    const record = model.agents.find((agent) => agent.id === hmi!.id);

    expect(record?.currentTask?.title).toBe("Review HMI and alarm bindings");
    expect(record?.messages.map((item) => item.body)).toEqual(["Published HMI review summary."]);
    expect(record?.recentEvents.every((event) => event.agent_id === hmi!.id || event.payload.agent_id === hmi!.id)).toBe(true);
  });

  it("keeps tasks, messages, and runs isolated between two instances of the same kind", () => {
    const templateAgent = demoAgentInstances[3];
    const templateTask = demoTasks[3];
    const firstAgent: AgentInstanceRead = {
      ...templateAgent,
      id: "agent-hmi-a",
      display_name: "HMI Agent A",
      current_task_id: "task-hmi-b"
    };
    const secondAgent: AgentInstanceRead = {
      ...templateAgent,
      id: "agent-hmi-b",
      display_name: "HMI Agent B",
      current_task_id: "task-hmi-b"
    };
    const task = (id: string, instanceId: string | null): TaskRead => ({
      ...templateTask,
      id,
      title: id,
      agent_instance_id: instanceId
    });
    const message = (id: string, instanceId: string | null): AgentMessageRead => ({
      id,
      project_id: demoProject.id,
      role: "agent",
      agent_kind: templateAgent.kind,
      agent_instance_id: instanceId,
      body: id,
      context: {},
      created_at: "2026-08-03T12:00:00Z"
    });
    const run = (id: string, instanceId: string | null): AgentRunRead => ({
      id,
      project_id: demoProject.id,
      agent_kind: templateAgent.kind,
      agent_instance_id: instanceId,
      status: "completed",
      started_at: "2026-08-03T12:00:00Z"
    });
    const model = buildOperationsModel(demoSource({
      agents: [firstAgent, secondAgent],
      tasks: [task("task-hmi-a", firstAgent.id), task("task-hmi-b", secondAgent.id), task("task-kind-only", null)],
      messages: [message("message-hmi-a", firstAgent.id), message("message-hmi-b", secondAgent.id), message("message-kind-only", null)],
      agentRuns: [run("run-hmi-a", firstAgent.id), run("run-hmi-b", secondAgent.id), run("run-kind-only", null)],
      events: []
    }));
    const first = model.agents.find((agent) => agent.id === firstAgent.id);
    const second = model.agents.find((agent) => agent.id === secondAgent.id);

    expect(first?.currentTask?.id).toBe("task-hmi-a");
    expect(first?.relatedTaskCount).toBe(1);
    expect(first?.messages.map((item) => item.id)).toEqual(["message-hmi-a"]);
    expect(first?.recentRuns.map((item) => item.id)).toEqual(["run-hmi-a"]);
    expect(second?.currentTask?.id).toBe("task-hmi-b");
    expect(second?.relatedTaskCount).toBe(1);
    expect(second?.messages.map((item) => item.id)).toEqual(["message-hmi-b"]);
    expect(second?.recentRuns.map((item) => item.id)).toEqual(["run-hmi-b"]);
  });

  it("keeps exact sequence data, reports internal gaps, and classifies review events without changing payloads", () => {
    const approval = demoEvents.find((event) => event.event_type === "approval.requested");
    expect(approval).toBeDefined();
    const model = buildOperationsModel(demoSource({ events: [demoEvents[0], approval!] }));
    const auditApproval = model.audit.find((event) => event.id === approval!.id);

    expect(model.firstSequence).toBe(demoEvents[0].seq);
    expect(model.latestSequence).toBe(approval!.seq);
    expect(model.sequenceGapCount).toBe(1);
    expect(auditApproval).toMatchObject({ category: "Review", signal: "attention", payload: approval!.payload });
    expect(auditApproval?.summary).toBe("Review guard-reset discrepancy");
  });
});
