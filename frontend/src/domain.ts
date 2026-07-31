export type Severity = "Critical" | "High" | "Medium" | "Low";
export type TaskState = "Queued" | "Running" | "Blocked" | "Awaiting approval" | "Completed" | "Failed";
export type BuildingId = "intake" | "automation" | "hmi" | "electrical" | "qa" | "documentation" | "approval" | "release";

export interface Finding {
  id: string; severity: Severity; category: string; title: string; status: string;
  source: string; location: string; agent: string; confidence: number; reviewer: string;
  approval: string; evidence: string; requirement: string; suggestion: string; risk: string;
}
export interface Agent {
  id: string; name: string; role: string; building: BuildingId; state: "active" | "waiting" | "complete";
  task: string;
}
export interface Project {
  id: string; name: string; customer: string; status: string; progress: number;
  openFindings: number; pendingApprovals: number; lastActivity: string;
}

export const buildings: Array<{ id: BuildingId; name: string; x: number; y: number; short: string }> = [
  { id: "intake", name: "Project Intake", x: 15, y: 38, short: "INTAKE" },
  { id: "automation", name: "Automation Core", x: 39, y: 32, short: "PLC" },
  { id: "hmi", name: "HMI District", x: 65, y: 36, short: "HMI" },
  { id: "electrical", name: "Electrical Works", x: 84, y: 61, short: "ELEC" },
  { id: "qa", name: "QA Campus", x: 65, y: 78, short: "QA" },
  { id: "documentation", name: "Documentation Quarter", x: 39, y: 82, short: "DOCS" },
  { id: "approval", name: "Approval Plaza", x: 15, y: 70, short: "APPROVAL" },
  { id: "release", name: "Release Logistics", x: 85, y: 87, short: "RELEASE" }
];

export const projects: Project[] = [
  { id: "PRJ-0241", name: "Line 4 Packaging Conveyor Upgrade", customer: "Internal · Packaging", status: "Engineering review", progress: 68, openFindings: 12, pendingApprovals: 2, lastActivity: "2 min ago" },
  { id: "PRJ-0238", name: "Utility Skid Controls Review", customer: "Internal · Process", status: "Awaiting approval", progress: 86, openFindings: 5, pendingApprovals: 3, lastActivity: "18 min ago" },
  { id: "PRJ-0229", name: "Palletizer FAT Package", customer: "Internal · Material Handling", status: "Completed", progress: 100, openFindings: 0, pendingApprovals: 0, lastActivity: "Yesterday" }
];

export const agents: Agent[] = [
  { id: "plc", name: "PLC Review", role: "Logic analysis", building: "automation", state: "active", task: "Reviewing safety interlocks" },
  { id: "hmi", name: "HMI Review", role: "Tag & alarm analysis", building: "hmi", state: "active", task: "Resolving tag references" },
  { id: "electrical", name: "Electrical Review", role: "I/O reconciliation", building: "electrical", state: "active", task: "Comparing device schedule" },
  { id: "senior", name: "Senior Review", role: "Finding validation", building: "qa", state: "waiting", task: "Waiting for evidence package" },
  { id: "docs", name: "Documentation", role: "Report delivery", building: "documentation", state: "complete", task: "Executive summary ready" }
];

export const findings: Finding[] = [
  { id: "F-104", severity: "Critical", category: "Safety interlock", title: "Guard reset bypasses stopped-state confirmation", status: "Needs review", source: "MainProgram.L5X", location: "Routine Safety, Rung 14", agent: "PLC Review", confidence: 0.94, reviewer: "N. Modha", approval: "Required", evidence: "XIC GuardReset branches around XIO AxisRunning before OTL GuardSafe.", requirement: "SRS-4.3.2 — reset permitted only with motion stopped.", suggestion: "Place stopped-state proof in the common reset path; validate on offline test fixture.", risk: "Unexpected motion after guard reset. Mandatory human review." },
  { id: "F-107", severity: "High", category: "I/O discrepancy", title: "Photoeye PE-402 address differs from device schedule", status: "Open", source: "IO_Schedule.xlsx", location: "Digital Inputs!F42", agent: "Electrical Review", confidence: 0.88, reviewer: "Unassigned", approval: "Pending", evidence: "Schedule lists Local:3:I.Data.7; PLC alias resolves to Local:3:I.Data.6.", requirement: "Device schedule and controller aliases must identify the same terminal.", suggestion: "Verify field wiring and update the incorrect source under change control.", risk: "Incorrect jam detection or misleading diagnostics." },
  { id: "F-112", severity: "Medium", category: "HMI tag", title: "Alarm banner references unresolved conveyor tag", status: "Open", source: "Alarms.xml", location: "/Alarms/Alarm[18]/Tag", agent: "HMI Review", confidence: 0.76, reviewer: "A. Patel", approval: "Not started", evidence: "Tag Conveyor_4.JamAlarm has no match in normalized controller tags.", requirement: "Alarm references must resolve to an exported controller tag.", suggestion: "Confirm renamed tag or remove stale alarm after operator review.", risk: "Alarm may not activate; runtime validation is not claimed." },
  { id: "F-118", severity: "Low", category: "Documentation", title: "Motor starter description is missing", status: "Open", source: "Device_List.csv", location: "Row 64", agent: "Electrical Review", confidence: 0.99, reviewer: "Unassigned", approval: "Not required", evidence: "Description column is empty for identifier MS-214.", requirement: "Device list entries require functional descriptions.", suggestion: "Add the approved equipment description.", risk: "Reduced maintainability." }
];

export const tasks: Array<{ name: string; owner: string; state: TaskState; progress: number }> = [
  { name: "Normalize uploaded project files", owner: "Project Manager", state: "Completed", progress: 100 },
  { name: "Review PLC program and interlocks", owner: "PLC Review", state: "Running", progress: 72 },
  { name: "Reconcile HMI tags and alarms", owner: "HMI Review", state: "Running", progress: 56 },
  { name: "Compare electrical I/O schedule", owner: "Electrical Review", state: "Running", progress: 61 },
  { name: "Validate high-risk findings", owner: "Senior Review", state: "Blocked", progress: 20 },
  { name: "Approve corrective action plan", owner: "N. Modha", state: "Awaiting approval", progress: 0 },
  { name: "Generate release package", owner: "Documentation", state: "Queued", progress: 0 }
];
