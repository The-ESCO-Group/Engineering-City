import type {
  WorkspaceEvidence,
  WorkspaceModel
} from "./types";

const requirementEvidence: WorkspaceEvidence = {
  id: "demo-ev-req-001",
  kind: "requirement",
  evidenceKind: "direct",
  role: "requirement",
  sourceLabel: "Synthetic Functional Description.docx",
  exactLocation: "Heading 4.3 · Conveyor permissives · Paragraph 2",
  excerpt: "The conveyor may run only when all guard circuits are healthy and the downstream zone is available.",
  expectation: "Every run command is dominated by the guard and downstream permissives.",
  note: "Synthetic excerpt created for offline demonstration."
};

const rungEvidence: WorkspaceEvidence = {
  id: "demo-ev-l5x-014",
  kind: "l5x",
  evidenceKind: "direct",
  role: "implementation",
  sourceLabel: "Synthetic MainProgram.L5X",
  exactLocation: "Program MainProgram · Routine Safety · Rung 14",
  excerpt: "XIC(GuardReset) OTL(GuardSafe) with AxisRunning proof on only one parallel branch.",
  expectation: "The stopped-state proof belongs in the common reset path.",
  note: "Rendered from a synthetic, non-deployable demonstration fixture."
};

const ioEvidence: WorkspaceEvidence = {
  id: "demo-ev-xlsx-042",
  kind: "xlsx",
  evidenceKind: "direct",
  role: "requirement",
  sourceLabel: "Synthetic IO_Schedule.xlsx",
  exactLocation: "Sheet Digital Inputs · Cell F42",
  excerpt: "PE-402 → Local:3:I.Data.7",
  expectation: "The device schedule and controller alias resolve to the same channel.",
  note: "Synthetic workbook cell for offline demonstration."
};

const tagEvidence: WorkspaceEvidence = {
  id: "demo-ev-l5x-tag-402",
  kind: "l5x",
  evidenceKind: "direct",
  role: "implementation",
  sourceLabel: "Synthetic MainProgram.L5X",
  exactLocation: "Controller Tags · PE_402 · AliasFor",
  excerpt: "AliasFor=Local:3:I.Data.6",
  expectation: "Resolve the field assignment before changing either source.",
  note: "Synthetic controller tag fixture."
};

const hmiEvidence: WorkspaceEvidence = {
  id: "demo-ev-xml-018",
  kind: "xml",
  evidenceKind: "direct",
  role: "implementation",
  sourceLabel: "Synthetic Alarms.xml",
  exactLocation: "/Alarms/Alarm[18]/TriggerTag",
  excerpt: "Conveyor_4.JamAlarm",
  expectation: "Every alarm trigger resolves to an approved controller tag.",
  note: "Synthetic XML node for offline demonstration."
};

export function createDemoWorkspaceModel(): WorkspaceModel {
  return {
    mode: "demo",
    demoLabel: "DEMO DATA · SYNTHETIC OFFLINE ENGINEERING RECORDS",
    unavailableReason: "",
    project: {
      id: "demo-project-line-4",
      reference: "DEMO-0241",
      name: "Line 4 Packaging Conveyor Upgrade",
      description: "Synthetic offline project used to demonstrate traceable engineering review.",
      status: "Engineering review",
      revision: 7,
      updatedAt: "2026-07-31T17:42:00Z"
    },
    capabilities: {
      intakeSubmission: { level: "available", reason: "Demo drafts remain on this device and do not create a live project." },
      projectCore: { level: "available", reason: "Synthetic Project Core objects are available for offline demonstration." },
      deliveryCore: { level: "available", reason: "Synthetic HMI, alarm, test, and documentation records are available for offline demonstration." },
      changesetOperations: { level: "available", reason: "Synthetic operation-level diffs are available for offline demonstration." },
      revisionComparison: { level: "available", reason: "Synthetic revision metadata is available for offline demonstration." },
      findingEvidence: { level: "available", reason: "Synthetic citations are embedded in the offline demonstration fixture." },
      approvalDecisions: { level: "available", reason: "Any decision recorded here is demonstration data only." }
    },
    files: [
      {
        id: "demo-file-l5x",
        name: "Synthetic MainProgram.L5X",
        kind: "l5x",
        role: "control_logic",
        sizeBytes: 842_114,
        sha256: "demo-not-a-real-artifact-hash",
        status: "parsed",
        detail: "Synthetic fixture parsed by the offline demo adapter.",
        archiveMemberPath: "",
        updatedAt: "2026-07-31T16:08:00Z"
      },
      {
        id: "demo-file-io",
        name: "Synthetic IO_Schedule.xlsx",
        kind: "xlsx",
        role: "io_list",
        sizeBytes: 126_820,
        sha256: "demo-not-a-real-artifact-hash",
        status: "parsed",
        detail: "Synthetic fixture; no customer data.",
        archiveMemberPath: "",
        updatedAt: "2026-07-31T16:09:00Z"
      },
      {
        id: "demo-file-fd",
        name: "Synthetic Functional Description.docx",
        kind: "docx",
        role: "functional_description",
        sizeBytes: 214_408,
        sha256: "demo-not-a-real-artifact-hash",
        status: "parsed",
        detail: "Synthetic fixture; no customer data.",
        archiveMemberPath: "",
        updatedAt: "2026-07-31T16:10:00Z"
      },
      {
        id: "demo-file-alarm",
        name: "Synthetic Alarms.xml",
        kind: "xml",
        role: "alarm_list",
        sizeBytes: 38_304,
        sha256: "demo-not-a-real-artifact-hash",
        status: "parsed",
        detail: "Synthetic fixture; FactoryTalk runtime validation is not claimed.",
        archiveMemberPath: "",
        updatedAt: "2026-07-31T16:11:00Z"
      }
    ],
    tasks: [
      {
        id: "demo-task-intake",
        title: "Normalize uploaded project files",
        description: "Index synthetic sources and retain exact citations.",
        kind: "project_intake",
        state: "completed",
        priority: 80,
        agent: "project_manager",
        agentId: "demo-agent-manager",
        progress: 100,
        blockedReason: "",
        error: "",
        attempts: 1,
        maxAttempts: 3,
        startedAt: "2026-07-31T16:02:00Z",
        finishedAt: "2026-07-31T16:12:00Z",
        updatedAt: "2026-07-31T16:12:00Z",
        semanticLocation: "project_intake"
      },
      {
        id: "demo-task-tags",
        title: "Normalize controller tags and aliases",
        description: "Compare the synthetic controller export with the device schedule.",
        kind: "tag_engineering",
        state: "running",
        priority: 70,
        agent: "tag_engineer",
        agentId: "demo-agent-tags",
        progress: 72,
        blockedReason: "",
        error: "",
        attempts: 1,
        maxAttempts: 3,
        startedAt: "2026-07-31T16:18:00Z",
        finishedAt: null,
        updatedAt: "2026-07-31T17:40:00Z",
        semanticLocation: "tag_foundry"
      },
      {
        id: "demo-task-electrical",
        title: "Reconcile device and I/O assignments",
        description: "Compare synthetic I/O schedule assignments with controller aliases.",
        kind: "electrical_io",
        state: "running",
        priority: 75,
        agent: "electrical_io",
        agentId: "demo-agent-electrical",
        progress: 61,
        blockedReason: "",
        error: "",
        attempts: 1,
        maxAttempts: 3,
        startedAt: "2026-07-31T16:20:00Z",
        finishedAt: null,
        updatedAt: "2026-07-31T17:38:00Z",
        semanticLocation: "electrical_works"
      },
      {
        id: "demo-task-plc",
        title: "Review conveyor permissives and interlocks",
        description: "Trace guard, stopped-state, and downstream availability requirements.",
        kind: "plc_logic",
        state: "blocked",
        priority: 95,
        agent: "logic_engineer",
        agentId: "demo-agent-plc",
        progress: 46,
        blockedReason: "Human disposition is required for DEMO-F-104.",
        error: "",
        attempts: 1,
        maxAttempts: 2,
        startedAt: "2026-07-31T16:28:00Z",
        finishedAt: null,
        updatedAt: "2026-07-31T17:31:00Z",
        semanticLocation: "ladder_logic_lab"
      },
      {
        id: "demo-task-hmi",
        title: "Validate HMI and alarm bindings",
        description: "Resolve synthetic display and alarm references against approved tags.",
        kind: "hmi_engineering",
        state: "awaiting_approval",
        priority: 60,
        agent: "hmi_engineer",
        agentId: "demo-agent-hmi",
        progress: 88,
        blockedReason: "Awaiting tag rename review.",
        error: "",
        attempts: 1,
        maxAttempts: 3,
        startedAt: "2026-07-31T16:44:00Z",
        finishedAt: null,
        updatedAt: "2026-07-31T17:36:00Z",
        semanticLocation: "hmi_district"
      },
      {
        id: "demo-task-tests",
        title: "Create IAT and FAT procedures",
        description: "Starts after required design decisions are recorded.",
        kind: "testing",
        state: "queued",
        priority: 50,
        agent: null,
        agentId: null,
        progress: 0,
        blockedReason: "",
        error: "",
        attempts: 0,
        maxAttempts: 3,
        startedAt: null,
        finishedAt: null,
        updatedAt: "2026-07-31T17:12:00Z",
        semanticLocation: "testing_grounds"
      }
    ],
    requirements: [
      {
        id: "demo-req-001",
        reference: "SRS-4.3.2",
        title: "Guarded conveyor reset",
        text: "Reset shall be permitted only while motion is stopped and all guard circuits are healthy.",
        source: "Synthetic Functional Description.docx · Heading 4.3",
        kind: "safety",
        priority: "mandatory",
        status: "accepted",
        acceptanceCriteria: [
          "Reset is rejected while AxisRunning is true.",
          "A broken guard circuit prevents the run permissive."
        ],
        acceptanceCriteriaCount: 2,
        implementationCoverage: 50,
        testCoverage: 0,
        openQuestions: ["DEMO-Q-007: Confirm required reset behavior after guard restoration."],
        inferred: false,
        confidence: 1
      },
      {
        id: "demo-req-002",
        reference: "URS-2.8",
        title: "Downstream zone permissive",
        text: "Line 4 shall stop feeding when the downstream accumulation zone is unavailable.",
        source: "Synthetic Functional Description.docx · Heading 2.8",
        kind: "functional",
        priority: "mandatory",
        status: "implemented",
        acceptanceCriteria: ["Loss of ZoneAvailable removes the conveyor run command."],
        acceptanceCriteriaCount: 1,
        implementationCoverage: 100,
        testCoverage: 100,
        openQuestions: [],
        inferred: false,
        confidence: 1
      },
      {
        id: "demo-req-003",
        reference: "ESCO-ALM-12",
        title: "Jam alarm operator response",
        text: "Jam alarms shall identify the affected zone and provide a safe operator response.",
        source: "Synthetic ESCO Alarm Philosophy · Rule 12",
        kind: "operational",
        priority: "expected",
        status: "proposed",
        acceptanceCriteria: ["Alarm message identifies Line 4 and zone.", "Response text directs the operator to verify motion is stopped."],
        acceptanceCriteriaCount: 2,
        implementationCoverage: 50,
        testCoverage: 0,
        openQuestions: [],
        inferred: true,
        confidence: 0.82
      }
    ],
    devices: [
      {
        id: "demo-device-pe402",
        area: "Packaging",
        unit: "Line 4 Infeed",
        deviceId: "PE-402",
        type: "Photoelectric sensor",
        vendorModel: "Synthetic vendor/model",
        ioAssignments: ["Schedule: Local:3:I.Data.7", "PLC alias: Local:3:I.Data.6"],
        networkNode: "Local chassis · Slot 3",
        failState: "False / beam clear — requires field confirmation",
        linkedTags: ["PE_402", "Line4_Infeed_Jam"],
        linkedLogic: ["MainProgram/Diagnostics/Rung 22"],
        coverage: { hmi: "Linked", alarms: "Linked", tests: "Missing" },
        validation: "Conflict · human verification required"
      },
      {
        id: "demo-device-m402",
        area: "Packaging",
        unit: "Line 4 Conveyor",
        deviceId: "MTR-402",
        type: "Conveyor motor",
        vendorModel: "Synthetic 2 HP starter",
        ioAssignments: ["Run: Local:4:O.Data.2", "Running: Local:3:I.Data.10"],
        networkNode: "Local chassis",
        failState: "De-energized",
        linkedTags: ["MTR_402_Cmd", "MTR_402_Running"],
        linkedLogic: ["MainProgram/Conveyors/Rung 8"],
        coverage: { hmi: "Linked", alarms: "Linked", tests: "Planned" },
        validation: "Internally consistent"
      }
    ],
    tags: [
      {
        id: "demo-tag-pe402",
        name: "PE_402",
        scope: "Controller",
        dataType: "BOOL",
        role: "Input alias",
        description: "Line 4 infeed photoeye",
        units: "",
        alias: "Local:3:I.Data.6",
        device: "PE-402",
        status: "Proposed correction",
        validation: "I/O assignment conflict",
        references: ["Diagnostics/Rung 22", "Line4_Overview/PE402", "Alarm DEMO-A-018"],
        approvedRevision: 6
      },
      {
        id: "demo-tag-jam",
        name: "Line4_Infeed_Jam",
        scope: "Controller",
        dataType: "BOOL",
        role: "Derived alarm trigger",
        description: "Line 4 infeed jam state",
        units: "",
        alias: "",
        device: "PE-402",
        status: "Accepted",
        validation: "Driven and referenced",
        references: ["Diagnostics/Rung 24", "Alarm DEMO-A-018", "FAT-DEMO-14"],
        approvedRevision: 7
      },
      {
        id: "demo-tag-running",
        name: "MTR_402_Running",
        scope: "Controller",
        dataType: "BOOL",
        role: "Input alias",
        description: "Line 4 conveyor motor running feedback",
        units: "",
        alias: "Local:3:I.Data.10",
        device: "MTR-402",
        status: "Accepted",
        validation: "Internally consistent",
        references: ["Conveyors/Rung 8", "Safety/Rung 14"],
        approvedRevision: 6
      },
      {
        id: "demo-tag-guard",
        name: "Line4_GuardHealthy",
        scope: "MainProgram",
        dataType: "BOOL",
        role: "Permissive",
        description: "Aggregate synthetic guard status; not a safety certification",
        units: "",
        alias: "",
        device: "Guard circuit",
        status: "Awaiting approval",
        validation: "Restricted-domain review required",
        references: ["Safety/Rung 14", "Line4_Overview/GuardStatus"],
        approvedRevision: null
      }
    ],
    plcPrograms: [
      {
        id: "demo-program-main",
        controller: "Synthetic CompactLogix profile",
        task: "MainTask · 20 ms periodic",
        name: "MainProgram",
        validation: "Structured preview only · no Studio 5000 import verification",
        generatedPreviewAvailable: false,
        routines: [
          {
            id: "demo-routine-safety",
            name: "Safety",
            type: "RLL",
            templateOrigin: "Synthetic imported source · not generated",
            rungs: [
              {
                id: "demo-rung-14",
                number: 14,
                comment: "Reset aggregate guard state after permitted conditions are proven.",
                instructions: [
                  { mnemonic: "XIC", operands: ["GuardReset"], description: "Reset request is active." },
                  { mnemonic: "XIO", operands: ["AxisRunning"], description: "Motion stopped proof; currently bypassed on one branch." },
                  { mnemonic: "OTL", operands: ["GuardSafe"], description: "Latch aggregate guard state." }
                ],
                requirementLinks: ["SRS-4.3.2"],
                evidenceLinks: ["demo-ev-l5x-014"],
                warnings: ["Parallel branch may bypass the stopped-state proof. Human review required."]
              },
              {
                id: "demo-rung-18",
                number: 18,
                comment: "Apply downstream zone permissive to the conveyor command.",
                instructions: [
                  { mnemonic: "XIC", operands: ["ZoneAvailable"], description: "Downstream zone is available." },
                  { mnemonic: "OTE", operands: ["Line4_RunPermissive"], description: "Permit Line 4 run request." }
                ],
                requirementLinks: ["URS-2.8"],
                evidenceLinks: ["demo-ev-req-001"],
                warnings: []
              }
            ]
          }
        ]
      }
    ],
    hmiScreens: [
      {
        id: "demo-hmi-overview",
        name: "Line4_Overview",
        displayType: "Process overview",
        template: "Synthetic ESCO ME overview template · v3.2",
        resolution: "1280 × 800",
        navigationFrom: ["Plant_Overview"],
        navigationTo: ["Line4_Diagnostics", "Line4_Alarms"],
        bindings: [
          {
            id: "demo-binding-run",
            object: "Conveyor status banner",
            property: "State indicator",
            tag: "Line4_MTR402_RunFb",
            status: "Valid",
            evidence: "Synthetic HMI.xml · Display Line4_Overview · Object 18"
          },
          {
            id: "demo-binding-jam",
            object: "Jam alarm banner",
            property: "Visibility",
            tag: "Conveyor_4.JamAlarm",
            status: "Unresolved",
            evidence: "Synthetic HMI.xml · Display Line4_Overview · Object 27"
          }
        ],
        validation: "1 unresolved binding · target import verification not performed",
        approvalState: "Awaiting tag rename review",
        source: "Synthetic HMI.xml · /Displays/Display[@name='Line4_Overview']"
      },
      {
        id: "demo-hmi-diagnostics",
        name: "Line4_Diagnostics",
        displayType: "Maintenance detail",
        template: "Synthetic ESCO ME diagnostics template · v2.1",
        resolution: "1280 × 800",
        navigationFrom: ["Line4_Overview"],
        navigationTo: ["Line4_Overview"],
        bindings: [
          {
            id: "demo-binding-photoeye",
            object: "Photoeye PE-402 diagnostic",
            property: "Value",
            tag: "Line4_PE402",
            status: "Valid",
            evidence: "Synthetic HMI.xml · Display Line4_Diagnostics · Object 11"
          }
        ],
        validation: "Structured references valid",
        approvalState: "Proposed",
        source: "Synthetic HMI.xml · /Displays/Display[@name='Line4_Diagnostics']"
      }
    ],
    alarms: [
      {
        id: "demo-alarm-jam",
        alarmId: "L4-JAM-402",
        message: "Line 4 conveyor jam at PE-402",
        trigger: "Conveyor_4.JamAlarm",
        severity: "High",
        acknowledgement: "Required",
        delay: "2 s",
        deadband: "Not applicable",
        operatorResponse: "Stop feed, verify the guarded zone is safe, then inspect PE-402. Follow the approved lockout procedure before entry.",
        linkedScreen: "Line4_Overview",
        linkedDevice: "PE-402",
        linkedTests: ["DEMO-FAT-014"],
        validation: "Trigger tag unresolved · human review required",
        source: "Synthetic Alarms.xml · /Alarms/Alarm[18]"
      },
      {
        id: "demo-alarm-downstream",
        alarmId: "L4-DOWNSTREAM-UNAVAILABLE",
        message: "Line 4 downstream accumulation unavailable",
        trigger: "Line4_ZoneAvailable = 0",
        severity: "Medium",
        acknowledgement: "Required",
        delay: "5 s",
        deadband: "Not applicable",
        operatorResponse: "Check downstream accumulation status and restore availability before restarting Line 4.",
        linkedScreen: "Line4_Overview",
        linkedDevice: "ZONE-405",
        linkedTests: ["DEMO-IAT-008"],
        validation: "Structured references valid",
        source: "Synthetic Alarms.xml · /Alarms/Alarm[21]"
      }
    ],
    testProcedures: [
      {
        id: "demo-test-fat-reset",
        testId: "DEMO-FAT-014",
        title: "Guarded conveyor reset permissive",
        testType: "FAT · automatic-mode interlock",
        status: "Blocked by clarification",
        prerequisites: ["Approved synthetic revision", "Guard circuit simulator available", "Named test witness assigned"],
        initialConditions: ["Line 4 stopped", "Guard circuit healthy", "Downstream zone available"],
        safetyPrecautions: ["No physical equipment operation is authorized by this procedure.", "Use the approved risk assessment and lockout procedure for any field work."],
        steps: [
          { sequence: 1, action: "Simulate AxisRunning = true and issue the reset request.", expectedResult: "Reset is rejected and GuardSafe remains false.", evidenceRequired: "Timestamped FAT result and controller test trace" },
          { sequence: 2, action: "Set AxisRunning = false with every guard input healthy, then issue reset.", expectedResult: "Reset is accepted and the run permissive may be evaluated.", evidenceRequired: "Witness initials and result capture" }
        ],
        requirementLinks: ["SRS-4.3.2"],
        findingLinks: ["DEMO-F-104"],
        approvalState: "Restricted-domain approval required"
      },
      {
        id: "demo-test-iat-downstream",
        testId: "DEMO-IAT-008",
        title: "Downstream availability response",
        testType: "IAT · permissive loss",
        status: "Ready for review",
        prerequisites: ["Approved I/O checkout", "Downstream interface simulator connected"],
        initialConditions: ["Line 4 running in the approved test environment", "ZoneAvailable true"],
        safetyPrecautions: ["Execute only under the site commissioning plan and named human supervision."],
        steps: [
          { sequence: 1, action: "Remove the simulated downstream ZoneAvailable signal.", expectedResult: "The Line 4 run permissive drops and the unavailable alarm enters its configured delay.", evidenceRequired: "IAT result, alarm timestamp, and witness initials" }
        ],
        requirementLinks: ["URS-2.8"],
        findingLinks: [],
        approvalState: "Reviewer unassigned"
      }
    ],
    documents: [
      {
        id: "demo-doc-discrepancy",
        title: "Detailed engineering discrepancy report",
        documentType: "Detailed report",
        format: "HTML / PDF / DOCX",
        status: "Generated from synthetic snapshot",
        revision: 7,
        evidenceCount: 5,
        approvalState: "Draft · not engineering approval",
        generatedAt: "2026-07-31T17:42:00Z",
        source: "Documentation and Release Agent · synthetic snapshot"
      },
      {
        id: "demo-doc-fat",
        title: "Line 4 FAT plan",
        documentType: "Test plan",
        format: "DOCX / XLSX",
        status: "Blocked by open safety clarification",
        revision: 7,
        evidenceCount: 3,
        approvalState: "Human review required",
        generatedAt: null,
        source: "Testing and Commissioning Agent · synthetic proposal"
      },
      {
        id: "demo-doc-audit",
        title: "Project audit export",
        documentType: "Audit",
        format: "JSON / CSV",
        status: "Current through synthetic event 40",
        revision: 7,
        evidenceCount: null,
        approvalState: "Read-only record",
        generatedAt: "2026-07-31T17:42:00Z",
        source: "Synthetic ordered event history"
      }
    ],
    plans: [
      {
        id: "demo-plan-01",
        runId: "demo-run-01",
        summary: "Normalize inputs, reconcile I/O and tags, review PLC/HMI/alarm coverage, then hold release work behind QA and named approvals.",
        taskCount: 11,
        taskCountsByKind: {
          requirements: 1,
          electrical_io: 1,
          tag_engineering: 1,
          plc_logic: 2,
          hmi_engineering: 1,
          alarm_engineering: 1,
          qa_review: 2,
          testing: 1,
          documentation: 1
        },
        riskCount: 2,
        outOfScopeCount: 1,
        blockers: ["DEMO-Q-007"],
        location: "requirements_hall",
        occurredAt: "2026-07-31T16:16:00Z"
      }
    ],
    changesets: [
      {
        id: "demo-cs-tag-rename",
        title: "Normalize Line 4 jam alarm tag",
        status: "in_review",
        baseRevision: 6,
        resultingRevision: null,
        author: "Synthetic Tag Engineering Agent",
        semanticLocation: "tag_foundry",
        operationCount: 1,
        warningCount: 1,
        assumptionCount: 0,
        confidence: 0.88,
        restrictedDomains: [],
        requiresHumanApproval: true,
        operationsAvailable: true,
        concerns: ["Confirm the alarm XML import candidate after the rename."],
        validationSummary: "Internal reference scan found five impacted objects; no external runtime import validation was run.",
        createdAt: "2026-07-31T17:02:00Z",
        operations: [
          {
            id: "demo-op-rename-01",
            type: "rename",
            objectType: "tag",
            objectLabel: "Conveyor_4.JamAlarm",
            field: "name",
            before: "Conveyor_4.JamAlarm",
            after: "Line4_Infeed_Jam",
            sourceEvidence: [hmiEvidence],
            templateBasis: "Synthetic ESCO naming rule: <Area>_<Equipment>_<State>",
            rationale: "Align the stale HMI/alarm reference with the normalized Line 4 controller namespace.",
            confidence: 0.88,
            risk: "A missed external reference could leave an alarm or display binding stale.",
            impactedObjects: [
              { type: "PLC reference", label: "Diagnostics/Rung 24", reference: "PLC:MainProgram/Diagnostics/24" },
              { type: "HMI binding", label: "Line4_Overview/JamIndicator", reference: "HMI:Line4_Overview#JamIndicator" },
              { type: "Alarm", label: "DEMO-A-018", reference: "ALARM:DEMO-A-018" },
              { type: "Test", label: "FAT-DEMO-14", reference: "TEST:FAT-DEMO-14" },
              { type: "Documentation", label: "Line 4 operator guide", reference: "DOC:Line4#Alarms" }
            ],
            validationResults: [
              "Controller reference scan: 1 resolved",
              "HMI binding scan: 1 proposed update",
              "Alarm reference scan: 1 proposed update",
              "FactoryTalk runtime validation: not performed"
            ],
            reviewerComments: ["Verify the downstream report template before merge."],
            requiresApproval: true
          }
        ]
      },
      {
        id: "demo-cs-io-402",
        title: "Resolve PE-402 input assignment",
        status: "awaiting_approval",
        baseRevision: 6,
        resultingRevision: null,
        author: "Synthetic Electrical & I/O Agent",
        semanticLocation: "electrical_works",
        operationCount: 1,
        warningCount: 1,
        assumptionCount: 0,
        confidence: 0.74,
        restrictedDomains: ["electrical_power_distribution"],
        requiresHumanApproval: true,
        operationsAvailable: true,
        concerns: ["The source fixtures disagree; field wiring is not known."],
        validationSummary: "Conflict is identified but intentionally unresolved pending human evidence.",
        createdAt: "2026-07-31T17:08:00Z",
        operations: [
          {
            id: "demo-op-io-402",
            type: "assign_io",
            objectType: "io_point",
            objectLabel: "PE-402",
            field: "address",
            before: "Schedule: Local:3:I.Data.7 · PLC: Local:3:I.Data.6",
            after: "No change proposed until field assignment is confirmed",
            sourceEvidence: [ioEvidence, tagEvidence],
            templateBasis: "Conflict policy: never select one source without authoritative evidence",
            rationale: "Escalate the contradiction rather than inventing which address is correct.",
            confidence: 0.74,
            risk: "Choosing the wrong channel could misreport the infeed state.",
            impactedObjects: [
              { type: "PLC alias", label: "PE_402", reference: "TAG:PE_402" },
              { type: "Device", label: "PE-402", reference: "DEVICE:PE-402" },
              { type: "Alarm", label: "DEMO-A-018", reference: "ALARM:DEMO-A-018" }
            ],
            validationResults: ["Duplicate assignment scan: clear", "Cross-source address check: failed"],
            reviewerComments: [],
            requiresApproval: true
          }
        ]
      }
    ],
    revisions: [
      {
        id: "demo-revision-7",
        number: 7,
        parentNumber: 6,
        summary: "Accepted requirement decomposition and normalized non-conflicting controller tags.",
        operationCount: 18,
        objectCounts: { requirement: 3, tag: 14, project_question: 1 },
        changesetId: "demo-cs-requirements",
        createdBy: "Synthetic Project Manager Agent",
        isReleaseBaseline: false,
        occurredAt: "2026-07-31T16:54:00Z"
      },
      {
        id: "demo-revision-6",
        number: 6,
        parentNumber: 5,
        summary: "Synthetic intake baseline after file parsing.",
        operationCount: 0,
        objectCounts: { source_file: 4 },
        changesetId: null,
        createdBy: "Synthetic intake workflow",
        isReleaseBaseline: false,
        occurredAt: "2026-07-31T16:12:00Z"
      }
    ],
    findings: [
      {
        id: "DEMO-F-104",
        title: "Guard reset may bypass stopped-state confirmation",
        category: "interlock_discrepancy",
        severity: "critical",
        status: "open",
        source: rungEvidence.sourceLabel,
        exactLocation: rungEvidence.exactLocation,
        agent: "Synthetic PLC Review Agent",
        confidence: 0.94,
        evidenceCount: 2,
        discipline: "PLC / safety review",
        reviewer: "Demo reviewer",
        approvalStatus: "Decision required",
        requiresApproval: true,
        fact: "The synthetic rung places AxisRunning proof on only one branch feeding the GuardSafe latch.",
        inference: "A parallel reset path may latch GuardSafe without proving motion is stopped.",
        expectedMissingEvidence: "Offline test evidence for reset behavior while AxisRunning is true.",
        parserWarnings: [],
        requirementSide: "SRS-4.3.2 requires stopped-state confirmation in every reset path.",
        implementationSide: "Synthetic Safety Rung 14 shows the confirmation on only one parallel branch.",
        impact: "Unexpected motion could remain permitted after a guard reset if the inference is confirmed.",
        suggestedAction: "Move the stopped-state proof into the common path, then validate against a safe offline fixture.",
        risk: "Safety-related recommendation. A qualified human reviewer must decide; the demo does not certify safety logic.",
        limitations: "Synthetic source only. No controller, emulator, field device, or Studio 5000 runtime was used.",
        comments: ["Demo QA requested an independent review."],
        history: ["17:10 finding created", "17:21 evidence package attached", "17:31 approval requested"],
        linkedChangesetId: "demo-cs-safety-reset",
        linkedTests: ["IAT-DEMO-07", "FAT-DEMO-09"],
        evidence: [requirementEvidence, rungEvidence],
        detailLoaded: true
      },
      {
        id: "DEMO-F-107",
        title: "PE-402 address differs between schedule and controller alias",
        category: "io_mismatch",
        severity: "high",
        status: "open",
        source: ioEvidence.sourceLabel,
        exactLocation: ioEvidence.exactLocation,
        agent: "Synthetic Electrical Review Agent",
        confidence: 0.88,
        evidenceCount: 2,
        discipline: "Electrical / I/O",
        reviewer: "Unassigned",
        approvalStatus: "Pending",
        requiresApproval: true,
        fact: "The synthetic I/O schedule lists bit 7 while the controller alias lists bit 6.",
        inference: "At least one source is stale or incorrect.",
        expectedMissingEvidence: "Approved field wiring or checkout record identifying the physical channel.",
        parserWarnings: [],
        requirementSide: "The device schedule assigns PE-402 to Local:3:I.Data.7.",
        implementationSide: "The controller alias PE_402 resolves to Local:3:I.Data.6.",
        impact: "The wrong input may drive Line 4 jam detection or diagnostics.",
        suggestedAction: "Verify the field terminal, then change the incorrect source under revision control.",
        risk: "Equipment behavior cannot be inferred until field evidence resolves the conflict.",
        limitations: "Synthetic schedule and L5X only; no field wiring was inspected.",
        comments: [],
        history: ["17:08 discrepancy detected"],
        linkedChangesetId: "demo-cs-io-402",
        linkedTests: ["FAT-DEMO-14"],
        evidence: [ioEvidence, tagEvidence],
        detailLoaded: true
      },
      {
        id: "DEMO-F-112",
        title: "Alarm trigger references an unresolved conveyor tag",
        category: "unresolved_reference",
        severity: "medium",
        status: "open",
        source: hmiEvidence.sourceLabel,
        exactLocation: hmiEvidence.exactLocation,
        agent: "Synthetic HMI Review Agent",
        confidence: 0.76,
        evidenceCount: 1,
        discipline: "HMI / alarms",
        reviewer: "Demo reviewer",
        approvalStatus: "Not started",
        requiresApproval: false,
        fact: "The alarm XML uses Conveyor_4.JamAlarm, which does not exist in the normalized synthetic tag set.",
        inference: "The alarm is likely a stale reference after a tag naming change.",
        expectedMissingEvidence: "Target-specific import or runtime validation of the corrected alarm candidate.",
        parserWarnings: ["FactoryTalk runtime semantics were not evaluated."],
        requirementSide: "ESCO-ALM-12 expects a valid trigger and operator response.",
        implementationSide: "The synthetic XML trigger does not resolve to an approved controller tag.",
        impact: "The alarm may not activate.",
        suggestedAction: "Review the structured rename impact and regenerate the candidate from an approved template.",
        risk: "A missed jam alarm can delay operator response.",
        limitations: "Design preview only; runtime validation is not claimed.",
        comments: [],
        history: ["17:02 reference scan completed"],
        linkedChangesetId: "demo-cs-tag-rename",
        linkedTests: ["FAT-DEMO-14"],
        evidence: [hmiEvidence],
        detailLoaded: true
      }
    ],
    approvals: [
      {
        id: "demo-approval-safety",
        projectId: "demo-project-line-4",
        findingId: "DEMO-F-104",
        changesetId: "demo-cs-safety-reset",
        gate: "qa_and_risk",
        kind: "safety_recommendation",
        status: "pending",
        title: "Disposition guard reset finding",
        requestedDecision: "Decide whether the proposed corrective direction may proceed to offline validation.",
        affectedRevision: 7,
        affectedObjects: ["MainProgram/Safety/Rung 14", "Tag GuardSafe", "IAT-DEMO-07"],
        evidence: [requirementEvidence, rungEvidence],
        validationResults: ["Structured reference trace: complete", "Offline behavior test: not run", "Import verification: not requested"],
        rationale: "The evidence shows a plausible bypass path; the agent recommends correction and offline validation, not approval of safety logic.",
        risk: "Critical safety-related recommendation.",
        severity: "critical",
        reviewer: "Qualified demo reviewer",
        dueDate: "2026-08-01",
        comments: [],
        priorDecisions: [],
        downstreamImpact: "PLC changes, IAT/FAT test creation, QA gate, and release remain blocked.",
        restrictedDomain: true,
        finalRelease: false,
        requestedBy: "Synthetic Senior Review Agent",
        requestedAt: "2026-07-31T17:31:00Z",
        allowedDecisions: ["approve", "reject", "request_changes", "accept_risk", "defer"]
      },
      {
        id: "demo-approval-tag",
        projectId: "demo-project-line-4",
        findingId: "DEMO-F-112",
        changesetId: "demo-cs-tag-rename",
        gate: "logic_and_delivery",
        kind: "tag_rename",
        status: "pending",
        title: "Review Line 4 jam tag rename",
        requestedDecision: "Review the five impacted references before the rename is merged.",
        affectedRevision: 7,
        affectedObjects: ["Tag Conveyor_4.JamAlarm", "Alarm DEMO-A-018", "Line4_Overview/JamIndicator"],
        evidence: [hmiEvidence],
        validationResults: ["Reference impact scan: five objects", "Runtime import validation: not performed"],
        rationale: "The normalized name follows the synthetic naming standard, but every consumer must move together.",
        risk: "Medium cross-reference risk.",
        severity: "medium",
        reviewer: "Demo reviewer",
        dueDate: null,
        comments: ["Confirm documentation reference before merge."],
        priorDecisions: [],
        downstreamImpact: "HMI and alarm work remain in review.",
        restrictedDomain: false,
        finalRelease: false,
        requestedBy: "Synthetic Tag Engineering Agent",
        requestedAt: "2026-07-31T17:12:00Z",
        allowedDecisions: ["approve", "reject", "request_changes", "defer"]
      }
    ]
  };
}
