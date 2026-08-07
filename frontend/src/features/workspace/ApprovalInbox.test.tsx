import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createDemoWorkspaceModel } from "./demo";
import { ApprovalInbox } from "./ApprovalInbox";
import type { WorkspaceApproval } from "./types";

const capability = { level: "available" as const, reason: "Gate decisions are available." };

afterEach(cleanup);

describe("ApprovalInbox decision controls", () => {
  it("keeps richer verdicts disabled for an ordinary approval", () => {
    render(<ApprovalInbox approvals={[createDemoWorkspaceModel().approvals[0]]} capability={capability} />);

    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Request changes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Accept risk" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Defer" })).toBeDisabled();
    expect(screen.queryByRole("heading", { name: "Validation blockers & waivers" })).not.toBeInTheDocument();
  });

  it("enables every verdict for an eligible gate approval", () => {
    const approval: WorkspaceApproval = {
      ...createDemoWorkspaceModel().approvals[0],
      kind: "gate_decision",
      gateId: "gate-1",
      gateKey: "qa_and_risk",
      allowedDecisions: ["approve", "reject", "request_changes", "accept_risk", "defer"]
    };

    render(<ApprovalInbox approvals={[approval]} capability={capability} />);

    for (const name of ["Approve", "Reject", "Request changes", "Accept risk", "Defer"]) {
      expect(screen.getByRole("button", { name })).toBeEnabled();
    }
  });
});
