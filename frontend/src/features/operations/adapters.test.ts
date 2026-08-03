import { describe, expect, it } from "vitest";
import { demoAgentInstances, demoEvents, demoProject, demoTasks } from "../../api/demo";
import type { AgentMessageRead } from "../../api/types";
import { buildOperationsModel } from "./adapters";
import type { OperationsSource } from "./types";

function demoSource(overrides: Partial<OperationsSource> = {}): OperationsSource {
  return {
    dataMode: "demo",
    fallbackReason: "Synthetic demo mode is active.",
    project: demoProject,
    agents: demoAgentInstances,
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
      role: "assistant",
      agent_kind: hmi!.kind,
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
