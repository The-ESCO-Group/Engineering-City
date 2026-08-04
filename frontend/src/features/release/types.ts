import type {
  CapabilityStatus,
  CurrentUserResponse,
  EventEnvelope,
  ExportArtifactRead,
  ProjectDetail,
  ReleaseCandidateRead,
  ReleaseReadinessRead,
  ReportRead
} from "../../api/types";
import type {
  ApprovalGateKey,
  ExportArtifactCreatedEventPayload,
  GateStatus,
  ImportVerificationStatus,
  ReleaseCandidateStatus,
  ValidationLevel
} from "../../generated";

export type ReleaseDataMode = "demo" | "live" | "unavailable";

export interface ReleaseDownloadLink {
  url: string;
  filename?: string;
  previewUrl?: string;
}

export interface ReleaseSource {
  dataMode: ReleaseDataMode;
  fallbackReason: string;
  project: ProjectDetail | null;
  reports: ReportRead[];
  events: EventEnvelope[];
  currentUser: CurrentUserResponse | null;
  capabilities: CapabilityStatus[];
  releaseReadiness?: ReleaseReadinessRead | null;
  releaseCandidates?: ReleaseCandidateRead[];
  releaseArtifacts?: ExportArtifactRead[];
  artifactDownloads?: Readonly<Record<string, ReleaseDownloadLink>>;
  releaseZipDownload?: ReleaseDownloadLink | null;
}

export type ReadinessState =
  | "passed"
  | "ready"
  | "pending"
  | "blocked"
  | "unavailable"
  | "unknown";

export interface ReleaseReadinessGate {
  id: string;
  label: string;
  group: ApprovalGateKey | "artifact_hashes" | "generator_status";
  state: ReadinessState;
  detail: string;
  unmetCriteria: string[];
  source: "event" | "derived" | "unavailable";
  eventStatus?: GateStatus;
}

export type ReleaseArtifactFolderId =
  | "00-manifest"
  | "01-controller"
  | "02-hmi"
  | "03-alarms"
  | "04-testing"
  | "05-reports"
  | "06-audit";

export interface ReleaseArtifact {
  id: string;
  candidateId: string;
  folderId: ReleaseArtifactFolderId;
  filename: string;
  kind: ExportArtifactCreatedEventPayload["kind"];
  relativePath: string;
  sizeBytes: number;
  sha256: string;
  generator: string;
  target: string;
  validationLevel: ValidationLevel;
  createdAt: string;
  sourceRevision: number | null;
  warningCount: number;
  importOrder: number;
  producedByPlugin: boolean;
  download: ReleaseDownloadLink | null;
  diagnostics: string[];
}

export interface ReleaseArtifactFolder {
  id: ReleaseArtifactFolderId;
  sequence: string;
  label: string;
  description: string;
  artifacts: ReleaseArtifact[];
}

export interface ReleaseCandidateSummary {
  id: string;
  number: number;
  name: string;
  revision: number;
  status: ReleaseCandidateStatus;
  validationLevel: ValidationLevel;
  artifactCount: number;
  expectedArtifactCount: number | null;
  limitationCount: number;
  unsupportedItemCount: number;
  generatorVersion: string;
  packageBytes: number;
  packageSha256: string;
  createdAt: string;
}

export interface ReleaseVerificationSummary {
  status: ImportVerificationStatus;
  detail: string;
  candidateOnly: boolean;
  validationLevel: ValidationLevel | null;
  occurredAt: string | null;
}

export interface ReleaseValidationSummary {
  id: string;
  revision: number;
  status: string;
  checksRun: number;
  errorCount: number;
  warningCount: number;
  humanReviewCount: number;
  unsupportedCount: number;
  isBlocking: boolean;
  disciplines: string[];
  summary: string;
  occurredAt: string;
}

export type ReportFormat = "html" | "pdf" | "docx" | "xlsx" | "csv" | "json" | string;

export interface ReportProvenanceItem {
  label: string;
  value: string;
  available: boolean;
}

export interface ReleaseReportFile {
  id: string;
  title: string;
  reportType: string;
  format: ReportFormat;
  status: string;
  mediaType: string;
  sizeBytes: number;
  sha256: string;
  provider: string;
  providerVersion: string;
  createdAt: string;
  error: string;
  download: ReleaseDownloadLink | null;
  provenance: ReportProvenanceItem[];
}

export interface ReleaseReportCatalogItem {
  id: string;
  title: string;
  description: string;
  files: ReleaseReportFile[];
}

export type ReleaseActionId =
  | "run_validation"
  | "create_release_candidate"
  | "generate_artifacts"
  | "request_import_verification"
  | "request_final_approval"
  | "finalize_release"
  | "download_release_zip";

export interface ReleaseActionAvailability {
  id: ReleaseActionId;
  label: string;
  enabled: boolean;
  reason: string;
}

export interface ReleaseModel {
  mode: ReleaseDataMode;
  demoLabel: string | null;
  unavailableReason: string;
  project: {
    id: string;
    reference: string;
    name: string;
    revision: number | null;
    status: string;
    targetProfile: string;
  } | null;
  candidate: ReleaseCandidateSummary | null;
  verification: ReleaseVerificationSummary;
  validation: ReleaseValidationSummary | null;
  gates: ReleaseReadinessGate[];
  folders: ReleaseArtifactFolder[];
  reports: ReleaseReportCatalogItem[];
  reportFormats: string[];
  actions: ReleaseActionAvailability[];
  releaseZipDownload: ReleaseDownloadLink | null;
  userRoleSummary: string;
  artifactCoverage: {
    actual: number;
    expected: number | null;
    hashed: number;
  };
  releaseOccurredAt: string | null;
}

export interface ReleaseActionContext {
  projectId: string;
  candidateId: string | null;
  revision: number | null;
}

export interface ReleaseActionResult {
  message?: string;
}

export interface ReleaseOperations {
  runValidation?: (context: ReleaseActionContext) => Promise<ReleaseActionResult | void>;
  createReleaseCandidate?: (context: ReleaseActionContext) => Promise<ReleaseActionResult | void>;
  generateArtifacts?: (context: ReleaseActionContext) => Promise<ReleaseActionResult | void>;
  requestImportVerification?: (context: ReleaseActionContext) => Promise<ReleaseActionResult | void>;
  requestFinalApproval?: (context: ReleaseActionContext) => Promise<ReleaseActionResult | void>;
  finalizeRelease?: (context: ReleaseActionContext) => Promise<ReleaseActionResult | void>;
  openCityLocation?: (semanticLocation: "release_logistics") => void;
}
