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
const DEMO_UPDATED_AT = "2026-07-31T14:42:00.000Z";

export const demoProject: ProjectDetail = {
  id: DEMO_PROJECT_ID,
  name: "Line 4 Packaging Conveyor Upgrade",
  description: "Synthetic, offline ESCO workflow demonstration.",
  client_reference: "PRJ-0241",
  status: "in_progress",
  is_paused: false,
  event_seq: 18,
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
  kind,
  display_name: displayName,
  status,
  location,
  current_activity: activity,
  current_task_id: `02410000-0000-4000-8200-${taskSuffix}`,
  updated_at: DEMO_UPDATED_AT
});

export const demoAgentInstances: AgentInstanceRead[] = [
  agent("000000000001", "requirements", "Requirements Agent", "waiting", "REQUIREMENTS_HALL", "Clarification resolved", "000000000001"),
  agent("000000000002", "electrical_io", "Electrical Agent", "working", "ELECTRICAL_WORKS", "Reconciling the I/O schedule", "000000000002"),
  agent("000000000003", "plc_logic", "PLC Logic Agent", "working", "LADDER_LOGIC_LAB", "Building an evidence-linked logic proposal", "000000000003"),
  agent("000000000004", "hmi_engineering", "HMI Agent", "working", "HMI_DISTRICT", "Checking template bindings", "000000000004"),
  agent("000000000005", "senior_review", "QA Review Agent", "blocked", "QA_CAMPUS", "Awaiting a named human decision", "000000000005")
];

const task = (
  suffix: string,
  kind: TaskRead["kind"],
  title: string,
  status: string,
  agentKind: string | null
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
  agent_instance_id: null,
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
  task("000000000001", "intake", "Validate project intake", "completed", "requirements"),
  task("000000000002", "review_interlocks", "Reconcile electrical I/O", "running", "electrical_io"),
  task("000000000003", "review_plc", "Build PLC logic proposal", "running", "plc_logic"),
  task("000000000004", "review_interlocks", "Review HMI and alarm bindings", "running", "hmi_engineering"),
  task("000000000005", "senior_review", "Review seeded discrepancies", "blocked", "senior_review"),
  task("000000000006", "delivery", "Assemble release evidence", "queued", "documentation_release")
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

const demoEventInputs: DemoEventInput[] = [
  { event_type: "project.created", payload: { project_id: DEMO_PROJECT_ID, name: demoProject.name, status: "intake" } },
  { event_type: "file.uploaded", payload: { file_id: "02410000-0000-4000-8400-000000000001", original_filename: "synthetic-project-package.zip", file_kind: "zip", document_role: "other", status: "uploaded" } },
  { event_type: "workflow.started", payload: { run_id: "02410000-0000-4000-8300-000000000001", task_count: 12, reason: "Synthetic demo started" } },
  { event_type: "agent.location_changed", agentId: demoAgentInstances[0].id, taskId: demoTasks[0].id, payload: { agent_id: demoAgentInstances[0].id, agent_kind: "requirements", previous_location: "PROJECT_INTAKE", location: "REQUIREMENTS_HALL" } },
  { event_type: "task.blocked", agentId: demoAgentInstances[0].id, taskId: demoTasks[0].id, payload: { task_id: demoTasks[0].id, kind: "intake", title: "Resolve drive sizing clarification", status: "blocked", agent_kind: "requirements", detail: "Human input required" } },
  { event_type: "task.completed", agentId: demoAgentInstances[0].id, taskId: demoTasks[0].id, payload: { task_id: demoTasks[0].id, kind: "intake", title: "Resolve drive sizing clarification", status: "completed", agent_kind: "requirements" } },
  { event_type: "agent.location_changed", agentId: demoAgentInstances[1].id, taskId: demoTasks[1].id, payload: { agent_id: demoAgentInstances[1].id, agent_kind: "electrical_io", previous_location: "ESCO_PROJECT_CORE", location: "ELECTRICAL_WORKS" } },
  { event_type: "agent.location_changed", agentId: demoAgentInstances[2].id, taskId: demoTasks[2].id, payload: { agent_id: demoAgentInstances[2].id, agent_kind: "plc_logic", previous_location: "AUTOMATION_CORE", location: "LADDER_LOGIC_LAB" } },
  { event_type: "agent.location_changed", agentId: demoAgentInstances[3].id, taskId: demoTasks[3].id, payload: { agent_id: demoAgentInstances[3].id, agent_kind: "hmi_engineering", previous_location: "TAG_FOUNDRY", location: "HMI_DISTRICT" } },
  { event_type: "finding.created", agentId: demoAgentInstances[4].id, taskId: demoTasks[4].id, payload: { finding_id: "02410000-0000-4000-8500-000000000001", category: "safety_interlock", confidence: 0.94, requires_approval: true, severity: "critical", status: "open", title: "Guard reset requires stopped-state confirmation", created_by_agent_kind: "senior_review", evidence_count: 2 } },
  { event_type: "approval.requested", agentId: demoAgentInstances[4].id, taskId: demoTasks[4].id, payload: { approval_id: "02410000-0000-4000-8600-000000000001", finding_id: "02410000-0000-4000-8500-000000000001", kind: "finding_disposition", status: "pending", title: "Review guard-reset discrepancy" } },
  { event_type: "agent.location_changed", agentId: demoAgentInstances[4].id, taskId: demoTasks[4].id, payload: { agent_id: demoAgentInstances[4].id, agent_kind: "senior_review", previous_location: "QA_CAMPUS", location: "APPROVAL_PLAZA" } }
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
