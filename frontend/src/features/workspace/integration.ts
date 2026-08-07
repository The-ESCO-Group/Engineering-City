import { engineeringApi } from "../../api/client";
import type { EngineeringData } from "../../api/live";
import { buildProjectIdentityInput } from "./intake";
import type {
  WorkspaceOperations,
  WorkspaceSource
} from "./types";

type WorkspaceEngineeringData = Pick<
  EngineeringData,
  | "dataMode"
  | "fallbackReason"
  | "project"
  | "files"
  | "tasks"
  | "findings"
  | "approvals"
  | "clarifications"
  | "requirements"
  | "events"
  | "refresh"
  | "selectProject"
> & Partial<Pick<EngineeringData, "connection" | "currentUser">>;

export function workspaceSourceFromEngineering(data: WorkspaceEngineeringData): WorkspaceSource {
  const authenticatedEmptyProject = data.dataMode === "unavailable" &&
    data.connection === "live" &&
    Boolean(data.currentUser);
  const projectId = data.project?.id;
  const globalRoles = new Set(data.currentUser?.user.roles ?? []);
  const projectRole = projectId ? data.currentUser?.project_roles[projectId] : undefined;
  const canReviewValidations = globalRoles.has("administrator") || (
    globalRoles.has("reviewer") && (projectRole === "owner" || projectRole === "reviewer")
  );
  return {
    dataMode: authenticatedEmptyProject ? "live" : data.dataMode,
    fallbackReason: data.fallbackReason,
    project: data.project,
    files: data.files,
    tasks: data.tasks,
    findings: data.findings,
    approvals: data.approvals,
    clarifications: data.clarifications,
    requirements: data.requirements,
    events: data.events,
    canReviewValidations
  };
}

export interface WorkspaceIntegrationOptions {
  openCityLocation?: (semanticLocation: string) => void;
  openReleaseCenter?: () => void;
}

export function workspaceOperationsFromEngineering(
  data: WorkspaceEngineeringData,
  options: WorkspaceIntegrationOptions = {}
): WorkspaceOperations {
  const operations: WorkspaceOperations = {
    openCityLocation: options.openCityLocation,
    openReleaseCenter: options.openReleaseCenter
  };
  const liveSession = data.dataMode === "live" || (
    data.dataMode === "unavailable" && data.connection === "live" && Boolean(data.currentUser)
  );
  if (!liveSession) return operations;

  operations.loadFindingDetail = engineeringApi.finding;
  operations.submitIntake = async ({ draft, files }) => {
    const project = await engineeringApi.createProject({
      ...buildProjectIdentityInput(draft),
      attributes: {
        intake_ui: {
          version: 2,
          safety_acknowledged: draft.safetyAcknowledged,
          analysis_confirmed: draft.analysisConfirmed
        }
      },
      engineering_profile: {
        project_name: draft.identity.name.trim(),
        customer_label: draft.identity.customerLabel.trim(),
        plant_or_site: draft.identity.plantSite.trim(),
        project_description: draft.identity.description.trim(),
        project_type: draft.identity.projectType,
        units: draft.identity.units,
        locale: draft.identity.locale.trim(),
        network_architecture: "",
        release_mode: draft.target.releaseMode,
        restricted_domains: [...draft.target.restrictedDomains],
        target: {
          controller_family: draft.target.controllerFamily,
          controller_catalog_number: draft.target.catalogNumber.trim(),
          firmware_revision: draft.target.firmware.trim(),
          studio_5000_version: draft.target.studio5000Version.trim(),
          safety_classification: draft.target.safetyClassification,
          hmi_product: draft.target.hmiProduct,
          hmi_version: draft.target.hmiVersion.trim(),
          hmi_terminal_model: draft.target.terminalModel.trim(),
          hmi_resolution: draft.target.resolution.trim()
        },
        standards: {
          naming_standard_version: draft.standards.namingStandard.trim(),
          alarm_philosophy_version: draft.standards.alarmPhilosophy.trim(),
          controller_template: draft.standards.controllerTemplate.trim(),
          aoi_udt_library: draft.standards.aoiUdtLibrary.trim(),
          hmi_library: draft.standards.hmiLibrary.trim(),
          test_template_set: draft.standards.testTemplateSet.trim()
        }
      }
    });
    const filesByRole = new Map<string, File[]>();
    files.forEach(({ file, documentRole }) => {
      filesByRole.set(documentRole, [...(filesByRole.get(documentRole) ?? []), file]);
    });
    const uploadResults = await Promise.all(
      [...filesByRole].map(([documentRole, groupedFiles]) =>
        engineeringApi.uploadFiles(project.id, groupedFiles, documentRole)
      )
    );
    const rejected = uploadResults.flatMap((result) =>
      result.results.filter((item) => !item.accepted)
    );
    const fileIds = [...new Set(uploadResults.flatMap((result) =>
      result.results.flatMap((item) => item.accepted
        ? [item.file?.id, ...item.extracted.map((file) => file.id)].filter((id): id is string => Boolean(id))
        : [])
    ))];
    data.selectProject(project.id);
    if (rejected.length > 0) {
      await data.refresh();
      const reasons = rejected.map((item) => `${item.original_filename}: ${item.rejection_reason}`).join("; ");
      throw new Error(
        `Project ${project.id} was created, but ${rejected.length} file${rejected.length === 1 ? " was" : "s were"} rejected: ${reasons}`
      );
    }
    try {
      await engineeringApi.startRun(project.id, fileIds);
    } finally {
      await data.refresh();
    }
    return { projectId: project.id };
  };
  operations.decideApproval = async (approval, decision, comment) => {
    if (approval.kind === "gate_decision") {
      if (!approval.gateId || !approval.gateKey) {
        throw new Error("This workflow gate approval is missing its exact gate binding.");
      }
      await engineeringApi.decideApprovalGate(approval.gateId, approval.id, decision, comment);
      await data.refresh();
      return;
    }
    if (decision !== "approve" && decision !== "reject") {
      throw new Error(`${decision.replaceAll("_", " ")} is not supported by the current local approval endpoint.`);
    }
    await engineeringApi.decideApproval(approval.id, decision === "approve", comment);
    await data.refresh();
  };
  operations.resolveClarification = async (kind, id, answer) => {
    if (kind === "project_question") await engineeringApi.resolveProjectQuestion(id, answer);
    else await engineeringApi.resolveClarification(id, answer);
    await data.refresh();
  };
  operations.decideRequirement = async (requirementId, decision, rationale) => {
    await engineeringApi.decideRequirement(requirementId, decision, rationale);
    await data.refresh();
  };
  return operations;
}
