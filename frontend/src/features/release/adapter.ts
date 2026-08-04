import type { EventEnvelope, ProjectDetail, ReportRead } from "../../api/types";
import type {
  ApprovalGateKey,
  ExportArtifactCreatedEventPayload,
  ExportStartedEventPayload,
  GateStatusChangedEventPayload,
  ReleaseCandidateCreatedEventPayload,
  ReleaseReadyEventPayload,
  RevisionCreatedEventPayload,
  ValidationCompletedEventPayload,
  ValidationLevel
} from "../../generated";
import type {
  ReadinessState,
  ReleaseActionAvailability,
  ReleaseActionId,
  ReleaseArtifact,
  ReleaseArtifactFolder,
  ReleaseArtifactFolderId,
  ReleaseCandidateSummary,
  ReleaseModel,
  ReleaseReadinessGate,
  ReleaseReportCatalogItem,
  ReleaseReportFile,
  ReleaseSource,
  ReleaseVerificationSummary,
  ReportProvenanceItem
} from "./types";

interface ReportDefinition {
  id: string;
  title: string;
  description: string;
}

interface GateDefinition {
  id: string;
  label: string;
  group: ApprovalGateKey | "artifact_hashes" | "generator_status";
  absentDetail: string;
}

export const REPORT_DEFINITIONS: readonly ReportDefinition[] = [
  { id: "executive_summary", title: "Executive Summary", description: "Scope, status, material findings, approvals, and validation facts." },
  { id: "detailed_engineering_report", title: "Detailed Engineering Report", description: "Cross-discipline canonical engineering model and review record." },
  { id: "requirements_traceability_matrix", title: "Requirements Traceability Matrix", description: "Source wording, implementation coverage, and verification method." },
  { id: "device_io_schedule", title: "Device and I/O Schedule", description: "Devices, fail states, physical assignments, and signal metadata." },
  { id: "tag_register", title: "Tag Register", description: "Names, scopes, data types, aliases, references, and approval state." },
  { id: "plc_program_routine_index", title: "PLC Program and Routine Index", description: "Controller task, program, routine, language, and provenance index." },
  { id: "interlock_matrix", title: "Interlock Matrix", description: "Documented conditions compared with implementation evidence." },
  { id: "hmi_screen_binding_register", title: "HMI Screen and Binding Register", description: "Template screens, instances, bindings, and unresolved references." },
  { id: "alarm_register", title: "Alarm Register", description: "Triggers, priorities, messages, delays, and operator response guidance." },
  { id: "findings_register", title: "Findings Register", description: "Facts, inferences, evidence posture, status, and human disposition." },
  { id: "iat_plan", title: "IAT Plan", description: "Recorded integration acceptance procedures and expected results." },
  { id: "fat_plan", title: "FAT Plan", description: "Recorded factory acceptance procedures and expected results." },
  { id: "manual_mode_test_plan", title: "Manual Mode Test Plan", description: "Manual-mode procedures, safety notes, and traceability." },
  { id: "automatic_mode_test_plan", title: "Automatic Mode Test Plan", description: "Automatic-mode procedures, safety notes, and traceability." },
  { id: "change_log", title: "Change Log", description: "Revision-scoped structured changes and reviewer decisions." },
  { id: "approval_record", title: "Approval Record", description: "Named human requests and decisions pinned to revisions." },
  { id: "validation_summary", title: "Validation Summary", description: "Deterministic validation scope, results, waivers, and required actions." },
  { id: "import_order_guide", title: "Import Order Guide", description: "Recorded artifact order and actual validation level." },
  { id: "known_limitations", title: "Known Limitations", description: "Unsupported inputs, verification boundaries, and unresolved scope." }
] as const;

const GATE_DEFINITIONS: readonly GateDefinition[] = [
  { id: "profile", label: "Profile complete", group: "project_profile", absentDetail: "No project-profile gate event has been received." },
  { id: "questions", label: "Mandatory questions resolved", group: "project_profile", absentDetail: "Question readiness is not reported separately by the current event contract." },
  { id: "requirements", label: "Requirements coverage", group: "project_profile", absentDetail: "Requirements coverage has not been reported by a gate event." },
  { id: "tag-io", label: "Tag / I/O gate", group: "devices_io_tags", absentDetail: "No device, I/O, and tag gate event has been received." },
  { id: "logic", label: "Logic gate", group: "logic_and_delivery", absentDetail: "No logic and delivery gate event has been received." },
  { id: "hmi-alarm", label: "HMI / alarm gate", group: "logic_and_delivery", absentDetail: "HMI and alarm readiness is grouped with logic and delivery in the contract." },
  { id: "qa", label: "QA gate", group: "qa_and_risk", absentDetail: "No QA and risk gate event has been received." },
  { id: "tests", label: "Test coverage", group: "qa_and_risk", absentDetail: "Test coverage is grouped with QA and risk in the current contract." },
  { id: "critical-findings", label: "Critical findings", group: "qa_and_risk", absentDetail: "Critical-finding clearance has not been reported by a gate event." },
  { id: "approvals", label: "Approvals", group: "release", absentDetail: "No final release gate event has been received." },
  { id: "generators", label: "Generator status", group: "generator_status", absentDetail: "No release generation event has been received." },
  { id: "import", label: "Import verification", group: "import_verification", absentDetail: "Import verification has not been attempted or reported." },
  { id: "hashes", label: "Artifact hashes", group: "artifact_hashes", absentDetail: "No event-backed artifact hashes are available." }
] as const;

const FOLDER_DEFINITIONS: ReadonlyArray<Omit<ReleaseArtifactFolder, "artifacts">> = [
  { id: "00-manifest", sequence: "00", label: "Manifest", description: "Manifest, target profile, checksums, and package index." },
  { id: "01-controller", sequence: "01", label: "Controller", description: "Controller, program, routine, type, tag, and import-order candidates." },
  { id: "02-hmi", sequence: "02", label: "HMI", description: "Target-aware HMI display and tag candidates." },
  { id: "03-alarms", sequence: "03", label: "Alarms", description: "Alarm import candidates and cross-reference material." },
  { id: "04-testing", sequence: "04", label: "Testing", description: "IAT, FAT, manual, and automatic test plans." },
  { id: "05-reports", sequence: "05", label: "Reports", description: "Engineering reports, limitations, and validation summaries." },
  { id: "06-audit", sequence: "06", label: "Audit", description: "Approvals, evidence indexes, and change history." }
];

const VALIDATION_RANK: Record<ValidationLevel, number> = {
  generated: 0,
  schema_validated: 1,
  internal_semantic_validated: 2,
  round_trip_parsed: 3,
  rockwell_import_verified: 4,
  human_approved: 5,
  released: 6
};

function payloadAs<T>(event: EventEnvelope): T {
  return event.payload as unknown as T;
}

function ordered(events: EventEnvelope[]): EventEnvelope[] {
  return [...events].sort((left, right) => left.seq - right.seq);
}

function latestEvent(events: EventEnvelope[], eventType: EventEnvelope["event_type"]): EventEnvelope | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].event_type === eventType) return events[index];
  }
  return null;
}

function latestGateEvents(events: EventEnvelope[]): Map<ApprovalGateKey, { payload: GateStatusChangedEventPayload; event: EventEnvelope }> {
  const result = new Map<ApprovalGateKey, { payload: GateStatusChangedEventPayload; event: EventEnvelope }>();
  events.forEach((event) => {
    if (event.event_type !== "gate.status_changed") return;
    const payload = payloadAs<GateStatusChangedEventPayload>(event);
    if (typeof payload.gate_key === "string") result.set(payload.gate_key, { payload, event });
  });
  return result;
}

function targetProfile(project: ProjectDetail | null): string {
  if (!project) return "Not available";
  const intake = project.attributes.esco_product_intake;
  const intakeTarget = typeof intake === "object" && intake !== null
    ? (intake as Record<string, unknown>).target_profile
    : undefined;
  const value = intakeTarget ?? project.attributes.target_profile;
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value !== "object" || value === null) return "Not reported by project API";
  const profile = value as Record<string, unknown>;
  const parts = [
    profile.controller_family,
    profile.catalog_number,
    profile.firmware ? `FW ${String(profile.firmware)}` : "",
    profile.studio_5000_version ? `Studio ${String(profile.studio_5000_version)}` : "",
    profile.hmi_product,
    profile.hmi_version
  ].filter((item): item is string => typeof item === "string" && Boolean(item.trim()));
  return parts.length ? parts.join(" · ") : "Profile exists; display fields are unavailable";
}

function latestRevision(events: EventEnvelope[]): number | null {
  const event = latestEvent(events, "revision.created");
  if (!event) return null;
  const payload = payloadAs<RevisionCreatedEventPayload>(event);
  return typeof payload.number === "number" ? payload.number : null;
}

function mapGateState(payload: GateStatusChangedEventPayload): ReadinessState {
  if (payload.is_enabled === false || payload.status === "skipped") return "unavailable";
  if (payload.status === "passed") return "passed";
  if (payload.status === "ready") return "ready";
  if (payload.status === "awaiting_decision") return "pending";
  if (payload.status === "failed" || payload.status === "not_ready") return "blocked";
  return "unknown";
}

function gateDetail(payload: GateStatusChangedEventPayload): string {
  if (payload.is_enabled === false) return payload.disabled_reason || "This gate is disabled by project policy.";
  if (payload.status === "passed") return "The backend recorded this grouped gate as passed.";
  if (payload.status === "ready") return "The backend recorded this grouped gate as ready for decision.";
  if (payload.status === "awaiting_decision") return "A named human decision is required.";
  if (payload.status === "skipped") return payload.disabled_reason || "The backend recorded this gate as skipped.";
  if (payload.unmet_criteria?.length) return payload.unmet_criteria.join(" · ");
  return payload.disabled_reason || `The backend recorded this gate as ${payload.status.replaceAll("_", " ")}.`;
}

function artifactFolder(kind: ExportArtifactCreatedEventPayload["kind"]): ReleaseArtifactFolderId {
  if (["manifest", "target_profile", "checksums"].includes(kind)) return "00-manifest";
  if (["controller_project_l5x", "program_l5x", "routine_l5x", "udt_l5x", "aoi_l5x", "tag_csv", "comment_csv", "cross_reference_csv", "import_order"].includes(kind)) return "01-controller";
  if (["hmi_display_xml", "hmi_tag_csv"].includes(kind)) return "02-hmi";
  if (kind === "alarm_import") return "03-alarms";
  if (kind === "test_plan") return "04-testing";
  if (["report", "validation_summary", "limitations"].includes(kind)) return "05-reports";
  return "06-audit";
}

function buildCandidate(source: ReleaseSource, events: EventEnvelope[]): ReleaseCandidateSummary | null {
  const persisted = source.releaseCandidates?.[0];
  if (persisted) {
    return {
      id: persisted.id,
      number: persisted.candidate_number,
      name: persisted.name,
      revision: persisted.revision_number,
      status: persisted.status,
      validationLevel: persisted.validation_level,
      artifactCount: persisted.artifact_count,
      expectedArtifactCount: persisted.manifest?.artifact_count ?? persisted.artifact_count,
      limitationCount: persisted.limitations.length,
      unsupportedItemCount: persisted.unsupported_items.length,
      generatorVersion: persisted.generator_version || "Not reported",
      packageBytes: persisted.archive?.size_bytes ?? persisted.package_bytes,
      packageSha256: persisted.archive?.sha256 ?? persisted.package_sha256,
      createdAt: persisted.created_at
    };
  }
  const headEvent = [...events].reverse().find((event) => event.event_type === "release.candidate_created" || event.event_type === "release.ready");
  if (!headEvent) return null;
  const candidateId = headEvent.event_type === "release.ready"
    ? payloadAs<ReleaseReadyEventPayload>(headEvent).release_candidate_id
    : payloadAs<ReleaseCandidateCreatedEventPayload>(headEvent).release_candidate_id;
  if (!candidateId) return null;
  const createdEvent = [...events].reverse().find((event) => event.event_type === "release.candidate_created" && payloadAs<ReleaseCandidateCreatedEventPayload>(event).release_candidate_id === candidateId) ?? null;
  const readyEvent = [...events].reverse().find((event) => event.event_type === "release.ready" && payloadAs<ReleaseReadyEventPayload>(event).release_candidate_id === candidateId) ?? null;
  const created = createdEvent ? payloadAs<ReleaseCandidateCreatedEventPayload>(createdEvent) : null;
  const ready = readyEvent ? payloadAs<ReleaseReadyEventPayload>(readyEvent) : null;
  const exportEvent = [...events].reverse().find((event) => {
    if (event.event_type !== "export.started") return false;
    return payloadAs<ExportStartedEventPayload>(event).release_candidate_id === candidateId;
  });
  const started = exportEvent ? payloadAs<ExportStartedEventPayload>(exportEvent) : null;
  return {
    id: candidateId,
    number: ready?.candidate_number ?? created?.candidate_number ?? started?.candidate_number ?? 0,
    name: created?.name || `Release candidate ${ready?.candidate_number ?? created?.candidate_number ?? ""}`.trim(),
    revision: ready?.revision_number ?? created?.revision_number ?? started?.revision_number ?? 0,
    status: ready?.status ?? created?.status ?? "draft",
    validationLevel: ready?.validation_level ?? created?.validation_level ?? "generated",
    artifactCount: ready?.artifact_count ?? created?.artifact_count ?? 0,
    expectedArtifactCount: typeof started?.expected_artifact_count === "number" ? started.expected_artifact_count : null,
    limitationCount: ready?.limitation_count ?? created?.limitation_count ?? 0,
    unsupportedItemCount: ready?.unsupported_item_count ?? created?.unsupported_item_count ?? 0,
    generatorVersion: created?.generator_version || "Not reported",
    packageBytes: ready?.package_bytes ?? created?.package_bytes ?? 0,
    packageSha256: ready?.package_sha256 ?? created?.package_sha256 ?? "",
    createdAt: readyEvent?.occurred_at ?? createdEvent?.occurred_at ?? exportEvent?.occurred_at ?? ""
  };
}

function buildVerification(source: ReleaseSource, events: EventEnvelope[], candidate: ReleaseCandidateSummary | null): ReleaseVerificationSummary {
  const persisted = source.releaseCandidates?.find((item) => item.id === candidate?.id);
  if (persisted) {
    return {
      status: persisted.import_verification_status,
      detail: persisted.import_verification_detail || `Import verification is ${persisted.import_verification_status.replaceAll("_", " ")}.`,
      candidateOnly: !persisted.archive?.rockwell_import_verified,
      validationLevel: persisted.validation_level,
      occurredAt: persisted.archive?.finalized_at ?? persisted.released_at ?? persisted.approved_at
    };
  }
  const event = [...events].reverse().find((item) => item.event_type === "release.ready" && (!candidate || payloadAs<ReleaseReadyEventPayload>(item).release_candidate_id === candidate.id)) ?? null;
  if (!event) {
    return {
      status: "not_attempted",
      detail: "No release.ready event has reported import verification.",
      candidateOnly: true,
      validationLevel: null,
      occurredAt: null
    };
  }
  const payload = payloadAs<ReleaseReadyEventPayload>(event);
  return {
    status: payload.import_verification_status,
    detail: payload.import_verification_detail || (payload.import_verification_status === "passed"
      ? "A licensed-workstation import verification was reported as passed."
      : `Import verification is ${payload.import_verification_status.replaceAll("_", " ")}.`),
    candidateOnly: payload.is_import_ready_candidate_only ?? true,
    validationLevel: payload.validation_level,
    occurredAt: event.occurred_at
  };
}

function buildArtifacts(source: ReleaseSource, events: EventEnvelope[], candidate: ReleaseCandidateSummary | null): ReleaseArtifact[] {
  const revision = candidate?.revision ?? latestRevision(events);
  if (source.releaseArtifacts?.length) {
    return source.releaseArtifacts
      .filter((artifact) => !candidate || artifact.release_candidate_id === candidate.id)
      .map((artifact) => ({
        id: artifact.id,
        candidateId: artifact.release_candidate_id,
        folderId: artifactFolder(artifact.kind),
        filename: artifact.filename,
        kind: artifact.kind,
        relativePath: artifact.relative_path || artifact.filename,
        sizeBytes: artifact.size_bytes,
        sha256: artifact.sha256,
        generator: artifact.is_produced_by_plugin
          ? artifact.plugin_name || "Unidentified plugin"
          : [artifact.generator_name, artifact.generator_version].filter(Boolean).join(" ") || "ESCO release generator",
        target: targetProfile(source.project),
        validationLevel: artifact.validation_level,
        createdAt: artifact.created_at,
        sourceRevision: revision,
        warningCount: artifact.warnings.length,
        importOrder: artifact.import_order,
        producedByPlugin: artifact.is_produced_by_plugin,
        download: source.artifactDownloads?.[artifact.id] ?? null,
        diagnostics: [
          ...artifact.warnings.map((warning) => typeof warning === "string" ? warning : JSON.stringify(warning)),
          ...(source.artifactDownloads?.[artifact.id] ? [] : ["Artifact metadata is available, but the backend does not expose an individual artifact download route."])
        ]
      }))
      .sort((left, right) => left.importOrder - right.importOrder || left.relativePath.localeCompare(right.relativePath));
  }
  const artifacts = new Map<string, ReleaseArtifact>();
  events.forEach((event) => {
    if (event.event_type !== "export.artifact_created") return;
    const payload = payloadAs<ExportArtifactCreatedEventPayload>(event);
    if (!payload.artifact_id || !payload.release_candidate_id || !payload.filename) return;
    if (candidate && payload.release_candidate_id !== candidate.id) return;
    const diagnostics: string[] = [];
    if ((payload.warning_count ?? 0) > 0) diagnostics.push(`${payload.warning_count} generator warning${payload.warning_count === 1 ? "" : "s"}`);
    if (!payload.sha256) diagnostics.push("SHA-256 was not supplied on the artifact event.");
    if (!source.artifactDownloads?.[payload.artifact_id]) diagnostics.push("No authorized artifact download link was supplied by the API host.");
    if (payload.is_produced_by_plugin) diagnostics.push(`Produced by plugin ${payload.plugin_name || "(name not reported)"}.`);
    artifacts.set(payload.artifact_id, {
      id: payload.artifact_id,
      candidateId: payload.release_candidate_id,
      folderId: artifactFolder(payload.kind),
      filename: payload.filename,
      kind: payload.kind,
      relativePath: payload.relative_path || payload.filename,
      sizeBytes: payload.size_bytes ?? 0,
      sha256: payload.sha256 || "",
      generator: payload.is_produced_by_plugin ? payload.plugin_name || "Unidentified plugin" : payload.plugin_name || "ESCO release generator",
      target: targetProfile(source.project),
      validationLevel: payload.validation_level,
      createdAt: event.occurred_at,
      sourceRevision: revision,
      warningCount: payload.warning_count ?? 0,
      importOrder: payload.import_order ?? 0,
      producedByPlugin: payload.is_produced_by_plugin ?? false,
      download: source.artifactDownloads?.[payload.artifact_id] ?? null,
      diagnostics
    });
  });
  return [...artifacts.values()].sort((left, right) => left.importOrder - right.importOrder || left.relativePath.localeCompare(right.relativePath));
}

function buildFolders(artifacts: ReleaseArtifact[]): ReleaseArtifactFolder[] {
  return FOLDER_DEFINITIONS.map((folder) => ({
    ...folder,
    artifacts: artifacts.filter((artifact) => artifact.folderId === folder.id)
  }));
}

function summaryValue(summary: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = summary[key];
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (Array.isArray(value)) return value.length ? `${value.length} recorded` : "0 recorded";
    if (typeof value === "object" && value !== null) return `${Object.keys(value).length} recorded`;
  }
  return "Not exposed in API summary";
}

function reportProvenance(report: ReportRead, project: ProjectDetail | null, revision: number | null): ReportProvenanceItem[] {
  const summary = report.summary ?? {};
  const item = (label: string, value: string): ReportProvenanceItem => ({
    label,
    value,
    available: value !== "Not exposed in API summary" && value !== "Not reported by project API"
  });
  return [
    item("Project / revision", project ? `${project.name} · ${revision === null ? "revision not reported" : `revision ${revision}`}` : "Not exposed in API summary"),
    item("Target profile", targetProfile(project)),
    item("Files reviewed", summaryValue(summary, ["files_reviewed", "files_reviewed_count"])),
    item("Files unsupported", summaryValue(summary, ["files_unsupported", "unsupported_files", "files_unsupported_count"])),
    item("Templates used", summaryValue(summary, ["templates_used", "template_ids"])),
    item("Generator", [report.provider_name, report.provider_version].filter(Boolean).join(" ") || "Not exposed in API summary"),
    item("Evidence references", summaryValue(summary, ["evidence_references", "evidence_reference_count"])),
    item("Findings", report.finding_ids?.length ? `${report.finding_ids.length} linked` : summaryValue(summary, ["finding_count", "findings"])),
    item("Human approvals", summaryValue(summary, ["human_approvals", "approval_count", "human_approved"])),
    item("Validation level", summaryValue(summary, ["validation_level"])),
    item("Report version", summaryValue(summary, ["report_version"])),
    item("Disclaimer / status", summaryValue(summary, ["disclaimer", "document_status"]))
  ];
}

function reportDownload(source: ReleaseSource, report: ReportRead) {
  if (report.status !== "generated" || !report.download_path) return null;
  const explicit = source.artifactDownloads?.[report.id];
  if (explicit) return explicit;
  return null;
}

function buildReportFile(source: ReleaseSource, report: ReportRead, revision: number | null): ReleaseReportFile {
  return {
    id: report.id,
    title: report.title,
    reportType: report.report_type,
    format: report.export_format.toLowerCase(),
    status: report.status,
    mediaType: report.media_type,
    sizeBytes: report.size_bytes ?? 0,
    sha256: report.sha256 || "",
    provider: report.provider_name || "Not reported",
    providerVersion: (report as ReportRead & { provider_version?: string }).provider_version || "Not reported",
    createdAt: report.created_at,
    error: report.error_message || "",
    download: reportDownload(source, report),
    provenance: reportProvenance(report, source.project, revision)
  };
}

function normalizeReportType(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
}

function buildReports(source: ReleaseSource, revision: number | null): ReleaseReportCatalogItem[] {
  const files = source.reports.map((report) => buildReportFile(source, report, revision));
  return REPORT_DEFINITIONS.map((definition) => ({
    ...definition,
    files: files
      .filter((report) => normalizeReportType(report.reportType) === definition.id)
      .sort((left, right) => left.format.localeCompare(right.format) || right.createdAt.localeCompare(left.createdAt))
  }));
}

function buildValidation(events: EventEnvelope[]): ReleaseModel["validation"] {
  const event = latestEvent(events, "validation.completed");
  if (!event) return null;
  const payload = payloadAs<ValidationCompletedEventPayload>(event);
  if (!payload.validation_run_id || typeof payload.revision_number !== "number") return null;
  return {
    id: payload.validation_run_id,
    revision: payload.revision_number,
    status: payload.status,
    checksRun: payload.checks_run ?? 0,
    errorCount: payload.error_count ?? 0,
    warningCount: payload.warning_count ?? 0,
    humanReviewCount: payload.human_review_count ?? 0,
    unsupportedCount: payload.unsupported_count ?? 0,
    isBlocking: payload.is_blocking ?? false,
    disciplines: [...(payload.disciplines ?? [])],
    summary: payload.summary,
    occurredAt: event.occurred_at
  };
}

function buildGates(
  source: ReleaseSource,
  events: EventEnvelope[],
  candidate: ReleaseCandidateSummary | null,
  verification: ReleaseVerificationSummary,
  artifacts: ReleaseArtifact[]
): ReleaseReadinessGate[] {
  const grouped = latestGateEvents(events);
  return GATE_DEFINITIONS.map((definition) => {
    if (definition.group === "generator_status") {
      if (!candidate) return { ...definition, state: "unknown", detail: definition.absentDetail, unmetCriteria: [], source: "derived" };
      const blocked = candidate.status === "failed" || candidate.status === "validation_failed";
      return {
        ...definition,
        state: blocked ? "blocked" : candidate.status === "generating" ? "pending" : "passed",
        detail: `Candidate ${candidate.number} is ${candidate.status.replaceAll("_", " ")} with ${candidate.artifactCount} artifact${candidate.artifactCount === 1 ? "" : "s"} reported.`,
        unmetCriteria: blocked ? ["Release generation or validation failed."] : [],
        source: "derived"
      };
    }
    if (definition.group === "artifact_hashes") {
      if (!artifacts.length) return { ...definition, state: "unknown", detail: definition.absentDetail, unmetCriteria: [], source: "derived" };
      const hashed = artifacts.filter((artifact) => Boolean(artifact.sha256)).length;
      const expected = candidate?.expectedArtifactCount ?? candidate?.artifactCount ?? artifacts.length;
      const complete = hashed === artifacts.length && artifacts.length >= expected;
      return {
        ...definition,
        state: complete ? "passed" : "blocked",
        detail: `${hashed} of ${artifacts.length} event-backed artifacts include SHA-256; ${expected} artifact${expected === 1 ? " is" : "s are"} expected.`,
        unmetCriteria: complete ? [] : ["Every expected artifact must be present with an event-reported SHA-256."],
        source: "derived"
      };
    }
    if (definition.group === "import_verification" && verification.occurredAt) {
      const state: ReadinessState = verification.status === "passed"
        ? "passed"
        : verification.status === "queued" || verification.status === "running"
          ? "pending"
          : verification.status === "unavailable"
            ? "unavailable"
            : verification.status === "not_attempted"
              ? "unknown"
              : "blocked";
      return { ...definition, state, detail: verification.detail, unmetCriteria: state === "blocked" ? [verification.detail] : [], source: "event" };
    }
    const eventGate = grouped.get(definition.group);
    if (eventGate) {
      return {
        ...definition,
        state: mapGateState(eventGate.payload),
        detail: gateDetail(eventGate.payload),
        unmetCriteria: [...(eventGate.payload.unmet_criteria ?? [])],
        source: "event",
        eventStatus: eventGate.payload.status
      };
    }
    if (definition.id === "approvals" && (source.project?.counts.approvals_pending ?? 0) > 0) {
      const pending = source.project?.counts.approvals_pending ?? 0;
      return {
        ...definition,
        state: "pending",
        detail: `${pending} approval${pending === 1 ? " is" : "s are"} pending; gate criteria are not available in the project summary.`,
        unmetCriteria: [`Resolve ${pending} pending approval${pending === 1 ? "" : "s"}.`],
        source: "derived"
      };
    }
    return {
      ...definition,
      state: source.dataMode === "unavailable" ? "unavailable" : "unknown",
      detail: source.dataMode === "unavailable" ? source.fallbackReason : definition.absentDetail,
      unmetCriteria: [],
      source: source.dataMode === "unavailable" ? "unavailable" : "derived"
    };
  });
}

function userPermissions(source: ReleaseSource) {
  if (source.dataMode === "demo") return { canEngineer: false, canReview: false, canView: true, summary: "Synthetic demo viewer" };
  const user = source.currentUser?.user;
  if (!user || !source.project) return { canEngineer: false, canReview: false, canView: false, summary: "No authorized project role" };
  const projectRole = source.currentUser?.project_roles[source.project.id] || "none";
  const globalRoles = user.roles;
  const elevated = globalRoles.includes("administrator");
  const canEngineer = elevated || (globalRoles.includes("engineer") && ["owner", "engineer"].includes(projectRole));
  const canReview = elevated || (globalRoles.includes("reviewer") && ["owner", "reviewer"].includes(projectRole));
  const canView = elevated || ["owner", "engineer", "reviewer", "viewer"].includes(projectRole);
  return {
    canEngineer,
    canReview,
    canView,
    summary: `${user.display_name} · ${projectRole} · ${globalRoles.join(", ") || "no global role"}`
  };
}

function disabledReason(
  source: ReleaseSource,
  hasOperation: boolean,
  permission: boolean,
  prerequisite: string | null,
  capabilityLabel: string
): string | null {
  if (source.dataMode === "demo") return "Synthetic demo mode never calls release or generation APIs.";
  if (source.dataMode === "unavailable") return source.fallbackReason || "Authorized project data is unavailable.";
  if (!source.project) return "Select an authorized project first.";
  if (!permission) return "Your global and project roles do not permit this engineering action.";
  if (!hasOperation) return `The current local backend contract does not expose a ${capabilityLabel} operation.`;
  return prerequisite;
}

function action(
  id: ReleaseActionId,
  label: string,
  reason: string | null,
  enabledReason: string
): ReleaseActionAvailability {
  return { id, label, enabled: reason === null, reason: reason ?? enabledReason };
}

function buildActions(
  source: ReleaseSource,
  support: Partial<Record<ReleaseActionId, boolean>>,
  candidate: ReleaseCandidateSummary | null,
  validation: ReleaseModel["validation"],
  verification: ReleaseVerificationSummary,
  gates: ReleaseReadinessGate[],
  artifacts: ReleaseArtifact[]
): ReleaseActionAvailability[] {
  const permissions = userPermissions(source);
  const revision = candidate?.revision ?? validation?.revision ?? null;
  const persistedCandidate = source.releaseCandidates?.find((item) => item.id === candidate?.id) ?? null;
  const qaBlocked = gates.some((gate) => ["qa", "critical-findings"].includes(gate.id) && gate.state === "blocked");
  const validationPrerequisite = revision === null ? "A numbered project revision is required before validation." : null;
  const candidatePrerequisite = source.releaseReadiness
    ? source.releaseReadiness.is_ready
      ? null
      : source.releaseReadiness.blocking.map((block) => block.summary).join(" · ") || "Release readiness is blocked."
    : !validation
      ? "Release readiness has not been reported by the backend."
      : validation.isBlocking
        ? "The latest validation result is blocking."
        : null;
  const artifactPrerequisite = !candidate
    ? "Create a release candidate before generating artifacts."
    : persistedCandidate && !persistedCandidate.actions.generate.available
      ? persistedCandidate.actions.generate.reason
      : null;
  const verificationPrerequisite = !candidate
    ? "Create a release candidate before requesting import verification."
    : VALIDATION_RANK[candidate.validationLevel] < VALIDATION_RANK.round_trip_parsed
      ? "The candidate has not reached round-trip-parsed validation."
      : artifacts.length === 0
        ? "No event-backed artifacts are available for import verification."
        : null;
  const approvalPrerequisite = !candidate
    ? "Create a release candidate before requesting final approval."
    : qaBlocked
      ? "QA or critical-finding readiness is blocked."
      : null;
  const finalizePrerequisite = !candidate
    ? "Create and approve a release candidate before finalizing delivery."
    : persistedCandidate && !persistedCandidate.actions.finalize.available
      ? persistedCandidate.actions.finalize.reason
      : !persistedCandidate
        ? "The persisted candidate API has not confirmed that finalization is available."
        : null;
  const downloadPrerequisite = !source.releaseZipDownload
    ? "The API has not supplied an authorized release ZIP download link."
    : null;
  return [
    action("run_validation", "Run Validation", disabledReason(source, Boolean(support.run_validation), permissions.canEngineer, validationPrerequisite, "release validation"), "Validation will run against the current numbered revision."),
    action("create_release_candidate", "Create Release Candidate", disabledReason(source, Boolean(support.create_release_candidate), permissions.canEngineer, candidatePrerequisite, "release candidate creation"), "The latest non-blocking validation can be assembled into a candidate."),
    action("generate_artifacts", "Generate Artifacts", disabledReason(source, Boolean(support.generate_artifacts), permissions.canEngineer, artifactPrerequisite, "artifact generation"), "Generation will target the current candidate."),
    action("request_import_verification", "Request Import Verification", disabledReason(source, Boolean(support.request_import_verification), permissions.canEngineer, verificationPrerequisite, "licensed-workstation import verification"), "The candidate can be queued for an authorized workstation check."),
    action("request_final_approval", "Request Final Approval", disabledReason(source, Boolean(support.request_final_approval), permissions.canEngineer, approvalPrerequisite, "final release approval"), "The candidate can be sent to a named human reviewer."),
    action("finalize_release", "Finalize Approved Release", disabledReason(source, Boolean(support.finalize_release), permissions.canReview, finalizePrerequisite, "immutable release finalization"), "A named reviewer can finalize the approved candidate into immutable delivery bytes."),
    action("download_release_zip", "Download Release ZIP", disabledReason(source, Boolean(source.releaseZipDownload), permissions.canView, downloadPrerequisite, "release ZIP download"), "The API supplied an authorized package link after release readiness was recorded.")
  ];
}

export function buildReleaseModel(
  source: ReleaseSource,
  operationSupport: Partial<Record<ReleaseActionId, boolean>> = {}
): ReleaseModel {
  const events = ordered(source.events);
  const candidate = buildCandidate(source, events);
  const verification = buildVerification(source, events, candidate);
  const artifacts = buildArtifacts(source, events, candidate);
  const validation = buildValidation(events);
  const gates = buildGates(source, events, candidate, verification, artifacts);
  const revision = candidate?.revision ?? validation?.revision ?? latestRevision(events);
  const reports = buildReports(source, revision);
  const permissions = userPermissions(source);
  const persistedCandidate = source.releaseCandidates?.find((item) => item.id === candidate?.id) ?? null;
  const releaseEvent = [...events].reverse().find((event) => event.event_type === "release.ready" && (!candidate || payloadAs<ReleaseReadyEventPayload>(event).release_candidate_id === candidate.id)) ?? null;
  return {
    mode: source.dataMode,
    demoLabel: source.dataMode === "demo" ? "DEMO DATA · SYNTHETIC RELEASE RECORDS" : null,
    unavailableReason: source.dataMode === "unavailable" ? source.fallbackReason : "",
    project: source.project ? {
      id: source.project.id,
      reference: source.project.client_reference || source.project.id,
      name: source.project.name,
      revision,
      status: source.project.status,
      targetProfile: targetProfile(source.project)
    } : null,
    candidate,
    verification,
    validation,
    gates,
    folders: buildFolders(artifacts),
    reports,
    reportFormats: [...new Set(source.reports.map((report) => report.export_format.toLowerCase()))].sort(),
    actions: buildActions(source, operationSupport, candidate, validation, verification, gates, artifacts),
    releaseZipDownload: source.releaseZipDownload ?? null,
    userRoleSummary: permissions.summary,
    artifactCoverage: {
      actual: artifacts.length,
      expected: candidate?.expectedArtifactCount ?? candidate?.artifactCount ?? null,
      hashed: artifacts.filter((artifact) => Boolean(artifact.sha256)).length
    },
    releaseOccurredAt: persistedCandidate?.archive?.finalized_at ?? persistedCandidate?.released_at ?? releaseEvent?.occurred_at ?? null
  };
}
