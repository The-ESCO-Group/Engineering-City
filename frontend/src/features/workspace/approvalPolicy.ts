import type { WorkspaceApproval, WorkspaceApprovalDecision } from "./types";

export function approvalDecisionRequiresComment(
  approval: WorkspaceApproval,
  decision: WorkspaceApprovalDecision
): boolean {
  const highRisk = approval.severity === "high" || approval.severity === "critical";
  return highRisk || approval.restrictedDomain || approval.finalRelease || decision === "accept_risk" || decision === "reject" || decision === "request_changes";
}

