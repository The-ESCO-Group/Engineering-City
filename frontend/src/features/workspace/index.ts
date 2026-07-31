export { EngineeringWorkspace, type EngineeringWorkspaceProps } from "./EngineeringWorkspace";
export { ProjectIntake, type ProjectIntakeProps } from "./ProjectIntake";
export { ProjectCoreExplorer, type ProjectCoreExplorerProps, type ProjectCoreView } from "./ProjectCoreExplorer";
export { FilesWorkspace, type FilesWorkspaceProps } from "./FilesWorkspace";
export { TaskWorkspace, type TaskWorkspaceProps } from "./TaskWorkspace";
export { FindingsWorkspace, type FindingsWorkspaceProps } from "./FindingsWorkspace";
export { EvidenceViewer, type EvidenceViewerProps } from "./EvidenceViewer";
export { ApprovalInbox, type ApprovalInboxProps } from "./ApprovalInbox";
export { approvalDecisionRequiresComment } from "./approvalPolicy";
export { ChangeReviewWorkspace, type ChangeReviewWorkspaceProps, type ChangeReviewView } from "./ChangeReviewWorkspace";
export { buildWorkspaceModel, mergeFindingDetail, inferEvidenceSourceKind, normalizeTaskState } from "./adapters";
export { workspaceSourceFromEngineering, workspaceOperationsFromEngineering, type WorkspaceIntegrationOptions } from "./integration";
export { createDemoWorkspaceModel } from "./demo";
export {
  INTAKE_DRAFT_STORAGE_KEY,
  createEmptyIntakeDraft,
  restoreIntakeDraft,
  loadIntakeDraft,
  saveIntakeDraft,
  clearIntakeDraft,
  validateIntakeDraft,
  stageIntakeFile,
  buildProjectIdentityInput
} from "./intake";
export type * from "./types";
