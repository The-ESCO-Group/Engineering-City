import type {
  AgentInstanceRead,
  EventEnvelope,
  ProjectDetail,
  ProjectRead,
  TaskRead,
  WorkflowStateResponse
} from "./types";

export const DEMO_PROJECT_ID = "02410000-0000-4000-8000-000000000001";

const DEMO_STARTED_AT = "2026-07-31T14:00:00.000Z";
const DEMO_UPDATED_AT = "2026-07-31T15:18:00.000Z";

export const demoProject: ProjectDetail = {
  id: DEMO_PROJECT_ID,
  name: "Line 4 Packaging Conveyor Upgrade",
  description: "Synthetic, offline ESCO workflow demonstration.",
  client_reference: "PRJ-0241",
  status: "in_progress",
  is_paused: false,
  event_seq: 40,
  created_at: DEMO_STARTED_AT,
  updated_at: DEMO_UPDATED_AT,
  attributes: {
    data_classification: "synthetic_demo",
    plant: "Demonstration facility",
    target_profile: "Demo profile — no controller output"
  },
  counts: {
    files: 9,
    files_parsed: 9,
    tasks: 12,
    tasks_completed: 5,
    findings: 4,
    findings_open: 3,
    approvals_pending: 1,
    reports: 0
  }
};

export const demoProjects: ProjectRead[] = [demoProject];

const agent = (
  idSuffix: string,
  kind: string,
  displayName: string,
  status: AgentInstanceRead["status"],
  location: string,
  activity: string,
  taskSuffix: string
): AgentInstanceRead => ({
  id: `02410000-0000-4000-8100-${idSuffix}`,
  project_id: DEMO_PROJECT_ID,
  definition_id: `02410000-0000-4000-8050-${idSuffix}`,
  kind,
  display_name: displayName,
  status,
  location,
  current_activity: activity,
  current_task_id: `02410000-0000-4000-8200-${taskSuffix}`,
  updated_at: DEMO_UPDATED_AT
});

export const demoAgentInstances: AgentInstanceRead[] = [
  agent("000000000001", "requirements_planning", "Requirements Agent", "waiting", "requirements_hall", "Clarification resolved", "000000000001"),
  agent("000000000002", "interlock_review", "Electrical Agent", "working", "electrical_works", "Reconciling the I/O schedule", "000000000002"),
  agent("000000000003", "plc_logic", "PLC Logic Agent", "working", "ladder_logic_lab", "Building an evidence-linked logic proposal", "000000000003"),
  agent("000000000004", "tag_architecture", "HMI Agent", "working", "hmi_district", "Checking template bindings", "000000000004"),
  agent("000000000005", "senior_review", "QA Review Agent", "blocked", "qa_campus", "Awaiting a named human decision", "000000000005")
];

const task = (
  suffix: string,
  kind: TaskRead["kind"],
  title: string,
  status: string,
  agentKind: string | null,
  agentInstanceSuffix: string | null
): TaskRead => ({
  id: `02410000-0000-4000-8200-${suffix}`,
  project_id: DEMO_PROJECT_ID,
  run_id: "02410000-0000-4000-8300-000000000001",
  kind,
  title,
  description: "Synthetic task routed through the contract-compatible demo adapter.",
  status,
  priority: 100,
  agent_kind: agentKind,
  agent_instance_id: agentInstanceSuffix
    ? `02410000-0000-4000-8100-${agentInstanceSuffix}`
    : null,
  attempts: status === "completed" ? 1 : 0,
  max_attempts: 3,
  started_at: status === "queued" ? null : DEMO_STARTED_AT,
  finished_at: status === "completed" ? DEMO_UPDATED_AT : null,
  error_code: "",
  error_message: "",
  blocked_reason: status === "blocked" ? "Named human approval is required." : "",
  created_at: DEMO_STARTED_AT,
  updated_at: DEMO_UPDATED_AT
});

export const demoTasks: TaskRead[] = [
  task("000000000001", "intake", "Validate project intake", "completed", "requirements_planning", "000000000001"),
  task("000000000002", "review_interlocks", "Reconcile electrical I/O", "running", "interlock_review", "000000000002"),
  task("000000000003", "review_plc", "Build PLC logic proposal", "running", "plc_logic", "000000000003"),
  task("000000000004", "review_interlocks", "Review HMI and alarm bindings", "running", "tag_architecture", "000000000004"),
  task("000000000005", "senior_review", "Review seeded discrepancies", "blocked", "senior_review", "000000000005"),
  task("000000000006", "delivery", "Assemble release evidence", "queued", "documentation_release", null)
];

export const demoWorkflow: WorkflowStateResponse = {
  project_id: DEMO_PROJECT_ID,
  project_status: "in_progress",
  is_paused: false,
  active_run_id: "02410000-0000-4000-8300-000000000001",
  queued: 1,
  running: 3,
  blocked: 1,
  awaiting_approval: 1,
  completed: 5,
  failed: 0,
  cancelled: 0,
  message: "Synthetic demo workflow — no engineering output is released."
};

type DemoEventInput = Pick<EventEnvelope, "event_type" | "payload"> & {
  agentId?: string | null;
  taskId?: string | null;
};

const agentStatusEvent = (
  agentIndex: number,
  status: string,
  currentActivity: string,
  taskIndex: number | null
): DemoEventInput => {
  const instance = demoAgentInstances[agentIndex];
  const taskItem = taskIndex === null ? null : demoTasks[taskIndex];
  return {
    event_type: "agent.status_changed",
    agentId: instance.id,
    taskId: taskItem?.id ?? null,
    payload: {
      agent_id: instance.id,
      agent_kind: instance.kind,
      display_name: instance.display_name,
      status,
      current_activity: currentActivity,
      task_id: taskItem?.id ?? null
    }
  };
};

const taskAssignedEvent = (
  agentIndex: number,
  taskIndex: number,
  location: string
): DemoEventInput => {
  const instance = demoAgentInstances[agentIndex];
  const taskItem = demoTasks[taskIndex];
  return {
    event_type: "task.assigned",
    agentId: instance.id,
    taskId: taskItem.id,
    payload: {
      agent_id: instance.id,
      agent_kind: instance.kind,
      attempt: 1,
      kind: taskItem.kind,
      location,
      task_id: taskItem.id,
      title: taskItem.title
    }
  };
};

const agentLocationEvent = (
  agentIndex: number,
  taskIndex: number,
  previousLocation: string,
  location: string
): DemoEventInput => {
  const instance = demoAgentInstances[agentIndex];
  return {
    event_type: "agent.location_changed",
    agentId: instance.id,
    taskId: demoTasks[taskIndex].id,
    payload: {
      agent_id: instance.id,
      agent_kind: instance.kind,
      previous_location: previousLocation,
      location
    }
  };
};

const taskStateEvent = (
  taskIndex: number,
  eventType: "task.started" | "task.completed" | "task.blocked",
  status: string,
  detail?: string
): DemoEventInput => {
  const taskItem = demoTasks[taskIndex];
  const instance = demoAgentInstances[taskIndex];
  return {
    event_type: eventType,
    agentId: instance.id,
    taskId: taskItem.id,
    payload: {
      task_id: taskItem.id,
      kind: taskItem.kind,
      title: taskItem.title,
      status,
      agent_kind: instance.kind,
      attempts: 1,
      ...(detail ? { detail } : {})
    }
  };
};

const DEMO_CHANGESET_ID = "02410000-0000-4000-8800-000000000001";
const DEMO_VALIDATION_ID = "02410000-0000-4000-8900-000000000001";
const DEMO_RELEASE_ID = "02410000-0000-4000-8a00-000000000001";

const demoEventInputs: DemoEventInput[] = [
  { event_type: "project.created", payload: { project_id: DEMO_PROJECT_ID, name: demoProject.name, status: "intake" } },
  { event_type: "file.uploaded", payload: { file_id: "02410000-0000-4000-8400-000000000001", original_filename: "synthetic-project-package.zip", file_kind: "zip", document_role: "other", status: "uploaded" } },
  { event_type: "workflow.started", payload: { run_id: "02410000-0000-4000-8300-000000000001", task_count: 12, reason: "Synthetic demo started" } },
  agentStatusEvent(0, "working", "Resolving the intake clarification", 0),
  taskAssignedEvent(0, 0, "requirements_hall"),
  agentLocationEvent(0, 0, "project_intake", "requirements_hall"),
  taskStateEvent(0, "task.started", "running"),
  taskStateEvent(0, "task.completed", "completed"),
  agentStatusEvent(0, "waiting", "Clarification resolved", null),
  agentStatusEvent(1, "working", "Reconciling the I/O schedule", 1),
  taskAssignedEvent(1, 1, "electrical_works"),
  agentLocationEvent(1, 1, "project_core", "electrical_works"),
  taskStateEvent(1, "task.started", "running"),
  agentStatusEvent(1, "working", "Reconciling the I/O schedule", 1),
  agentStatusEvent(2, "working", "Building an evidence-linked logic proposal", 2),
  taskAssignedEvent(2, 2, "ladder_logic_lab"),
  agentLocationEvent(2, 2, "automation_core", "ladder_logic_lab"),
  taskStateEvent(2, "task.started", "running"),
  agentStatusEvent(2, "working", "Building an evidence-linked logic proposal", 2),
  agentStatusEvent(3, "working", "Checking template and alarm bindings", 3),
  taskAssignedEvent(3, 3, "hmi_district"),
  agentLocationEvent(3, 3, "tag_foundry", "hmi_district"),
  taskStateEvent(3, "task.started", "running"),
  agentStatusEvent(3, "working", "Checking template and alarm bindings", 3),
  agentStatusEvent(4, "reviewing", "Reviewing cross-discipline discrepancies", 4),
  taskAssignedEvent(4, 4, "qa_campus"),
  agentLocationEvent(4, 4, "testing_grounds", "qa_campus"),
  taskStateEvent(4, "task.started", "running"),
  { event_type: "finding.created", agentId: demoAgentInstances[4].id, taskId: demoTasks[4].id, payload: { finding_id: "02410000-0000-4000-8500-000000000001", category: "safety_interlock", confidence: 0.94, requires_approval: true, severity: "critical", status: "open", title: "Guard reset requires stopped-state confirmation", created_by_agent_kind: "senior_review", evidence_count: 2 } },
  { event_type: "approval.requested", agentId: demoAgentInstances[4].id, taskId: demoTasks[4].id, payload: { approval_id: "02410000-0000-4000-8600-000000000001", finding_id: "02410000-0000-4000-8500-000000000001", kind: "finding_disposition", status: "pending", title: "Review guard-reset discrepancy" } },
  taskStateEvent(4, "task.blocked", "awaiting_approval", "Named human approval is required."),
  agentStatusEvent(4, "awaiting_approval", "Awaiting a named human decision", 4),
  agentLocationEvent(4, 4, "qa_campus", "approval_plaza"),
  {
    event_type: "changeset.created",
    agentId: demoAgentInstances[2].id,
    taskId: demoTasks[2].id,
    payload: {
      changeset_id: DEMO_CHANGESET_ID,
      base_revision_number: 3,
      location: "automation_core",
      status: "draft",
      title: "Line 4 logic proposal",
      operation_count: 18,
      confidence: 0.86,
      assumption_count: 2,
      warning_count: 1
    }
  },
  {
    event_type: "changeset.submitted",
    agentId: demoAgentInstances[2].id,
    taskId: demoTasks[2].id,
    payload: {
      changeset_id: DEMO_CHANGESET_ID,
      base_revision_number: 3,
      location: "qa_campus",
      status: "submitted",
      title: "Line 4 logic proposal",
      operation_count: 18,
      warning_count: 1
    }
  },
  {
    event_type: "validation.started",
    taskId: demoTasks[4].id,
    payload: {
      validation_run_id: DEMO_VALIDATION_ID,
      revision_number: 4,
      disciplines: ["plc_logic", "interlocks", "hmi"],
      location: "testing_grounds",
      triggered_by_task_id: demoTasks[4].id
    }
  },
  {
    event_type: "validation.completed",
    taskId: demoTasks[4].id,
    payload: {
      validation_run_id: DEMO_VALIDATION_ID,
      revision_number: 4,
      location: "testing_grounds",
      status: "completed",
      summary: "Synthetic checks completed with one human review item.",
      checks_run: 42,
      error_count: 0,
      warning_count: 1,
      human_review_count: 1,
      is_blocking: false
    }
  },
  {
    event_type: "export.started",
    taskId: demoTasks[5].id,
    payload: {
      release_candidate_id: DEMO_RELEASE_ID,
      candidate_number: 1,
      revision_number: 4,
      location: "documentation_quarter",
      expected_artifact_count: 3,
      artifact_kinds: ["manifest", "validation_summary", "report"]
    }
  },
  {
    event_type: "export.artifact_created",
    taskId: demoTasks[5].id,
    payload: {
      release_candidate_id: DEMO_RELEASE_ID,
      artifact_id: "02410000-0000-4000-8b00-000000000001",
      kind: "validation_summary",
      filename: "validation-summary.pdf",
      relative_path: "reports/validation-summary.pdf",
      location: "documentation_quarter",
      validation_level: "round_trip_parsed",
      warning_count: 1
    }
  },
  {
    event_type: "release.candidate_created",
    taskId: demoTasks[5].id,
    payload: {
      release_candidate_id: DEMO_RELEASE_ID,
      candidate_number: 1,
      revision_number: 4,
      location: "release_logistics",
      name: "Line 4 candidate 1",
      status: "awaiting_approval",
      artifact_count: 3,
      limitation_count: 1,
      validation_level: "round_trip_parsed",
      validation_run_id: DEMO_VALIDATION_ID
    }
  }
];

export const demoEvents: EventEnvelope[] = demoEventInputs.map((event, index) => ({
  id: `02410000-0000-4000-8700-${String(index + 1).padStart(12, "0")}`,
  schema_version: 1,
  seq: index + 1,
  event_type: event.event_type,
  project_id: DEMO_PROJECT_ID,
  agent_id: event.agentId || null,
  task_id: event.taskId || null,
  actor_user_id: null,
  occurred_at: new Date(Date.parse(DEMO_STARTED_AT) + index * 120_000).toISOString(),
  payload: event.payload
}));
