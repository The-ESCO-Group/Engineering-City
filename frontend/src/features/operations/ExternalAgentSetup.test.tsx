import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ExternalAgentDeployResponse } from "../../api/types";
import { ExternalAgentSetup } from "./ExternalAgentSetup";

afterEach(cleanup);

function deployment(): ExternalAgentDeployResponse {
  return {
    instance: {
      id: "02410000-0000-4000-8100-000000000099",
      project_id: "02410000-0000-4000-8000-000000000001",
      definition_id: "02410000-0000-4000-8000-000000000099",
      kind: "external_openai_test",
      display_name: "Orbit · Openai SDK",
      status: "walking",
      location: "project_intake",
      current_activity: "Connecting",
      current_task_id: null,
      updated_at: "2026-08-06T13:00:00Z"
    },
    agent_token: "signed-one-time-token",
    token_expires_at: "2026-08-06T14:00:00Z",
    provider: "openai",
    model: "gpt-5.6-sol",
    mission: "Review the available evidence.",
    allowed_actions: ["create_finding", "post_message"],
    endpoints: {
      context: "/api/external-agents/session/context",
      progress: "/api/external-agents/session/progress",
      submit: "/api/external-agents/session/submit"
    }
  };
}

describe("ExternalAgentSetup", () => {
  it("reduces deployment to provider, callsign, model, mission, and acknowledgement", async () => {
    const onDeploy = vi.fn().mockResolvedValue(deployment());
    render(
      <ExternalAgentSetup
        projectName="Line 4 Upgrade"
        serverBaseUrl="http://127.0.0.1:8000"
        onClose={vi.fn()}
        onDeploy={onDeploy}
      />
    );
    const dialog = screen.getByRole("dialog", { name: "Send an agent into the city" });

    expect(within(dialog).getByRole("button", { name: /OpenAI Agents SDK worker/ })).toHaveAttribute("aria-pressed", "true");
    expect(within(dialog).getByDisplayValue("Orbit")).toBeInTheDocument();
    expect(within(dialog).getByDisplayValue("gpt-5.6-sol")).toBeInTheDocument();
    fireEvent.change(within(dialog).getByPlaceholderText(/Review the uploaded requirements/), {
      target: { value: "Review the available evidence." }
    });
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Send agent" }));

    await waitFor(() => expect(onDeploy).toHaveBeenCalledTimes(1));
    expect(onDeploy).toHaveBeenCalledWith(expect.objectContaining({
      provider: "openai",
      display_name: "Orbit",
      model: "gpt-5.6-sol",
      mission: "Review the available evidence.",
      acknowledge_external_processing: true
    }));
    expect(await screen.findByRole("heading", { name: "Agent ready to launch" })).toBeInTheDocument();
    expect(screen.getByText(/Orbit · Openai SDK is now visible/)).toBeInTheDocument();
    expect(screen.getByDisplayValue("signed-one-time-token")).toBeInTheDocument();
    expect(screen.getByText(/openai_worker\.py/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/API key/i)).not.toBeInTheDocument();
  });

  it("switches to a Claude callsign and requires an account model id", () => {
    render(
      <ExternalAgentSetup
        projectName="Line 4 Upgrade"
        serverBaseUrl="http://127.0.0.1:8000"
        onClose={vi.fn()}
        onDeploy={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /Claude Anthropic SDK worker/ }));

    expect(screen.getByDisplayValue("Beacon")).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/Model ID available to your Anthropic account/)).toBeRequired();
    expect(screen.getByText(/send the granted project context to Anthropic/)).toBeInTheDocument();
  });
});
