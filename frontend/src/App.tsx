import { useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { Activity, AlertTriangle, Archive, Bell, Bot, Building2, ChevronDown, ClipboardCheck, Clock3, FileText, Filter, FolderKanban, ListChecks, LogOut, Menu, Plus, Search, Settings, ShieldAlert, Wifi } from "lucide-react";
import { reportDownloadUrl } from "./api/client";
import { useApprovalDecision, useEngineeringData, useFindingDetail } from "./api/live";
import type { FindingDetail as ApiFindingDetail, FindingRead } from "./api/types";
import { City } from "./components/City";
import { ConnectionCenter } from "./components/ConnectionCenter";
import { agents as demoAgents, findings as demoFindings, projects, tasks, type Finding } from "./domain";
import { useUI } from "./store";

type DisplayFinding = Finding & {
  fact: string;
  inference: string;
  limitations: string;
  citations: string[];
  requiresApproval: boolean;
  isAccepted: boolean;
};

function titleCase(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function demoFinding(finding: Finding): DisplayFinding {
  return {
    ...finding,
    fact: finding.evidence,
    inference: finding.risk,
    limitations: "Synthetic demonstration record; no live backend evidence was queried.",
    citations: [finding.location],
    requiresApproval: finding.approval === "Required" || finding.approval === "Pending",
    isAccepted: finding.status.toLowerCase() === "accepted"
  };
}

function liveFinding(finding: FindingRead): DisplayFinding {
  const severity = titleCase(finding.severity) as DisplayFinding["severity"];
  return {
    id: finding.id,
    severity,
    category: titleCase(finding.category),
    title: finding.title,
    status: titleCase(finding.status),
    source: `${finding.evidence_count} evidence citation${finding.evidence_count === 1 ? "" : "s"}`,
    location: "Open finding to resolve exact citation locations",
    agent: titleCase(finding.created_by_agent_kind || "Review agent"),
    confidence: finding.confidence,
    reviewer: finding.is_reviewed ? "Senior reviewed" : "Awaiting senior review",
    approval: finding.requires_approval && finding.status !== "accepted" ? "Required" : "Released",
    evidence: finding.fact,
    requirement: finding.inference,
    suggestion: finding.suggested_review_action,
    risk: finding.inference,
    fact: finding.fact,
    inference: finding.inference,
    limitations: finding.limitations || "No limitations were stated by the reviewing agent.",
    citations: [],
    requiresApproval: finding.requires_approval,
    isAccepted: finding.status === "accepted"
  };
}

function applyFindingDetail(finding: DisplayFinding, detail?: ApiFindingDetail): DisplayFinding {
  if (!detail) return finding;
  return {
    ...finding,
    fact: detail.fact,
    inference: detail.inference,
    limitations: detail.limitations || "No limitations were stated by the reviewing agent.",
    suggestion: detail.suggested_review_action,
    citations: detail.evidence
      .map((item) => item.anchor?.display_location || "")
      .filter(Boolean),
    source: detail.evidence[0]?.anchor?.source_label || finding.source,
    location: detail.evidence[0]?.anchor?.display_location || finding.location
  };
}

const nav = [
  ["Engineering City", "/", Building2], ["Projects", "/projects", FolderKanban], ["Tasks", "/tasks", ListChecks],
  ["Findings", "/findings", ShieldAlert], ["Approvals", "/approvals", ClipboardCheck], ["Reports", "/reports", FileText],
  ["Agents", "/agents", Bot], ["Audit Trail", "/audit", Archive], ["Settings", "/settings", Settings]
] as const;
function Shell() {
  const location = useLocation();
  const isCityHome = location.pathname === "/";
  const engineering = useEngineeringData();
  const projectReference = engineering.project?.client_reference || "NO PROJECT";
  const projectName = engineering.project?.name || "Select an authorized workspace";
  const findingCount = engineering.dataMode === "demo"
    ? demoFindings.filter((finding) => finding.status !== "Accepted").length
    : engineering.findings.filter((finding) => finding.status === "open").length;
  const approvalCount = engineering.dataMode === "demo"
    ? 1
    : engineering.approvals.filter((approval) => approval.status === "pending").length;
  const enabledProvider = engineering.providers.find((provider) => provider.available)?.provider;
  const modeLabel = engineering.dataMode === "demo"
    ? "DEMO DATA"
    : engineering.dataMode === "live"
      ? "LIVE LOCAL DATA"
      : "LOCAL DATA UNAVAILABLE";
  const user = engineering.currentUser?.user;
  const initials = user?.display_name
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase() || (engineering.dataMode === "demo" ? "DM" : "--");
  const connectionLabel = titleCase(engineering.connection);
  return <div className={`app${isCityHome ? " city-home" : ""}`}>
    <header className="topbar">
      <button className="menu"><Menu /></button>
      <div className="brand-mark">E</div><div className="brand"><strong>ESCO</strong><span>ENGINEERING CITY</span></div>
      <div className="project-switch"><span>Current project</span><b>{projectReference} · {projectName}</b><ChevronDown aria-hidden="true" /></div>
      <div className="top-spacer" />
      <span className="demo-badge">{modeLabel}</span>
      <span className="connection"><Wifi aria-hidden="true" /> {isCityHome ? "System status" : "Local backend"} <b>{engineering.dataMode === "live" ? `${connectionLabel} · ${enabledProvider || "provider private"}` : connectionLabel}</b></span>
      <button className="icon-button" type="button" aria-label="Notifications"><Bell aria-hidden="true" /><i className="notification" /></button>
      <NavLink className="user" to="/settings" aria-label="Open connection and account settings">{initials}</NavLink>
    </header>
    <aside className="sidebar">
      <nav aria-label="Primary navigation">{nav.map(([label, to, Icon]) => <NavLink key={label} to={to} end={to === "/"} aria-label={isCityHome ? label : undefined} title={isCityHome ? label : undefined}><Icon /><span>{label}</span>{label === "Findings" && <em>{findingCount}</em>}{label === "Approvals" && <em>{approvalCount}</em>}</NavLink>)}</nav>
      <div className="sidebar-foot"><div className="avatar">{initials}</div><div><b>{user?.display_name || (engineering.dataMode === "demo" ? "Demo reviewer" : "Not signed in")}</b><span>{user ? user.roles.map(titleCase).join(" · ") : engineering.dataMode === "demo" ? "Synthetic session" : "Local account required"}</span></div>{user && <button type="button" className="sidebar-logout" onClick={() => void engineering.logout()} aria-label="Sign out of local backend"><LogOut aria-hidden="true" /></button>}</div>
    </aside>
    <main>
      <Routes>
        <Route path="/" element={<City />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/tasks" element={<Tasks />} />
        <Route path="/findings" element={<Findings />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/reports" element={<Reports />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<SimplePage />} />
      </Routes>
    </main>
    {!isCityHome && <ActivityPanel />}
  </div>;
}

function SettingsPage() {
  return <section className="page settings-page"><PageHeader eyebrow="LOCAL OPERATIONS" title="Settings" /><ConnectionCenter /></section>;
}

function PageHeader({ eyebrow, title, action }: { eyebrow: string; title: string; action?: string }) {
  return <div className="page-header"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1></div>{action && <button className="primary"><Plus />{action}</button>}</div>;
}
function Projects() {
  const engineering = useEngineeringData();
  const liveProject = engineering.project;
  const liveProgress = liveProject?.counts.tasks
    ? Math.round((liveProject.counts.tasks_completed / liveProject.counts.tasks) * 100)
    : 0;
  const displayedProjects = engineering.dataMode === "demo"
    ? projects
    : liveProject ? [{
        id: liveProject.client_reference || liveProject.id.slice(0, 8),
        name: liveProject.name,
        customer: liveProject.description || "Local engineering project",
        status: titleCase(liveProject.status),
        progress: liveProgress,
        openFindings: liveProject.counts.findings_open,
        pendingApprovals: liveProject.counts.approvals_pending,
        lastActivity: new Date(liveProject.updated_at).toLocaleString()
      }]
    : [];
  const unsupported = engineering.parsers.filter((parser) => !parser.available);
  return <section className="page"><PageHeader eyebrow="PORTFOLIO" title="Engineering projects" action="New project" /><div className="filters"><div className="search"><Search /><input aria-label="Search projects" placeholder="Search projects" /></div><button type="button"><Filter /> Filters</button></div>{engineering.dataMode === "live" && <p className="advisory wide"><ShieldAlert />{engineering.parsers.length - unsupported.length} parser kinds available. {unsupported.length ? `${unsupported.length} unsupported kind(s): ${unsupported.map((item) => item.file_kind).join(", ")}.` : "Uploaded formats can be checked locally before review."}</p>}<div className="project-grid">{displayedProjects.map(p => <article className="project-card" key={p.id}><div className="project-card-head"><span>{p.id}</span><em className={p.progress === 100 ? "done" : ""}>{p.status}</em></div><h3>{p.name}</h3><p>{p.customer}</p><div className="progress"><i style={{ width: `${p.progress}%` }} /></div><div className="card-stats"><span><b>{p.progress}%</b> complete</span><span><b>{p.openFindings}</b> findings</span><span><b>{p.pendingApprovals}</b> approvals</span></div><footer><Clock3 /> Updated {p.lastActivity}<button type="button">Open workspace →</button></footer></article>)}</div>{displayedProjects.length === 0 && <div className="empty-state"><FolderKanban /><h2>No project data available</h2><p>{engineering.fallbackReason}</p></div>}</section>;
}
function Tasks() {
  const engineering = useEngineeringData();
  const displayedTasks = engineering.dataMode === "demo" ? tasks : engineering.tasks.map((task) => ({
    name: task.title,
    owner: titleCase(task.agent_kind || "Unassigned"),
    state: titleCase(task.status) as (typeof tasks)[number]["state"],
    progress: task.status === "completed" ? 100 : task.status === "running" ? 50 : 0
  }));
  return <section className="page"><PageHeader eyebrow="WORKFLOW" title="Task board" /><div className="task-board">{["Queued","Running","Blocked","Awaiting approval","Completed"].map(state => <div className="task-column" key={state}><h3>{state}<span>{displayedTasks.filter(t=>t.state===state).length}</span></h3>{displayedTasks.filter(t=>t.state===state).map(t=><article key={t.name}><div className={`status-bar ${t.state.toLowerCase().replace(" ","-")}`} /><h4>{t.name}</h4><p><Bot />{t.owner}</p><div className="progress"><i style={{width:`${t.progress}%`}} /></div></article>)}</div>)}</div>{displayedTasks.length === 0 && <div className="empty-state"><ListChecks /><h2>No workflow tasks available</h2><p>{engineering.fallbackReason}</p></div>}</section>;
}
function Findings() {
  const engineering = useEngineeringData();
  const { selectedFinding, selectFinding } = useUI();
  const displayedFindings = engineering.dataMode === "demo"
    ? demoFindings.map(demoFinding)
    : engineering.findings.map(liveFinding);
  const selected = displayedFindings.find((finding) => finding.id === selectedFinding)
    || displayedFindings[0];
  const detailQuery = useFindingDetail(selected?.id || null, engineering.dataMode === "live");
  const detailed = selected ? applyFindingDetail(selected, detailQuery.data) : null;
  if (!detailed) {
    return <section className="page"><PageHeader eyebrow="ENGINEERING REVIEW" title="Findings & evidence" /><div className="empty-state"><ShieldAlert /><h2>No findings recorded</h2><p>The review has not produced any findings for this project.</p></div></section>;
  }
  return <section className="page findings-page"><PageHeader eyebrow="ENGINEERING REVIEW" title="Findings & evidence" /><div className="findings-layout"><div className="table-wrap"><div className="filters"><div className="search"><Search /><input placeholder="Search findings" /></div><button><Filter /> Severity: All</button></div><table><thead><tr><th>Severity</th><th>Finding</th><th>Evidence</th><th>Agent</th><th>Confidence</th><th>Release state</th></tr></thead><tbody>{displayedFindings.map(f=><tr key={f.id} className={detailed.id===f.id?"selected":""} onClick={()=>selectFinding(f.id)}><td><span className={`severity ${f.severity.toLowerCase()}`}>{f.severity}</span></td><td><b>{f.id.slice(0, 8)} · {f.title}</b><small>{f.category} · {f.status}</small></td><td>{f.source}<small>{f.location}</small></td><td>{f.agent}</td><td>{Math.round(f.confidence*100)}%</td><td>{f.requiresApproval && !f.isAccepted ? "Decision required" : f.isAccepted ? "Accepted" : "Advisory"}</td></tr>)}</tbody></table></div><FindingDetail finding={detailed} /></div></section>;
}
function FindingDetail({ finding: f }: { finding: DisplayFinding }) {
  const unreleased = f.requiresApproval && !f.isAccepted;
  return <aside className="detail-panel"><div className="detail-title"><span className={`severity ${f.severity.toLowerCase()}`}>{f.severity}</span><span>{f.id}</span></div><h2>{f.title}</h2>{unreleased && <p className="risk"><AlertTriangle />Proposed finding — human approval is required. This is not a released engineering result.</p>}<Detail label="Observed fact" text={f.fact || "No fact was recorded."} /><Detail label="Engineering inference" text={f.inference || "No inference was recorded."} /><Detail label="Limitations" text={f.limitations} /><div className="detail"><span className="eyebrow">Evidence citations</span>{f.citations.length ? f.citations.map((citation) => <p key={citation}>{citation}</p>) : <p>{f.location}</p>}</div><Detail label="Suggested review action" text={f.suggestion || "No review action was recorded."} /><div className="confidence"><span>Agent confidence</span><b>{Math.round(f.confidence*100)}%</b><div className="progress"><i style={{width:`${f.confidence*100}%`}} /></div></div><p className="advisory"><ShieldAlert />AI output is advisory. Engineering responsibility remains with the authorized human reviewer.</p><div className="button-row"><button>Request clarification</button><button className="primary">{unreleased ? "Open approval decision" : "Review record"}</button></div></aside>;
}
function Detail({label,text}:{label:string;text:string}) { return <div className="detail"><span className="eyebrow">{label}</span><p>{text}</p></div>; }
function Approvals() {
  const engineering = useEngineeringData();
  const pending = engineering.approvals.filter((approval) => approval.status === "pending");
  const approval = pending[0];
  const [decision, setDecision] = useState("");
  const [notes, setNotes] = useState("");
  const mutation = useApprovalDecision(engineering.projectId);
  if (engineering.dataMode !== "demo" && !approval) {
    return <section className="page"><PageHeader eyebrow="HUMAN AUTHORITY" title="Approval inbox" /><div className="empty-state"><ClipboardCheck /><h2>No decisions waiting</h2><p>Every approval currently has a recorded human decision.</p></div></section>;
  }
  const title = approval?.title || "Corrective action for guard reset logic";
  const question = approval?.question || "The proposed change affects a safety-related reset sequence. Confirm the evidence and validation plan before deciding.";
  const submit = () => {
    if (!approval || !decision || !notes.trim()) return;
    mutation.mutate({ approvalId: approval.id, approved: decision === "Accept", notes: notes.trim() });
  };
  return <section className="page"><PageHeader eyebrow="HUMAN AUTHORITY" title="Approval inbox" /><div className="approval-layout"><article className="approval-list"><span className="severity critical">Decision</span><h3>{title}</h3><p>{engineering.project?.client_reference || "PRJ-0241"} · {approval?.finding_id ? `Finding ${approval.finding_id.slice(0, 8)}` : "Synthetic finding F-104"} · Requested by {titleCase(approval?.requested_by_agent_kind || "Senior Review")}</p><div><Clock3 /> {approval?.requested_at ? new Date(approval.requested_at).toLocaleString() : "Due today"} <span>Authorized reviewer required</span></div></article><article className="approval-decision"><span className="eyebrow">DECISION REQUIRED</span><h2>{title}</h2><p>{question}</p><label>Decision<select value={decision} onChange={e=>setDecision(e.target.value)}><option value="">Select decision…</option><option>Accept</option><option>Reject</option></select></label><label>Engineering rationale (required)<textarea value={notes} onChange={event => setNotes(event.target.value)} placeholder="Record the evidence considered and basis for this decision…" /></label><button className="primary" disabled={!decision || !notes.trim() || mutation.isPending} onClick={submit}>{mutation.isPending ? "Recording decision…" : "Submit signed decision"}</button>{mutation.isError && <p className="risk"><AlertTriangle />{mutation.error.message}</p>}<p className="advisory"><ShieldAlert />This decision is recorded in the audit trail. AI analysis does not constitute engineering approval.</p></article></div></section>;
}
function Reports() {
  const engineering = useEngineeringData();
  const downloadDemo = (name:string) => {
    const blob = new Blob([JSON.stringify({ project:"PRJ-0241", report:name, notice:"Synthetic demo data only" }, null, 2)], { type:"application/json" });
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(blob);
    anchor.download = `${name.toLowerCase().replaceAll(" ","-")}.json`;
    anchor.click();
    URL.revokeObjectURL(anchor.href);
  };
  if (engineering.dataMode === "live") {
    return <section className="page"><PageHeader eyebrow="DELIVERY" title="Reports & exports" /><div className="report-grid">{engineering.reports.map((report) => <article className="report-card" key={report.id}><FileText /><div><span>{report.export_format.toUpperCase()} · {report.provider_name}</span><h3>{report.title}</h3><p>{report.finding_ids.length} accepted finding{report.finding_ids.length === 1 ? "" : "s"} · {titleCase(report.status)}</p></div>{report.download_path && <a className="primary" href={reportDownloadUrl(report.download_path)}>Download generated artifact</a>}</article>)}</div>{engineering.reports.length === 0 && <div className="empty-state"><FileText /><h2>No reports generated</h2><p>The delivery task has not produced an accepted-finding artifact yet.</p></div>}<p className="advisory wide"><ShieldAlert />Generated reports remain traceable to accepted finding IDs. “Generated” does not mean import-verified or human-approved.</p></section>;
  }
  if (engineering.dataMode === "demo") {
    return <section className="page"><PageHeader eyebrow="DELIVERY" title="Reports & exports" /><div className="report-grid">{["Detailed discrepancy report","Executive summary","Findings register","FAT test plan","Audit export"].map((report,index)=><article className="report-card" key={report}><FileText /><div><span>JSON · DEMO</span><h3>{report}</h3><p>Generated from synthetic evidence · Version 0.{index+3}</p></div><button onClick={()=>downloadDemo(report)}>Generate demo JSON</button></article>)}</div><p className="advisory wide"><ShieldAlert />Demo exports are clearly marked and contain no real engineering analysis. Sign in to the local backend for accepted-finding reports.</p></section>;
  }
  return <section className="page"><PageHeader eyebrow="DELIVERY" title="Reports & exports" /><div className="empty-state"><FileText /><h2>Report data is unavailable</h2><p>{engineering.fallbackReason}</p></div></section>;
}
function SimplePage() { return <section className="page"><PageHeader eyebrow="WORKSPACE" title="Module overview" /><div className="empty-state"><Activity /><h2>Operational module</h2><p>This module is available from the project workspace once the backend contract is connected.</p></div></section>; }
function ActivityPanel() {
  const engineering = useEngineeringData();
  const visibleAgents = engineering.dataMode === "demo"
    ? demoAgents
    : engineering.agents.map((agent) => ({
        id: agent.id,
        name: agent.display_name,
        state: agent.status === "working" || agent.status === "walking" ? "active" : agent.status === "blocked" ? "waiting" : "complete",
        task: agent.current_activity || agent.location || titleCase(agent.status)
      }));
  const active = visibleAgents.filter((agent) => agent.state === "active").length;
  const waiting = visibleAgents.filter((agent) => agent.state === "waiting").length;
  const recentEvents = engineering.dataMode === "demo"
    ? [["F-104 escalated for review","12:42","warning"],["HMI tag scan 56% complete","12:39","active"],["Electrical evidence indexed","12:34","done"],["Senior review waiting","12:31","waiting"]]
    : engineering.messages.slice(-4).reverse().map((message) => [message.body, new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), "active"]);
  const pendingApprovals = engineering.dataMode === "demo"
    ? 1
    : engineering.approvals.filter((approval) => approval.status === "pending").length;
  const activityTitle = engineering.dataMode === "demo" ? "Demo activity" : engineering.dataMode === "live" ? "Live activity" : "Activity unavailable";
  return <aside className="activity-panel"><div className="activity-head"><div><span className="eyebrow">OPERATIONS</span><h2>{activityTitle}</h2></div><button type="button" aria-label="Filter activity"><Filter /></button></div><div className="agent-summary"><span><b>{active}</b> active</span><span><b>{waiting}</b> waiting</span><span><b>{engineering.agents.filter((agent) => agent.status === "blocked").length}</b> blocked</span></div><div className="panel-section"><h3>Active agents <span>{visibleAgents.length}</span></h3>{visibleAgents.slice(0,4).map(agent=><div className="agent-row" key={agent.id}><div className={`agent-icon ${agent.state}`}>{agent.name.slice(0,2).toUpperCase()}</div><div><b>{agent.name}</b><span>{agent.task}</span></div><i /></div>)}</div><div className="panel-section"><h3>Recent events</h3>{recentEvents.map(([eventText,time,type])=><div className="event" key={`${eventText}-${time}`}><i className={type}/><div><b>{eventText}</b><span>{time}</span></div></div>)}</div>{pendingApprovals > 0 && <div className="warning-box"><AlertTriangle /><div><b>{pendingApprovals} approval{pendingApprovals === 1 ? "" : "s"} need attention</b><span>Human decisions are required before release.</span></div></div>}</aside>;
}

export function App() { return <Shell />; }
