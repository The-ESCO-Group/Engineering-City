import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { demoAgentInstances, demoEvents, demoProject, demoTasks } from "../../api/demo";
import type { AgentMessageRead, AgentRunRead } from "../../api/types";
import { OperationsCenter } from "./OperationsCenter";
import type { OperationsSource } from "./types";

function demoSource(): OperationsSource {
  return {
    dataMode: "demo",
    fallbackReason: "Synthetic demo mode is active.",
    project: demoProject,
    agentDefinitions: [],
    agents: demoAgentInstances,
    agentRuns: [],
    tasks: demoTasks,
    messages: [],
    events: demoEvents
  };
}

afterEach(cleanup);

describe("OperationsCenter", () => {
  it("renders an honest, filterable read-only agent surface and supports city navigation", () => {
    const openCityLocation = vi.fn();
    render(<OperationsCenter source={demoSource()} view="agents" onOpenCityLocation={openCityLocation} />);

    expect(screen.getByText("DEMO DATA · SYNTHETIC OFFLINE")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Agent operations" })).toBeInTheDocument();
    expect(screen.getByText(/This view has no agent-control actions/)).toBeInTheDocument();
    expect(screen.getByText("5", { selector: ".ops-summary-grid dd" })).toBeInTheDocument();
    expect(screen.getByText("No instance-linked run is present in the loaded 0-run project window.")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/Search agent, role, task, or location/), { target: { value: "electrical" } });
    const list = screen.getByRole("list", { name: "Reported agents" });
    expect(within(list).getByText("Electrical Agent")).toBeInTheDocument();
    expect(within(list).queryByText("Requirements Agent")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Focus location in city" }));
    expect(openCityLocation).toHaveBeenCalledWith("electrical_works");
  });

  it("renders only contracted definition, run, and instance-message metadata", () => {
    const agent = demoAgentInstances[0];
    const message: AgentMessageRead = {
      id: "message-instance",
      project_id: demoProject.id,
      role: "agent",
      agent_kind: agent.kind,
      agent_instance_id: agent.id,
      body: "Instance-specific update",
      created_at: "2026-08-03T12:00:00Z"
    };
    const run: AgentRunRead = {
      id: "run-instance",
      project_id: demoProject.id,
      agent_kind: agent.kind,
      agent_instance_id: agent.id,
      status: "completed",
      provider: "local-provider",
      model: "engineering-model",
      prompt_tokens: 120,
      completion_tokens: 45,
      rationale: "Checked the published requirement set.",
      started_at: "2026-08-03T12:00:00Z"
    };
    const source = demoSource();
    source.agents = [agent];
    source.agentDefinitions = [{
      id: agent.definition_id,
      kind: agent.kind,
      display_name: agent.display_name,
      description: "Requirement planning definition",
      is_available: false,
      unavailable_reason: "Required local plugin is unavailable."
    }];
    source.agentRuns = [run];
    source.messages = [message];

    render(<OperationsCenter source={source} view="agents" />);

    expect(screen.getByText("Requirement planning definition")).toBeInTheDocument();
    expect(screen.getByText("Required local plugin is unavailable.")).toBeInTheDocument();
    expect(screen.getByText("Instance-specific update")).toBeInTheDocument();
    expect(screen.getByText("Checked the published requirement set.")).toBeInTheDocument();
    expect(screen.getByText(/local-provider · engineering-model · 120 in \/ 45 out tokens/)).toBeInTheDocument();
  });

  it("labels Audit Trail as a recent stream window and exposes exact event payload fields", () => {
    render(<OperationsCenter source={demoSource()} view="audit" />);

    expect(screen.getByRole("heading", { name: "Audit Trail", level: 2 })).toBeInTheDocument();
    expect(screen.getByText(/Recent stream window—not a separate audit-read service/)).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/Search event type, sequence, IDs, or payload/), { target: { value: "approval.requested" } });
    const eventList = screen.getByRole("list", { name: "Recent project events" });
    fireEvent.click(within(eventList).getByRole("button", { name: /Approval Requested/ }));

    expect(screen.getByRole("heading", { name: "Approval Requested" })).toBeInTheDocument();
    expect(screen.getByText("approval_id")).toBeInTheDocument();
    expect(screen.getByText("Review guard-reset discrepancy", { selector: "pre" })).toBeInTheDocument();
    expect(screen.getByText(/No mutation, approval, export, or replay action/)).toBeInTheDocument();
  });

  it("shows an explicit unavailable state without substituting demo records", () => {
    const source: OperationsSource = {
      dataMode: "unavailable",
      fallbackReason: "Sign in to the local backend to view an authorized project.",
      project: null,
      agentDefinitions: [],
      agents: [],
      agentRuns: [],
      tasks: [],
      messages: [],
      events: []
    };
    render(<OperationsCenter source={source} view="agents" />);

    expect(screen.getByRole("alert")).toHaveTextContent("Sign in to the local backend");
    expect(screen.getAllByText("0", { selector: ".ops-summary-grid dd" })).toHaveLength(5);
    expect(screen.queryByText("Requirements Agent")).not.toBeInTheDocument();
  });
});
