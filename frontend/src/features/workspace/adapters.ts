import type {
  ApprovalRead,
  EventEnvelope,
  FindingDetail,
  FindingRead,
  RequirementRead,
  TaskRead
} from "../../api/types";
import { createDemoWorkspaceModel } from "./demo";
import type {
  EvidenceSourceKind,
  WorkspaceApproval,
  WorkspaceChangeSet,
  WorkspaceEvidence,
  WorkspaceFinding,
  WorkspaceModel,
  WorkspacePlan,
  WorkspaceRequirement,
  WorkspaceRevision,
  WorkspaceSource,
  WorkspaceTask,
  WorkspaceTaskState
} from "./types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function booleanValue(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function numberRecord(value: unknown): Record<string, number> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).filter((entry): entry is [string, number] =>
      typeof entry[1] === "number" && Number.isFinite(entry[1])
    )
  );
}

function displayText(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function normalizeTaskState(status: string): WorkspaceTaskState {
  const normalized = status.trim().toLowerCase().replaceAll(" ", "_");
  if (normalized === "pending" || normalized === "ready" || normalized === "created") return "queued";
  if (normalized === "in_progress" || normalized === "started" || normalized === "working") return "running";
  if (normalized === "awaiting_approval" || normalized === "approval") return "awaiting_approval";
  if (
    normalized === "queued" ||
    normalized === "running" ||
    normalized === "blocked" ||
    normalized === "completed" ||
    normalized === "failed" ||
    normalized === "cancelled"
  ) return normalized;
  return "unknown";
}

function taskLocations(events: EventEnvelope[]): Map<string, string> {
  const locations = new Map<string, string>();
  for (const event of events) {
    if (event.event_type !== "task.assigned" || !event.task_id) continue;
    const location = stringValue(event.payload.location);
    if (location) locations.set(event.task_id, location);
  }
  return locations;
}

function taskToWorkspace(task: TaskRead, location: string | null): WorkspaceTask {
  return {
    id: task.id,
    title: task.title,
    description: task.description,
    kind: task.kind,
    state: normalizeTaskState(task.status),
    priority: task.priority,
    agent: task.agent_kind,
    agentId: task.agent_instance_id,
    progress: task.status === "completed" ? 100 : null,
    blockedReason: task.blocked_reason,
    error: [task.error_code, task.error_message].filter(Boolean).join(": "),
    attempts: task.attempts,
    maxAttempts: task.max_attempts,
    startedAt: task.started_at,
    finishedAt: task.finished_at,
    updatedAt: task.updated_at,
    semanticLocation: location
  };
}

function requirementFromEvent(event: EventEnvelope): WorkspaceRequirement | null {
  if (event.event_type !== "requirement.created") return null;
  const payload = event.payload;
  const id = stringValue(payload.requirement_id);
  const reference = stringValue(payload.reference);
  const title = stringValue(payload.title);
  if (!id || !reference || !title) return null;
  const criteriaCount = numberValue(payload.acceptance_criteria_count);
  return {
    id,
    reference,
    title,
    text: "Requirement text is available from the pending Project Core resource contract.",
    source: stringValue(payload.source_label, "Source not published in event"),
    kind: stringValue(payload.kind, "other"),
    priority: stringValue(payload.priority, "expected"),
    status: stringValue(payload.status, "proposed"),
    acceptanceCriteria: [],
    acceptanceCriteriaCount: criteriaCount,
    implementationCoverage: null,
    testCoverage: null,
    openQuestions: [],
    inferred: booleanValue(payload.is_inferred),
    confidence: nullableNumber(payload.confidence)
  };
}

function requirementFromResource(item: RequirementRead): WorkspaceRequirement {
  const primary = item.sources.find((source) => source.is_primary) ?? item.sources[0];
  return {
    id: item.id,
    reference: item.reference,
    title: item.title,
    text: item.statement,
    source: primary?.location_label || "Source citation not recorded",
    kind: item.kind,
    priority: item.priority,
    status: item.status,
    acceptanceCriteria: item.acceptance_criteria
      .sort((left, right) => left.ordinal - right.ordinal)
      .map((criterion) => `${criterion.reference} · ${criterion.statement}`),
    acceptanceCriteriaCount: item.acceptance_criteria.length,
    implementationCoverage: null,
    testCoverage: null,
    openQuestions: [],
    inferred: item.is_inferred,
    confidence: item.confidence
  };
}

function plansFromEvents(events: EventEnvelope[]): WorkspacePlan[] {
  return events.flatMap((event) => {
    if (event.event_type !== "workflow.planned") return [];
    const payload = event.payload;
    return [{
      id: event.id,
      runId: nullableString(payload.run_id),
      summary: stringValue(payload.summary, "Workflow plan summary was not included."),
      taskCount: numberValue(payload.task_count),
      taskCountsByKind: numberRecord(payload.task_counts_by_kind),
      riskCount: numberValue(payload.risk_count),
      outOfScopeCount: numberValue(payload.out_of_scope_count),
      blockers: stringArray(payload.blocked_by_question_references),
      location: stringValue(payload.location, "project_core"),
      occurredAt: event.occurred_at
    }];
  });
}

function eventChangeSetId(event: EventEnvelope): string {
  return stringValue(event.payload.changeset_id);
}

function changesetsFromEvents(events: EventEnvelope[]): WorkspaceChangeSet[] {
  const changesets = new Map<string, WorkspaceChangeSet>();
  for (const event of events) {
    if (!event.event_type.startsWith("changeset.")) continue;
    const payload = event.payload;
    const id = eventChangeSetId(event);
    if (!id) continue;
    let record = changesets.get(id);
    if (!record) {
      record = {
        id,
        title: stringValue(payload.title, `Change set ${id.slice(0, 8)}`),
        status: stringValue(payload.status, event.event_type.replace("changeset.", "")),
        baseRevision: numberValue(payload.base_revision_number),
        resultingRevision: nullableNumber(payload.resulting_revision_number),
        author: stringValue(payload.author_agent_kind, stringValue(payload.author_user_id, "Not published")),
        semanticLocation: stringValue(payload.location, "project_core"),
        operationCount: numberValue(payload.operation_count, numberValue(payload.operations_applied)),
        warningCount: numberValue(payload.warning_count),
        assumptionCount: numberValue(payload.assumption_count),
        confidence: nullableNumber(payload.confidence),
        restrictedDomains: stringArray(payload.restricted_domains),
        requiresHumanApproval: booleanValue(payload.requires_human_approval),
        operations: [],
        operationsAvailable: false,
        concerns: [],
        validationSummary: "Operation-level diff is not included in the current event contract.",
        createdAt: event.occurred_at
      };
      changesets.set(id, record);
    }
    if (event.event_type === "changeset.validation_failed") {
      record.status = "validation_failed";
      record.validationSummary = stringValue(payload.reason, "Deterministic validation failed.");
      record.warningCount = numberValue(payload.warning_count, record.warningCount);
    } else if (event.event_type === "changeset.reviewed") {
      record.status = stringValue(payload.status, record.status);
      record.concerns = stringArray(payload.concerns);
      record.confidence = nullableNumber(payload.adjusted_confidence) ?? record.confidence;
    } else if (event.event_type === "changeset.merged") {
      record.status = "merged";
      record.resultingRevision = nullableNumber(payload.resulting_revision_number);
      record.operationCount = numberValue(payload.operations_applied, record.operationCount);
      record.validationSummary = stringValue(payload.summary, "Change set merged transactionally.");
    } else {
      record.status = stringValue(payload.status, record.status);
      record.title = stringValue(payload.title, record.title);
    }
  }
  return [...changesets.values()].sort((left, right) => right.createdAt.localeCompare(left.createdAt));
}

function revisionsFromEvents(events: EventEnvelope[]): WorkspaceRevision[] {
  return events.flatMap((event) => {
    if (event.event_type !== "revision.created") return [];
    const payload = event.payload;
    const id = stringValue(payload.revision_id);
    const number = nullableNumber(payload.number);
    if (!id || number === null) return [];
    return [{
      id,
      number,
      parentNumber: nullableNumber(payload.parent_revision_number),
      summary: stringValue(payload.summary, "Revision created."),
      operationCount: numberValue(payload.operation_count),
      objectCounts: numberRecord(payload.object_counts_by_type),
      changesetId: nullableString(payload.changeset_id),
      createdBy: stringValue(payload.created_by_agent_kind, stringValue(payload.created_by_id, "System")),
      isReleaseBaseline: booleanValue(payload.is_release_baseline),
      occurredAt: event.occurred_at
    }];
  }).sort((left, right) => right.number - left.number);
}

export function inferEvidenceSourceKind(sourceLabel: string, evidenceKind: string): EvidenceSourceKind {
  const normalized = `${sourceLabel} ${evidenceKind}`.toLowerCase();
  if (normalized.includes(".l5x") || normalized.includes("ladder") || normalized.includes("rung")) return "l5x";
  if (normalized.includes(".xlsx") || normalized.includes("sheet") || normalized.includes("cell")) return "xlsx";
  if (normalized.includes(".csv") || normalized.includes("row")) return "csv";
  if (normalized.includes(".docx") || normalized.includes("paragraph") || normalized.includes("heading")) return "docx";
  if (normalized.includes(".pdf") || normalized.includes("page")) return "pdf";
  if (normalized.includes(".xml") || normalized.includes("xpath")) return "xml";
  if (normalized.includes("requirement")) return "requirement";
  if (evidenceKind === "template_rule") return "template_rule";
  if (evidenceKind === "human_decision") return "human_decision";
  if (evidenceKind === "generated_object") return "generated_object";
  if (evidenceKind === "validation_output") return "validation_output";
  return "unknown";
}

export function evidenceFromFindingDetail(detail: FindingDetail): WorkspaceEvidence[] {
  return detail.evidence.map((evidence) => {
    const sourceLabel = evidence.anchor?.source_label || "Evidence source unavailable";
    return {
      id: evidence.id,
      kind: inferEvidenceSourceKind(sourceLabel, evidence.evidence_kind),
      evidenceKind: evidence.evidence_kind,
      role: evidence.role,
      sourceLabel,
      exactLocation: evidence.anchor?.display_location || "Exact location unavailable",
      excerpt: evidence.anchor?.excerpt || "No excerpt was published.",
      expectation: evidence.anchor?.expectation || "",
      note: evidence.note
    };
  });
}

function detailsText(details: Record<string, unknown>, key: string): string {
  return stringValue(details[key]);
}

function detailsStrings(details: Record<string, unknown>, key: string): string[] {
  return stringArray(details[key]);
}

export function findingToWorkspace(finding: FindingRead): WorkspaceFinding {
  return {
    id: finding.id,
    title: finding.title,
    category: finding.category,
    severity: finding.severity,
    status: finding.status,
    source: "Open the finding to load its cited source.",
    exactLocation: "Exact citation not loaded",
    agent: finding.created_by_agent_kind,
    confidence: finding.confidence,
    evidenceCount: finding.evidence_count,
    discipline: displayText(finding.category),
    reviewer: finding.is_reviewed ? "Reviewed" : "Unassigned",
    approvalStatus: finding.requires_approval ? "Required" : "Not required",
    requiresApproval: finding.requires_approval,
    fact: finding.fact,
    inference: finding.inference,
    expectedMissingEvidence: "",
    parserWarnings: [],
    requirementSide: "",
    implementationSide: "",
    impact: "",
    suggestedAction: finding.suggested_review_action,
    risk: "",
    limitations: finding.limitations,
    comments: finding.decision_notes ? [finding.decision_notes] : [],
    history: [],
    linkedChangesetId: null,
    linkedTests: [],
    evidence: [],
    detailLoaded: false
  };
}

export function mergeFindingDetail(finding: WorkspaceFinding, detail: FindingDetail): WorkspaceFinding {
  const evidence = evidenceFromFindingDetail(detail);
  const details = detail.details;
  const direct = evidence.find((item) => item.evidenceKind === "direct") ?? evidence[0];
  return {
    ...finding,
    source: direct?.sourceLabel || finding.source,
    exactLocation: direct?.exactLocation || finding.exactLocation,
    fact: detail.fact,
    inference: detail.inference,
    expectedMissingEvidence: detailsText(details, "expected_missing_evidence"),
    parserWarnings: detailsStrings(details, "parser_warnings"),
    requirementSide: detailsText(details, "requirement_side"),
    implementationSide: detailsText(details, "implementation_side"),
    impact: detailsText(details, "impact"),
    suggestedAction: detail.suggested_review_action,
    risk: detailsText(details, "risk"),
    limitations: detail.limitations,
    comments: [detail.decision_notes, ...detailsStrings(details, "comments")].filter(Boolean),
    history: detailsStrings(details, "history"),
    linkedChangesetId: nullableString(details.linked_changeset_id),
    linkedTests: detailsStrings(details, "linked_tests"),
    evidence,
    evidenceCount: detail.evidence_count,
    detailLoaded: true
  };
}

function approvalContext(approval: ApprovalRead): Record<string, unknown> {
  return isRecord(approval.context) ? approval.context : {};
}

function contextEvidence(context: Record<string, unknown>): WorkspaceEvidence[] {
  const rawEvidence = context.evidence;
  if (!Array.isArray(rawEvidence)) return [];
  return rawEvidence.flatMap((item, index) => {
    if (!isRecord(item)) return [];
    const sourceLabel = stringValue(item.source_label, "Approval context");
    const evidenceKind = stringValue(item.evidence_kind, "direct");
    return [{
      id: stringValue(item.id, `approval-evidence-${index}`),
      kind: inferEvidenceSourceKind(sourceLabel, evidenceKind),
      evidenceKind,
      role: stringValue(item.role, "supporting"),
      sourceLabel,
      exactLocation: stringValue(item.exact_location, stringValue(item.display_location, "Location not published")),
      excerpt: stringValue(item.excerpt, "No excerpt was published."),
      expectation: stringValue(item.expectation),
      note: stringValue(item.note)
    }];
  });
}

function approvalToWorkspace(approval: ApprovalRead): WorkspaceApproval {
  const context = approvalContext(approval);
  return {
    id: approval.id,
    projectId: approval.project_id,
    findingId: approval.finding_id,
    changesetId: nullableString(context.changeset_id),
    gate: stringValue(context.gate, approval.kind),
    kind: approval.kind,
    status: approval.status,
    title: approval.title,
    requestedDecision: approval.question,
    affectedRevision: nullableNumber(context.affected_revision),
    affectedObjects: stringArray(context.affected_objects),
    evidence: contextEvidence(context),
    validationResults: stringArray(context.validation_results),
    rationale: stringValue(context.agent_rationale),
    risk: stringValue(context.risk),
    severity: stringValue(context.severity),
    reviewer: stringValue(context.reviewer, "Authorized reviewer unassigned"),
    dueDate: nullableString(context.due_date),
    comments: stringArray(context.comments),
    priorDecisions: stringArray(context.prior_decisions),
    downstreamImpact: stringValue(context.downstream_impact),
    restrictedDomain: booleanValue(context.restricted_domain),
    finalRelease: approval.kind === "release_candidate" || booleanValue(context.final_release),
    requestedBy: approval.requested_by_agent_kind,
    requestedAt: approval.requested_at,
    allowedDecisions: approval.status === "pending" ? ["approve", "reject"] : []
  };
}

export function buildWorkspaceModel(source: WorkspaceSource): WorkspaceModel {
  if (source.dataMode === "demo") return createDemoWorkspaceModel();

  const orderedEvents = [...source.events].sort((left, right) => left.seq - right.seq);
  const revisions = revisionsFromEvents(orderedEvents);
  const locations = taskLocations(orderedEvents);
  const project = source.project ? {
    id: source.project.id,
    reference: source.project.client_reference,
    name: source.project.name,
    description: source.project.description,
    status: source.project.status,
    revision: revisions[0]?.number ?? null,
    updatedAt: source.project.updated_at
  } : null;

  const live = source.dataMode === "live";
  const contractPending = "The current contract does not publish this canonical resource yet.";
  return {
    mode: source.dataMode,
    demoLabel: null,
    unavailableReason: source.fallbackReason,
    project,
    capabilities: {
      intakeSubmission: {
        level: live ? "available" : "unavailable",
        reason: live
          ? "Project identity, target platform, pinned standards, files, and review boundaries are submitted to the authorized local workflow."
          : source.fallbackReason
      },
      projectCore: { level: live ? "summary_only" : "unavailable", reason: contractPending },
      deliveryCore: {
        level: live ? "summary_only" : "unavailable",
        reason: live
          ? "The current contract publishes delivery tasks and findings, but not canonical HMI, alarm, or test resources."
          : source.fallbackReason
      },
      changesetOperations: { level: live ? "summary_only" : "unavailable", reason: contractPending },
      revisionComparison: { level: live ? "summary_only" : "unavailable", reason: contractPending },
      findingEvidence: {
        level: live ? "available" : "unavailable",
        reason: live ? "Finding citations are loaded from the local backend on demand." : source.fallbackReason
      },
      approvalDecisions: {
        level: live ? "summary_only" : "unavailable",
        reason: live
          ? "The current endpoint supports signed Approve or Reject decisions only."
          : source.fallbackReason
      }
    },
    files: source.files.map((file) => ({
      id: file.id,
      name: file.original_filename,
      kind: file.file_kind,
      role: file.document_role,
      sizeBytes: file.size_bytes,
      sha256: file.sha256,
      status: file.status,
      detail: file.status_detail,
      archiveMemberPath: file.archive_member_path,
      updatedAt: file.updated_at
    })),
    tasks: source.tasks.map((task) => taskToWorkspace(task, locations.get(task.id) ?? null)),
    requirements: source.requirements.length
      ? source.requirements.map(requirementFromResource)
      : orderedEvents.flatMap((event) => requirementFromEvent(event) ?? []),
    devices: [],
    tags: [],
    plcPrograms: [],
    hmiScreens: [],
    alarms: [],
    testProcedures: [],
    documents: [],
    plans: plansFromEvents(orderedEvents),
    changesets: changesetsFromEvents(orderedEvents),
    revisions,
    findings: source.findings.map(findingToWorkspace),
    approvals: source.approvals.map(approvalToWorkspace)
  };
}
