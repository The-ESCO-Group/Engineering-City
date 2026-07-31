import { describe, expect, it } from "vitest";
import type { EventEnvelope, ProjectDetail, ReportRead } from "../../api/types";
import { buildReleaseModel } from "./adapter";
import { createDemoReleaseSource } from "./demo";
import type { ReleaseSource } from "./types";

const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const CANDIDATE_ID = "10000000-0000-4000-8000-000000000002";
const ARTIFACT_ID = "10000000-0000-4000-8000-000000000003";

const project: ProjectDetail = {
  id: PROJECT_ID,
  name: "Line 4 Packaging Conveyor Upgrade",
  description: "Synthetic test project",
  client_reference: "PRJ-0241",
  status: "ready_for_export",
  is_paused: false,
  event_seq: 10,
  created_at: "2026-07-31T12:00:00Z",
  updated_at: "2026-07-31T14:00:00Z",
  attributes: {
    esco_product_intake: {
      target_profile: {
        controller_family: "CompactLogix",
        catalog_number: "5069-L320ER",
        firmware: "36",
        studio_5000_version: "36"
      }
    }
  },
  counts: {
    files: 4,
    files_parsed: 4,
    tasks: 8,
    tasks_completed: 8,
    findings: 1,
    findings_open: 0,
    approvals_pending: 0,
    reports: 1
  }
};

function event(seq: number, eventType: EventEnvelope["event_type"], payload: Record<string, unknown>): EventEnvelope {
  return {
    id: `10000000-0000-4000-9000-${String(seq).padStart(12, "0")}`,
    schema_version: 1,
    seq,
    event_type: eventType,
    project_id: PROJECT_ID,
    agent_id: null,
    task_id: null,
    actor_user_id: null,
    occurred_at: new Date(Date.parse("2026-07-31T12:00:00Z") + seq * 60_000).toISOString(),
    payload
  };
}

const gates = [
  "project_profile",
  "devices_io_tags",
  "logic_and_delivery",
  "qa_and_risk",
  "import_verification",
  "release"
] as const;

const releaseEvents: EventEnvelope[] = [
  ...gates.map((gate, index) => event(index + 1, "gate.status_changed", {
    gate_key: gate,
    location: gate === "release" ? "release_logistics" : "qa_campus",
    previous_status: "ready",
    status: "passed",
    is_enabled: true,
    unmet_criteria: []
  })),
  event(7, "validation.completed", {
    validation_run_id: "10000000-0000-4000-8000-000000000004",
    revision_number: 8,
    location: "testing_grounds",
    status: "completed",
    summary: "All configured release checks completed.",
    checks_run: 84,
    error_count: 0,
    warning_count: 0,
    is_blocking: false,
    disciplines: ["plc_logic", "hmi", "alarms", "release"]
  }),
  event(8, "export.started", {
    release_candidate_id: CANDIDATE_ID,
    candidate_number: 2,
    revision_number: 8,
    location: "documentation_quarter",
    expected_artifact_count: 1,
    artifact_kinds: ["manifest"]
  }),
  event(9, "export.artifact_created", {
    release_candidate_id: CANDIDATE_ID,
    artifact_id: ARTIFACT_ID,
    kind: "manifest",
    filename: "manifest.json",
    relative_path: "00 Manifest/manifest.json",
    location: "release_logistics",
    validation_level: "round_trip_parsed",
    sha256: "a".repeat(64),
    size_bytes: 2048
  }),
  event(10, "release.candidate_created", {
    release_candidate_id: CANDIDATE_ID,
    candidate_number: 2,
    revision_number: 8,
    location: "release_logistics",
    name: "Line 4 candidate 2",
    status: "approved",
    artifact_count: 1,
    limitation_count: 0,
    validation_level: "round_trip_parsed",
    generator_version: "2.0.0"
  }),
  event(11, "release.ready", {
    release_candidate_id: CANDIDATE_ID,
    candidate_number: 2,
    revision_number: 8,
    location: "release_logistics",
    status: "released",
    validation_level: "rockwell_import_verified",
    import_verification_status: "passed",
    import_verification_detail: "Imported on authorized workstation LAB-01.",
    is_import_ready_candidate_only: false,
    artifact_count: 1,
    package_sha256: "b".repeat(64)
  })
];

const report: ReportRead = {
  id: "10000000-0000-4000-8000-000000000005",
  report_type: "executive_summary",
  export_format: "pdf",
  provider_name: "ESCO reporting",
  title: "Executive Summary",
  status: "generated",
  media_type: "application/pdf",
  size_bytes: 8192,
  sha256: "c".repeat(64),
  finding_ids: [],
  summary: {
    validation_level: "round_trip_parsed",
    report_version: "1.0.0",
    document_status: "DRAFT - GENERATED; NOT REPORT-APPROVED"
  },
  error_message: "",
  download_path: "/api/reports/summary/content",
  created_at: "2026-07-31T14:00:00Z"
};

function liveSource(projectRole = "engineer"): ReleaseSource {
  return {
    dataMode: "live",
    fallbackReason: "",
    project,
    reports: [report],
    events: releaseEvents,
    currentUser: {
      user: {
        id: "10000000-0000-4000-8000-000000000006",
        email: "engineer@example.local",
        display_name: "Test Engineer",
        is_active: true,
        roles: projectRole === "viewer" ? ["reviewer"] : ["engineer"]
      },
      project_roles: { [PROJECT_ID]: projectRole }
    },
    capabilities: [],
    artifactDownloads: {
      [ARTIFACT_ID]: { url: "/downloads/manifest.json", filename: "manifest.json" },
      [report.id]: { url: "/downloads/executive-summary.pdf", filename: "executive-summary.pdf" }
    },
    releaseZipDownload: { url: "/downloads/release.zip", filename: "line-4-release.zip" }
  };
}

const fullSupport = {
  run_validation: true,
  create_release_candidate: true,
  generate_artifacts: true,
  request_import_verification: true,
  request_final_approval: true,
  download_release_zip: true
} as const;

describe("buildReleaseModel", () => {
  it("preserves absent demo facts instead of presenting a complete release", () => {
    const model = buildReleaseModel(createDemoReleaseSource());
    expect(model.demoLabel).toMatch(/SYNTHETIC/);
    expect(model.candidate?.number).toBe(1);
    expect(model.folders).toHaveLength(7);
    expect(model.artifactCoverage).toEqual({ actual: 1, expected: 3, hashed: 0 });
    expect(model.gates.find((gate) => gate.id === "hashes")?.state).toBe("blocked");
    expect(model.actions.every((item) => !item.enabled)).toBe(true);
    expect(model.actions[0].reason).toMatch(/Synthetic demo mode/);
  });

  it("derives a complete event-backed package and real API download support", () => {
    const model = buildReleaseModel(liveSource(), fullSupport);
    expect(model.candidate).toMatchObject({ number: 2, revision: 8, status: "released" });
    expect(model.verification).toMatchObject({ status: "passed", candidateOnly: false });
    expect(model.artifactCoverage).toEqual({ actual: 1, expected: 1, hashed: 1 });
    expect(model.gates.find((gate) => gate.id === "hashes")?.state).toBe("passed");
    expect(model.folders[0].artifacts[0].download?.url).toBe("/downloads/manifest.json");
    const executive = model.reports.find((item) => item.id === "executive_summary");
    expect(executive?.files[0].download?.url).toBe("/downloads/executive-summary.pdf");
    expect(executive?.files[0].provenance.find((item) => item.label === "Report version")?.value).toBe("1.0.0");
    expect(model.actions.every((item) => item.enabled)).toBe(true);
  });

  it("disables engineering controls with an explicit role explanation for a viewer", () => {
    const model = buildReleaseModel(liveSource("viewer"), fullSupport);
    const validation = model.actions.find((item) => item.id === "run_validation");
    const download = model.actions.find((item) => item.id === "download_release_zip");
    expect(validation).toMatchObject({ enabled: false });
    expect(validation?.reason).toMatch(/roles do not permit/);
    expect(download).toMatchObject({ enabled: true });
  });

  it("does not apply an older candidate's verification to a newer candidate", () => {
    const newerCandidateId = "10000000-0000-4000-8000-000000000099";
    const source = liveSource();
    source.events = [...source.events, event(12, "release.candidate_created", {
      release_candidate_id: newerCandidateId,
      candidate_number: 3,
      revision_number: 9,
      location: "release_logistics",
      name: "Line 4 candidate 3",
      status: "generated",
      artifact_count: 0,
      limitation_count: 0,
      validation_level: "generated"
    })];
    const model = buildReleaseModel(source, fullSupport);
    expect(model.candidate).toMatchObject({ id: newerCandidateId, number: 3 });
    expect(model.verification.status).toBe("not_attempted");
    expect(model.releaseOccurredAt).toBeNull();
  });
});
