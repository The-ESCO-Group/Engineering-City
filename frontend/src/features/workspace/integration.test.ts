import { afterEach, describe, expect, it, vi } from "vitest";
import { engineeringApi } from "../../api/client";
import { createDemoWorkspaceModel } from "./demo";
import { createEmptyIntakeDraft } from "./intake";
import { workspaceOperationsFromEngineering, workspaceSourceFromEngineering } from "./integration";
import type { WorkspaceApproval, WorkspaceSource } from "./types";

function liveData() {
  return {
    dataMode: "live" as const,
    fallbackReason: "",
    project: null,
    files: [],
    tasks: [],
    findings: [],
    approvals: [],
    clarifications: null,
    requirements: [],
    events: [],
    refresh: vi.fn(async () => undefined),
    selectProject: vi.fn()
  };
}

afterEach(() => vi.restoreAllMocks());

describe("workspace live integration", () => {
  it("adapts the engineering provider without mixing demo data", () => {
    const data = liveData();
    const source: WorkspaceSource = workspaceSourceFromEngineering(data);
    expect(source.dataMode).toBe("live");
    expect(source.findings).toEqual([]);
  });

  it("keeps intake available for an authenticated live account before its first project exists", () => {
    const data = {
      ...liveData(),
      dataMode: "unavailable" as const,
      connection: "live" as const,
      currentUser: {
        user: {
          id: "user-1",
          email: "admin@esco.internal",
          display_name: "ESCO Administrator",
          is_active: true,
          roles: ["administrator" as const]
        },
        project_roles: {}
      }
    };

    expect(workspaceSourceFromEngineering(data).dataMode).toBe("live");
    expect(workspaceOperationsFromEngineering(data).submitIntake).toBeTypeOf("function");
  });

  it("enables validation waivers only for a globally authorized project reviewer", () => {
    const data = {
      ...liveData(),
      project: {
        id: "project-review",
        name: "Review project",
        description: "Synthetic",
        client_reference: "SYN-REVIEW",
        status: "awaiting_review",
        is_paused: false,
        event_seq: 1,
        created_at: "2026-08-05T10:00:00Z",
        updated_at: "2026-08-05T10:00:00Z",
        attributes: {},
        counts: { files: 0, files_parsed: 0, tasks: 0, tasks_completed: 0, findings: 0, findings_open: 0, approvals_pending: 0, reports: 0 }
      },
      currentUser: {
        user: {
          id: "reviewer-1",
          email: "reviewer@esco.internal",
          display_name: "Review Engineer",
          is_active: true,
          roles: ["reviewer" as const]
        },
        project_roles: { "project-review": "reviewer" }
      }
    };

    expect(workspaceSourceFromEngineering(data).canReviewValidations).toBe(true);
    expect(workspaceSourceFromEngineering({
      ...data,
      currentUser: {
        ...data.currentUser,
        user: { ...data.currentUser.user, roles: ["engineer" as const] }
      }
    }).canReviewValidations).toBe(false);
  });

  it("creates project identity and uploads files by declared document role", async () => {
    const data = liveData();
    const project = {
      id: "project-created",
      name: "Synthetic project",
      description: "Synthetic description",
      client_reference: "Internal synthetic",
      status: "intake",
      is_paused: false,
      event_seq: 1,
      created_at: "2026-07-31T10:00:00Z",
      updated_at: "2026-07-31T10:00:00Z",
      attributes: {},
      counts: { files: 0, files_parsed: 0, tasks: 0, tasks_completed: 0, findings: 0, findings_open: 0, approvals_pending: 0, reports: 0 }
    };
    const storedFile = {
      id: "file-parent",
      project_id: project.id,
      original_filename: "source.zip",
      file_kind: "zip",
      document_role: "control_logic",
      media_type: "application/zip",
      extension: ".zip",
      size_bytes: 20,
      sha256: "synthetic-hash",
      status: "uploaded",
      status_detail: "",
      parent_file_id: null,
      archive_member_path: "",
      created_at: "2026-07-31T10:01:00Z",
      updated_at: "2026-07-31T10:01:00Z"
    };
    const extractedFile = { ...storedFile, id: "file-extracted", original_filename: "Main.L5X", file_kind: "l5x", extension: ".l5x", parent_file_id: storedFile.id, archive_member_path: "Main.L5X" };
    const create = vi.spyOn(engineeringApi, "createProject").mockResolvedValue(project);
    const upload = vi.spyOn(engineeringApi, "uploadFiles").mockResolvedValue({ project_id: project.id, accepted_count: 1, rejected_count: 0, results: [{ accepted: true, original_filename: "source.zip", file: storedFile, extracted: [extractedFile], rejection_code: "", rejection_reason: "" }] });
    const startRun = vi.spyOn(engineeringApi, "startRun").mockResolvedValue({ run_id: "run-1", project_id: project.id, status: "running", started_at: "2026-07-31T10:02:00Z", task_count: 0, tasks: [] });
    const draft = createEmptyIntakeDraft();
    draft.identity = { name: "Synthetic project", customerLabel: "Internal synthetic", plantSite: "Synthetic site", description: "Synthetic description", projectType: "upgrade", units: "metric", locale: "en-US" };
    const operations = workspaceOperationsFromEngineering(data);
    draft.target = { ...draft.target, controllerFamily: "compactlogix", catalogNumber: "Synthetic catalog", firmware: "35", studio5000Version: "35", releaseMode: "import_package_only", restrictedDomains: ["safety_logic"] };
    draft.standards = { ...draft.standards, namingStandard: "ESCO synthetic", alarmPhilosophy: "Synthetic alarms", controllerTemplate: "Synthetic template", testTemplateSet: "Synthetic tests" };
    const result = await operations.submitIntake?.({ draft, files: [{ file: new File(["synthetic"], "source.zip"), documentRole: "control_logic" }] });
    expect(create).toHaveBeenCalledWith(expect.objectContaining({
      name: "Synthetic project",
      description: "Synthetic description",
      client_reference: "Internal synthetic",
      attributes: { intake_ui: { version: 2, safety_acknowledged: false, analysis_confirmed: false } },
      engineering_profile: expect.objectContaining({
        project_type: "upgrade",
        plant_or_site: "Synthetic site",
        release_mode: "import_package_only",
        restricted_domains: ["safety_logic"],
        target: expect.objectContaining({ controller_family: "compactlogix", controller_catalog_number: "Synthetic catalog" }),
        standards: expect.objectContaining({ naming_standard_version: "ESCO synthetic", test_template_set: "Synthetic tests" })
      })
    }));
    expect(upload).toHaveBeenCalledWith(project.id, [expect.any(File)], "control_logic");
    expect(startRun).toHaveBeenCalledWith(project.id, ["file-parent", "file-extracted"]);
    expect(data.selectProject).toHaveBeenCalledWith(project.id);
    expect(data.refresh).toHaveBeenCalled();
    expect(result).toEqual({ projectId: project.id });
  });

  it("does not start analysis when any upload is rejected", async () => {
    const data = liveData();
    const project = {
      id: "project-partial",
      name: "Synthetic project",
      description: "Synthetic description",
      client_reference: "Internal synthetic",
      status: "intake",
      is_paused: false,
      event_seq: 1,
      created_at: "2026-07-31T10:00:00Z",
      updated_at: "2026-07-31T10:00:00Z",
      attributes: {},
      counts: { files: 0, files_parsed: 0, tasks: 0, tasks_completed: 0, findings: 0, findings_open: 0, approvals_pending: 0, reports: 0 }
    };
    vi.spyOn(engineeringApi, "createProject").mockResolvedValue(project);
    vi.spyOn(engineeringApi, "uploadFiles").mockResolvedValue({ project_id: project.id, accepted_count: 0, rejected_count: 1, results: [{ accepted: false, original_filename: "unsafe.exe", file: null, extracted: [], rejection_code: "unsupported", rejection_reason: "Executable files are not accepted." }] });
    const startRun = vi.spyOn(engineeringApi, "startRun");
    const draft = createEmptyIntakeDraft();
    draft.identity = { name: "Synthetic project", customerLabel: "Internal synthetic", plantSite: "Synthetic site", description: "Synthetic description", projectType: "upgrade", units: "metric", locale: "en-US" };
    const operations = workspaceOperationsFromEngineering(data);
    await expect(operations.submitIntake?.({ draft, files: [{ file: new File(["synthetic"], "unsafe.exe"), documentRole: "other" }] })).rejects.toThrow(/was created, but 1 file was rejected/);
    expect(startRun).not.toHaveBeenCalled();
    expect(data.selectProject).toHaveBeenCalledWith(project.id);
    expect(data.refresh).toHaveBeenCalled();
  });

  it("blocks richer decisions that the current boolean endpoint cannot represent", async () => {
    const data = liveData();
    const decide = vi.spyOn(engineeringApi, "decideApproval");
    const operations = workspaceOperationsFromEngineering(data);
    await expect(operations.decideApproval?.(createDemoWorkspaceModel().approvals[0], "defer", "Synthetic comment")).rejects.toThrow(/not supported/);
    expect(decide).not.toHaveBeenCalled();
  });

  it("routes every gate verdict through the exact gate and approval binding", async () => {
    const data = liveData();
    const gateDecision = vi.spyOn(engineeringApi, "decideApprovalGate").mockResolvedValue({} as never);
    const legacyDecision = vi.spyOn(engineeringApi, "decideApproval");
    const approval: WorkspaceApproval = {
      ...createDemoWorkspaceModel().approvals[0],
      kind: "gate_decision",
      gateId: "gate-1",
      gateKey: "qa_and_risk",
      allowedDecisions: ["approve", "reject", "request_changes", "accept_risk", "defer"]
    };
    const operations = workspaceOperationsFromEngineering(data);

    await operations.decideApproval?.(approval, "accept_risk", "Named risk rationale.");

    expect(gateDecision).toHaveBeenCalledWith(
      "gate-1",
      approval.id,
      "accept_risk",
      "Named risk rationale."
    );
    expect(legacyDecision).not.toHaveBeenCalled();
    expect(data.refresh).toHaveBeenCalled();
  });

  it("refuses a gate decision whose exact binding is absent", async () => {
    const data = liveData();
    const gateDecision = vi.spyOn(engineeringApi, "decideApprovalGate");
    const approval = {
      ...createDemoWorkspaceModel().approvals[0],
      kind: "gate_decision",
      gateId: null,
      gateKey: "qa_and_risk"
    };

    await expect(
      workspaceOperationsFromEngineering(data).decideApproval?.(
        approval,
        "approve",
        "Checked."
      )
    ).rejects.toThrow(/missing its exact gate binding/);
    expect(gateDecision).not.toHaveBeenCalled();
  });
});
