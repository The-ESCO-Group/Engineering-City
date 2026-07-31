import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { toAgentId, toEventId, toLocationId } from "../world/ids";
import type { WorldAgent } from "../world/types";
import { WorkflowAgentDock } from "./WorkflowAgentDock";

const agentId = toAgentId("agent-1")!;
const agent: WorldAgent = {
  id: agentId,
  agentKind: "plc_logic",
  displayName: "PLC Logic Agent",
  status: "working",
  sourceStatus: "working",
  currentActivity: "Reviewing interlocks",
  taskId: null,
  locationId: toLocationId("ladder_logic_lab"),
  targetLocationId: null,
  previousLocationId: null,
  lastEvent: {
    eventId: toEventId("event-1")!,
    sequence: 7,
    occurredAt: "2026-07-31T14:12:00Z"
  }
};

describe("WorkflowAgentDock", () => {
  it("offers a keyboard-accessible inspector target for each rendered agent", () => {
    const onSelectAgent = vi.fn();
    render(
      <WorkflowAgentDock
        agents={[agent]}
        selectedAgentId={null}
        onSelectAgent={onSelectAgent}
      />
    );

    const button = screen.getByRole("button", {
      name: "Inspect PLC Logic Agent, working"
    });
    expect(button).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(button);
    expect(onSelectAgent).toHaveBeenCalledWith(agentId);
  });
});
