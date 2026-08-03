import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FindingDetail } from "../../api/types";
import { createDemoWorkspaceModel } from "./demo";
import { FindingsWorkspace } from "./FindingsWorkspace";

afterEach(cleanup);

describe("FindingsWorkspace", () => {
  it("hydrates a live summary selected by a deep-link record query", async () => {
    const model = createDemoWorkspaceModel();
    const requested = model.findings[1];
    const findings = model.findings.map((finding) => finding.id === requested.id
      ? { ...finding, evidence: [], evidenceCount: 1, detailLoaded: false, fact: "", exactLocation: "Location loads on demand" }
      : finding);
    const detail: FindingDetail = {
      id: requested.id,
      project_id: "project-live",
      title: requested.title,
      category: requested.category,
      severity: "high",
      status: "open",
      fact: "Hydrated fact from the local backend.",
      inference: requested.inference,
      suggested_review_action: requested.suggestedAction,
      limitations: requested.limitations,
      confidence: requested.confidence ?? 0,
      requires_approval: requested.requiresApproval,
      is_reviewed: false,
      created_by_agent_kind: requested.agent,
      evidence_count: 1,
      decision_notes: "",
      created_at: "2026-08-03T12:00:00Z",
      updated_at: "2026-08-03T12:00:00Z",
      evidence: [{
        id: "evidence-live-1",
        anchor_id: "anchor-live-1",
        evidence_kind: "direct",
        role: "supporting",
        note: "Loaded on demand",
        anchor: {
          id: "anchor-live-1",
          source_label: "Local I/O schedule.csv",
          display_location: "Row 42 · Address column",
          excerpt: "PE-402,Local:2:I.Data.4",
          expectation: "Address matches the reviewed controller alias"
        }
      }],
      details: {},
      warnings: []
    };
    const loadFindingDetail = vi.fn().mockResolvedValue(detail);

    render(<FindingsWorkspace findings={findings} initialFindingId={requested.id} capability={model.capabilities.findingEvidence} loadFindingDetail={loadFindingDetail} />);

    await waitFor(() => expect(loadFindingDetail).toHaveBeenCalledWith(requested.id));
    expect(await screen.findByText("Hydrated fact from the local backend.")).toBeInTheDocument();
    expect(screen.getAllByText("Row 42 · Address column").length).toBeGreaterThan(0);
  });

  it("shows an explicit not-found state instead of substituting a finding", () => {
    const model = createDemoWorkspaceModel();
    render(<FindingsWorkspace findings={model.findings} initialFindingId="missing-finding" capability={model.capabilities.findingEvidence} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Finding link not found");
    expect(screen.queryByText(model.findings[0].fact)).not.toBeInTheDocument();
  });

  it("does not reapply the original route selection after a live refresh", () => {
    const model = createDemoWorkspaceModel();
    const props = { findings: model.findings, initialFindingId: model.findings[1].id, capability: model.capabilities.findingEvidence };
    const { rerender } = render(<FindingsWorkspace {...props} />);
    fireEvent.click(screen.getByRole("button", { name: new RegExp(model.findings[0].id) }));
    expect(screen.getByRole("heading", { name: model.findings[0].title })).toBeInTheDocument();
    rerender(<FindingsWorkspace {...props} findings={[...model.findings]} />);
    expect(screen.getByRole("heading", { name: model.findings[0].title })).toBeInTheDocument();
  });
});
