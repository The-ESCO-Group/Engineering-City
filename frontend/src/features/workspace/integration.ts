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
  | "events"
  | "refresh"
  | "selectProject"
>;

export function workspaceSourceFromEngineering(data: WorkspaceEngineeringData): WorkspaceSource {
  return {
    dataMode: data.dataMode,
    fallbackReason: data.fallbackReason,
    project: data.project,
    files: data.files,
    tasks: data.tasks,
    findings: data.findings,
    approvals: data.approvals,
    events: data.events
  };
}

export interface WorkspaceIntegrationOptions {
  openCityLocation?: (semanticLocation: string) => void;
}

export function workspaceOperationsFromEngineering(
  data: WorkspaceEngineeringData,
  options: WorkspaceIntegrationOptions = {}
): WorkspaceOperations {
  const operations: WorkspaceOperations = {
    openCityLocation: options.openCityLocation
  };
  if (data.dataMode !== "live") return operations;

  operations.loadFindingDetail = engineeringApi.finding;
  operations.submitIntake = async ({ draft, files }) => {
    const project = await engineeringApi.createProject({
      ...buildProjectIdentityInput(draft),
      attributes: {
        esco_product_intake: {
          version: 1,
          plant_site: draft.identity.plantSite.trim(),
          project_type: draft.identity.projectType.trim(),
          target_profile: {
            controller_family: draft.target.controllerFamily.trim(),
            catalog_number: draft.target.catalogNumber.trim(),
            firmware: draft.target.firmware.trim(),
            studio_5000_version: draft.target.studio5000Version.trim(),
            hmi_product: draft.target.hmiProduct.trim(),
            hmi_version: draft.target.hmiVersion.trim(),
            terminal_model: draft.target.terminalModel.trim(),
            resolution: draft.target.resolution.trim(),
            release_mode: draft.target.releaseMode,
            restricted_domains: [...draft.target.restrictedDomains]
          },
          standards: {
            naming_standard: draft.standards.namingStandard.trim(),
            alarm_philosophy: draft.standards.alarmPhilosophy.trim(),
            controller_template: draft.standards.controllerTemplate.trim(),
            aoi_udt_library: draft.standards.aoiUdtLibrary.trim(),
            hmi_library: draft.standards.hmiLibrary.trim(),
            test_template_set: draft.standards.testTemplateSet.trim()
          }
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
    if (decision !== "approve" && decision !== "reject") {
      throw new Error(`${decision.replaceAll("_", " ")} is not supported by the current local approval endpoint.`);
    }
    await engineeringApi.decideApproval(approval.id, decision === "approve", comment);
    await data.refresh();
  };
  return operations;
}
