import { describe, expect, it } from "vitest";
import { createDemoWorkspaceModel } from "./demo";
import { approvalDecisionRequiresComment } from "./approvalPolicy";

describe("approval comment policy", () => {
  const [critical, medium] = createDemoWorkspaceModel().approvals;

  it("requires comments for every high-consequence decision", () => {
    expect(approvalDecisionRequiresComment(critical, "approve")).toBe(true);
    expect(approvalDecisionRequiresComment(critical, "defer")).toBe(true);
  });

  it("requires comments for rejection, requested changes, and risk acceptance", () => {
    expect(approvalDecisionRequiresComment(medium, "reject")).toBe(true);
    expect(approvalDecisionRequiresComment(medium, "request_changes")).toBe(true);
    expect(approvalDecisionRequiresComment(medium, "accept_risk")).toBe(true);
  });

  it("allows a low/medium nonrestricted approval only when policy permits", () => {
    expect(approvalDecisionRequiresComment(medium, "approve")).toBe(false);
  });
});

