import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { toAgentId, toEventId, toLocationId, toTaskId } from "../world/ids";
import type { WorldAgent, WorldTask } from "../world/types";
import { AgentInspector, type AgentInspectorProps } from "./AgentInspector";

afterEach(cleanup);

const agentId = toAgentId("agent-plc-01")!;
const taskId = toTaskId("task-42")!;
const locationId = toLocationId("ladder_logic_lab")!;
const eventId = toEventId("evt-12")!;

const agent: WorldAgent = {
  id: agentId,
  agentKind: "PLC reviewer",
  displayName: "Atlas",
  status: "working",
  sourceStatus: "working",
  currentActivity: "Checking motor permissives",
  taskId,
  locationId,
  targetLocationId: null,
  previousLocationId: null,
  lastEvent: { eventId, sequence: 12, occurredAt: "2026-07-31T18:00:00Z" }
};

const task: WorldTask = {
  id: taskId,
  taskKind: "plc_review",
  title: "Review conveyor interlocks",
  status: "running",
  sourceStatus: "in_progress",
  agentKind: "PLC reviewer",
  agentId,
  locationId,
  attempts: 1,
  detail: "Line 4 package",
  lastEvent: { eventId, sequence: 12, occurredAt: "2026-07-31T18:00:00Z" }
};

function inspectorProps(overrides: Partial<AgentInspectorProps> = {}): AgentInspectorProps {
  return {
    open: true,
    agent,
    task,
    project: { id: "project-line-4", name: "Line 4 Packaging Conveyor Upgrade" },
    supplemental: {
      locationName: "Ladder Logic Lab",
      progress: { current: 3, total: 5, label: "Review checks" },
      blocker: "Awaiting the approved motor list.",
      confidencePercent: 86,
      evidence: ["Motor schedule revision C", "PLC export checksum"],
      provider: "Local runtime",
      model: "review-model-v2",
      reportedCost: "$0.18",
      costBasis: "this task",
      lastAction: "Flagged an unresolved safety relay reference.",
      recentEvents: [
        {
          id: "event-1",
          label: "Task started",
          detail: "Loaded PLC package",
          occurredAt: "2026-07-31T17:58:00Z"
        }
      ],
      pendingApproval: {
        title: "Motor list revision",
        detail: "Engineering approval is required before continuing."
      },
      publicRationale: "The relay reference does not match the approved drawing set.",
      warnings: ["One source document is older than the active package."]
    },
    onClose: vi.fn(),
    ...overrides
  };
}

describe("AgentInspector", () => {
  it("renders reported workflow facts without inventing actions", () => {
    render(<AgentInspector {...inspectorProps()} />);

    expect(screen.getByRole("dialog", { name: "Atlas" })).toBeInTheDocument();
    expect(screen.getByText("PLC reviewer")).toBeInTheDocument();
    expect(screen.getByText("working")).toBeInTheDocument();
    expect(screen.getByText("Ladder Logic Lab")).toBeInTheDocument();
    expect(screen.getByText("Line 4 Packaging Conveyor Upgrade")).toBeInTheDocument();
    expect(screen.getByText("Review conveyor interlocks")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Review checks" })).toHaveAttribute(
      "aria-valuenow",
      "3"
    );
    expect(screen.getByText("Awaiting the approved motor list.")).toBeInTheDocument();
    expect(screen.getByText("86%")).toBeInTheDocument();
    expect(screen.getByText("Motor schedule revision C")).toBeInTheDocument();
    expect(screen.getByText("review-model-v2")).toBeInTheDocument();
    expect(screen.getByText("$0.18")).toBeInTheDocument();
    expect(screen.getByText("Flagged an unresolved safety relay reference.")).toBeInTheDocument();
    expect(screen.getByText("Task started")).toBeInTheDocument();
    expect(screen.getByText(/Pending approval · Motor list revision/)).toBeInTheDocument();
    expect(screen.getByText("The relay reference does not match the approved drawing set.")).toBeInTheDocument();
    expect(screen.getByText("One source document is older than the active package.")).toBeInTheDocument();
    expect(screen.getByText(/Private chain-of-thought is never exposed/)).toBeInTheDocument();

    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Close agent inspector" })).toBeInTheDocument();
  });

  it("omits optional execution metadata when it was not reported", () => {
    render(<AgentInspector {...inspectorProps({ supplemental: {} })} />);

    expect(screen.queryByText("Reported execution facts")).not.toBeInTheDocument();
    expect(screen.queryByText("Pending approval", { exact: false })).not.toBeInTheDocument();
    expect(screen.queryByText("Operational notes")).not.toBeInTheDocument();
    expect(screen.getByText("Checking motor permissives")).toBeInTheDocument();
  });

  it("closes on Escape and restores the previous focus when removed", () => {
    const onClose = vi.fn();
    const launch = document.createElement("button");
    launch.textContent = "Open inspector";
    document.body.append(launch);
    launch.focus();

    const view = render(<AgentInspector {...inspectorProps({ onClose })} />);
    expect(screen.getByRole("button", { name: "Close agent inspector" })).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    view.rerender(<AgentInspector {...inspectorProps({ open: false, onClose })} />);
    expect(launch).toHaveFocus();
    launch.remove();
  });

  it("returns no panel when closed or no agent is selected", () => {
    const view = render(<AgentInspector {...inspectorProps({ open: false })} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    view.rerender(<AgentInspector {...inspectorProps({ open: true, agent: null })} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
