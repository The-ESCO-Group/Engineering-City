import { describe, expect, it } from "vitest";
import { buildWorkspaceModel, inferEvidenceSourceKind, normalizeTaskState } from "./adapters";
import type { WorkspaceSource } from "./types";

const liveSource: WorkspaceSource = {
  dataMode: "live",
  fallbackReason: "",
  project: {
    id: "project-1",
    name: "Synthetic project",
    description: "Synthetic only",
    client_reference: "SYN-001",
    status: "in_progress",
    is_paused: false,
    event_seq: 4,
    created_at: "2026-07-31T10:00:00Z",
    updated_at: "2026-07-31T11:00:00Z",
    attributes: {},
    counts: { files: 0, files_parsed: 0, tasks: 0, tasks_completed: 0, findings: 0, findings_open: 0, approvals_pending: 0, reports: 0 }
  },
  files: [],
  tasks: [],
  findings: [],
  approvals: [],
  events: [
    {
      id: "event-plan",
      schema_version: 1,
      seq: 1,
      event_type: "workflow.planned",
      project_id: "project-1",
      agent_id: null,
      task_id: null,
      actor_user_id: null,
      occurred_at: "2026-07-31T10:01:00Z",
      payload: { location: "requirements_hall", summary: "Trace synthetic sources.", task_count: 3, risk_count: 1, task_counts_by_kind: { requirements: 1, qa_review: 2 } }
    },
    {
      id: "event-requirement",
      schema_version: 1,
      seq: 2,
      event_type: "requirement.created",
      project_id: "project-1",
      agent_id: null,
      task_id: null,
      actor_user_id: null,
      occurred_at: "2026-07-31T10:02:00Z",
      payload: { requirement_id: "req-1", reference: "SYN-R-1", title: "Synthetic requirement", kind: "functional", priority: "mandatory", status: "proposed", location: "requirements_hall", acceptance_criteria_count: 2 }
    },
    {
      id: "event-changeset",
      schema_version: 1,
      seq: 3,
      event_type: "changeset.submitted",
      project_id: "project-1",
      agent_id: null,
      task_id: null,
      actor_user_id: null,
      occurred_at: "2026-07-31T10:03:00Z",
      payload: { changeset_id: "cs-1", title: "Synthetic proposal", status: "in_review", base_revision_number: 1, location: "tag_foundry", operation_count: 5 }
    },
    {
      id: "event-revision",
      schema_version: 1,
      seq: 4,
      event_type: "revision.created",
      project_id: "project-1",
      agent_id: null,
      task_id: null,
      actor_user_id: null,
      occurred_at: "2026-07-31T10:04:00Z",
      payload: { revision_id: "rev-2", number: 2, parent_revision_number: 1, location: "project_core", summary: "Synthetic revision", operation_count: 5, object_counts_by_type: { tag: 5 } }
    }
  ]
};

describe("workspace contract adapters", () => {
  it("maps versioned summary events without fabricating canonical object details", () => {
    const model = buildWorkspaceModel(liveSource);
    expect(model.mode).toBe("live");
    expect(model.project?.revision).toBe(2);
    expect(model.plans[0]).toMatchObject({ taskCount: 3, riskCount: 1 });
    expect(model.requirements[0]).toMatchObject({ reference: "SYN-R-1", acceptanceCriteriaCount: 2, acceptanceCriteria: [] });
    expect(model.requirements[0].text).toContain("pending Project Core resource contract");
    expect(model.changesets[0]).toMatchObject({ operationCount: 5, operationsAvailable: false, operations: [] });
    expect(model.capabilities.changesetOperations.level).toBe("summary_only");
  });

  it("keeps demo and live records strictly separated", () => {
    const demo = buildWorkspaceModel({ ...liveSource, dataMode: "demo" });
    expect(demo.demoLabel).toContain("DEMO DATA");
    expect(demo.project?.id).toBe("demo-project-line-4");
    expect(demo.requirements.some((item) => item.id === "req-1")).toBe(false);
  });

  it("normalizes backend task states without estimating unknown states", () => {
    expect(normalizeTaskState("in_progress")).toBe("running");
    expect(normalizeTaskState("awaiting approval")).toBe("awaiting_approval");
    expect(normalizeTaskState("vendor_specific_state")).toBe("unknown");
  });

  it("recognizes exact evidence source families", () => {
    expect(inferEvidenceSourceKind("Controller.L5X", "direct")).toBe("l5x");
    expect(inferEvidenceSourceKind("IO Schedule.xlsx", "direct")).toBe("xlsx");
    expect(inferEvidenceSourceKind("Any", "human_decision")).toBe("human_decision");
  });
});

