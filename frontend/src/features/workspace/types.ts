import type {
  ApprovalRead,
  EventEnvelope,
  FindingDetail,
  FindingRead,
  ProjectDetail,
  TaskRead,
  UploadedFileRead
} from "../../api/types";

export type WorkspaceMode = "demo" | "live" | "unavailable";

export type WorkspaceView =
  | "intake"
  | "files"
  | "tasks"
  | "requirements"
  | "devices"
  | "tags"
  | "plc"
  | "planning"
  | "changesets"
  | "revisions"
  | "findings"
  | "approvals";

export type CapabilityLevel = "available" | "summary_only" | "unavailable";

export interface WorkspaceCapability {
  level: CapabilityLevel;
  reason: string;
}

export interface WorkspaceCapabilities {
  intakeSubmission: WorkspaceCapability;
  projectCore: WorkspaceCapability;
  changesetOperations: WorkspaceCapability;
  revisionComparison: WorkspaceCapability;
  findingEvidence: WorkspaceCapability;
  approvalDecisions: WorkspaceCapability;
}

export interface WorkspaceProject {
  id: string;
  reference: string;
  name: string;
  description: string;
  status: string;
  revision: number | null;
  updatedAt: string;
}

export interface WorkspaceFile {
  id: string;
  name: string;
  kind: string;
  role: string;
  sizeBytes: number;
  sha256: string;
  status: string;
  detail: string;
  archiveMemberPath: string;
  updatedAt: string;
}

export type WorkspaceTaskState =
  | "queued"
  | "running"
  | "blocked"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled"
  | "unknown";

export interface WorkspaceTask {
  id: string;
  title: string;
  description: string;
  kind: string;
  state: WorkspaceTaskState;
  priority: number;
  agent: string | null;
  agentId: string | null;
  progress: number | null;
  blockedReason: string;
  error: string;
  attempts: number;
  maxAttempts: number;
  startedAt: string | null;
  finishedAt: string | null;
  updatedAt: string;
  semanticLocation: string | null;
}

export interface WorkspaceRequirement {
  id: string;
  reference: string;
  title: string;
  text: string;
  source: string;
  kind: string;
  priority: string;
  status: string;
  acceptanceCriteria: string[];
  acceptanceCriteriaCount: number;
  implementationCoverage: number | null;
  testCoverage: number | null;
  openQuestions: string[];
  inferred: boolean;
  confidence: number | null;
}

export interface WorkspaceDevice {
  id: string;
  area: string;
  unit: string;
  deviceId: string;
  type: string;
  vendorModel: string;
  ioAssignments: string[];
  networkNode: string;
  failState: string;
  linkedTags: string[];
  linkedLogic: string[];
  coverage: {
    hmi: string;
    alarms: string;
    tests: string;
  };
  validation: string;
}

export interface WorkspaceTag {
  id: string;
  name: string;
  scope: string;
  dataType: string;
  role: string;
  description: string;
  units: string;
  alias: string;
  device: string;
  status: string;
  validation: string;
  references: string[];
  approvedRevision: number | null;
}

export interface WorkspaceLogicInstruction {
  mnemonic: string;
  operands: string[];
  description: string;
}

export interface WorkspaceLogicRung {
  id: string;
  number: number;
  comment: string;
  instructions: WorkspaceLogicInstruction[];
  requirementLinks: string[];
  evidenceLinks: string[];
  warnings: string[];
}

export interface WorkspaceRoutine {
  id: string;
  name: string;
  type: string;
  templateOrigin: string;
  rungs: WorkspaceLogicRung[];
}

export interface WorkspaceProgram {
  id: string;
  controller: string;
  task: string;
  name: string;
  routines: WorkspaceRoutine[];
  validation: string;
  generatedPreviewAvailable: boolean;
}

export type EvidenceSourceKind =
  | "l5x"
  | "xlsx"
  | "csv"
  | "docx"
  | "pdf"
  | "xml"
  | "requirement"
  | "template_rule"
  | "human_decision"
  | "generated_object"
  | "validation_output"
  | "unknown";

export interface WorkspaceEvidence {
  id: string;
  kind: EvidenceSourceKind;
  evidenceKind: string;
  role: string;
  sourceLabel: string;
  exactLocation: string;
  excerpt: string;
  expectation: string;
  note: string;
}

export interface WorkspaceFinding {
  id: string;
  title: string;
  category: string;
  severity: string;
  status: string;
  source: string;
  exactLocation: string;
  agent: string;
  confidence: number | null;
  evidenceCount: number;
  discipline: string;
  reviewer: string;
  approvalStatus: string;
  requiresApproval: boolean;
  fact: string;
  inference: string;
  expectedMissingEvidence: string;
  parserWarnings: string[];
  requirementSide: string;
  implementationSide: string;
  impact: string;
  suggestedAction: string;
  risk: string;
  limitations: string;
  comments: string[];
  history: string[];
  linkedChangesetId: string | null;
  linkedTests: string[];
  evidence: WorkspaceEvidence[];
  detailLoaded: boolean;
}

export type ChangeOperationType =
  | "create"
  | "update"
  | "rename"
  | "delete"
  | "link"
  | "unlink"
  | "assign_io"
  | "logic"
  | "other";

export interface WorkspaceChangeOperation {
  id: string;
  type: ChangeOperationType;
  objectType: string;
  objectLabel: string;
  field: string;
  before: string;
  after: string;
  sourceEvidence: WorkspaceEvidence[];
  templateBasis: string;
  rationale: string;
  confidence: number | null;
  risk: string;
  impactedObjects: Array<{ type: string; label: string; reference: string }>;
  validationResults: string[];
  reviewerComments: string[];
  requiresApproval: boolean;
}

export interface WorkspaceChangeSet {
  id: string;
  title: string;
  status: string;
  baseRevision: number;
  resultingRevision: number | null;
  author: string;
  semanticLocation: string;
  operationCount: number;
  warningCount: number;
  assumptionCount: number;
  confidence: number | null;
  restrictedDomains: string[];
  requiresHumanApproval: boolean;
  operations: WorkspaceChangeOperation[];
  operationsAvailable: boolean;
  concerns: string[];
  validationSummary: string;
  createdAt: string;
}

export interface WorkspacePlan {
  id: string;
  runId: string | null;
  summary: string;
  taskCount: number;
  taskCountsByKind: Record<string, number>;
  riskCount: number;
  outOfScopeCount: number;
  blockers: string[];
  location: string;
  occurredAt: string;
}

export interface WorkspaceRevision {
  id: string;
  number: number;
  parentNumber: number | null;
  summary: string;
  operationCount: number;
  objectCounts: Record<string, number>;
  changesetId: string | null;
  createdBy: string;
  isReleaseBaseline: boolean;
  occurredAt: string;
}

export type WorkspaceApprovalDecision =
  | "approve"
  | "reject"
  | "request_changes"
  | "accept_risk"
  | "defer";

export interface WorkspaceApproval {
  id: string;
  projectId: string;
  findingId: string | null;
  changesetId: string | null;
  gate: string;
  kind: string;
  status: string;
  title: string;
  requestedDecision: string;
  affectedRevision: number | null;
  affectedObjects: string[];
  evidence: WorkspaceEvidence[];
  validationResults: string[];
  rationale: string;
  risk: string;
  severity: string;
  reviewer: string;
  dueDate: string | null;
  comments: string[];
  priorDecisions: string[];
  downstreamImpact: string;
  restrictedDomain: boolean;
  finalRelease: boolean;
  requestedBy: string;
  requestedAt: string | null;
  allowedDecisions: WorkspaceApprovalDecision[];
}

export interface WorkspaceModel {
  mode: WorkspaceMode;
  demoLabel: string | null;
  unavailableReason: string;
  project: WorkspaceProject | null;
  capabilities: WorkspaceCapabilities;
  files: WorkspaceFile[];
  tasks: WorkspaceTask[];
  requirements: WorkspaceRequirement[];
  devices: WorkspaceDevice[];
  tags: WorkspaceTag[];
  plcPrograms: WorkspaceProgram[];
  plans: WorkspacePlan[];
  changesets: WorkspaceChangeSet[];
  revisions: WorkspaceRevision[];
  findings: WorkspaceFinding[];
  approvals: WorkspaceApproval[];
}

export interface WorkspaceSource {
  dataMode: WorkspaceMode;
  fallbackReason: string;
  project: ProjectDetail | null;
  files: UploadedFileRead[];
  tasks: TaskRead[];
  findings: FindingRead[];
  approvals: ApprovalRead[];
  events: EventEnvelope[];
}

export interface IntakeIdentity {
  name: string;
  customerLabel: string;
  plantSite: string;
  description: string;
  projectType: string;
}

export interface IntakeTargetProfile {
  controllerFamily: string;
  catalogNumber: string;
  firmware: string;
  studio5000Version: string;
  hmiProduct: string;
  hmiVersion: string;
  terminalModel: string;
  resolution: string;
  releaseMode: string;
  restrictedDomains: string[];
}

export interface IntakeStandards {
  namingStandard: string;
  alarmPhilosophy: string;
  controllerTemplate: string;
  aoiUdtLibrary: string;
  hmiLibrary: string;
  testTemplateSet: string;
}

export interface StagedIntakeFile {
  id: string;
  name: string;
  sizeBytes: number;
  extension: string;
  documentRole: string;
  validation: "ready" | "unsupported" | "needs_reselection";
  validationMessage: string;
}

export interface IntakeDraft {
  version: 1;
  updatedAt: string;
  lastCompletedStep: number;
  identity: IntakeIdentity;
  target: IntakeTargetProfile;
  standards: IntakeStandards;
  files: StagedIntakeFile[];
  safetyAcknowledged: boolean;
  analysisConfirmed: boolean;
}

export interface IntakeValidationIssue {
  step: number;
  field: string;
  message: string;
  severity: "error" | "warning";
}

export interface IntakeSubmission {
  draft: IntakeDraft;
  files: Array<{ file: File; documentRole: string }>;
}

export interface WorkspaceOperations {
  allowPartialChangeSetReview?: boolean;
  submitIntake?: (submission: IntakeSubmission) => Promise<{ projectId: string }>;
  loadFindingDetail?: (findingId: string) => Promise<FindingDetail>;
  decideApproval?: (
    approval: WorkspaceApproval,
    decision: WorkspaceApprovalDecision,
    comment: string
  ) => Promise<void>;
  reviewChangeSet?: (
    changeset: WorkspaceChangeSet,
    decision: "approve" | "reject" | "request_changes",
    comment: string,
    selectedOperationIds?: string[]
  ) => Promise<void>;
  compareRevisions?: (baseRevision: number, targetRevision: number) => Promise<void>;
  exportCoreView?: (view: "requirements" | "devices" | "tags" | "plc") => Promise<void>;
  openCityLocation?: (semanticLocation: string) => void;
}
