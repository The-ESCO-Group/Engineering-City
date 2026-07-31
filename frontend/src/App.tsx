import { useMemo } from "react";
import { NavLink, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { Activity, AlertTriangle, Archive, Bell, Bot, Building2, ChevronDown, ClipboardCheck, Clock3, FileText, Filter, FolderKanban, ListChecks, LogOut, Menu, Plus, Search, Settings, ShieldAlert, Wifi } from "lucide-react";
import { reportDownloadUrl } from "./api/client";
import { useEngineeringData } from "./api/live";
import { City } from "./components/City";
import { ConnectionCenter } from "./components/ConnectionCenter";
import { agents as demoAgents, findings as demoFindings, projects } from "./domain";
import {
  EngineeringWorkspace,
  workspaceOperationsFromEngineering,
  workspaceSourceFromEngineering,
  type WorkspaceView
} from "./features/workspace";

function titleCase(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

const nav = [
  ["Engineering City", "/", Building2], ["Projects", "/projects", FolderKanban], ["Tasks", "/tasks", ListChecks],
  ["Findings", "/findings", ShieldAlert], ["Approvals", "/approvals", ClipboardCheck], ["Reports", "/reports", FileText],
  ["Agents", "/agents", Bot], ["Audit Trail", "/audit", Archive], ["Settings", "/settings", Settings]
] as const;
function Shell() {
  const location = useLocation();
  const isCityHome = location.pathname === "/";
  const isWorkspaceShell = location.pathname.startsWith("/workspace/") ||
    location.pathname === "/workspace" ||
    location.pathname === "/tasks" ||
    location.pathname === "/findings" ||
    location.pathname === "/approvals";
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
  return <div className={`app${isCityHome ? " city-home" : ""}${isWorkspaceShell ? " engineering-workspace-shell" : ""}`}>
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
        <Route path="/tasks" element={<EngineeringWorkspacePage initialView="tasks" />} />
        <Route path="/findings" element={<EngineeringWorkspacePage initialView="findings" />} />
        <Route path="/approvals" element={<EngineeringWorkspacePage initialView="approvals" />} />
        <Route path="/workspace" element={<EngineeringWorkspacePage initialView="requirements" />} />
        <Route path="/workspace/:view" element={<WorkspaceRoute />} />
        <Route path="/reports" element={<Reports />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<SimplePage />} />
      </Routes>
    </main>
    {!isCityHome && !isWorkspaceShell && <ActivityPanel />}
  </div>;
}

const WORKSPACE_VIEWS = new Set<WorkspaceView>([
  "intake",
  "files",
  "tasks",
  "requirements",
  "devices",
  "tags",
  "plc",
  "planning",
  "changesets",
  "revisions",
  "findings",
  "approvals"
]);

function WorkspaceRoute() {
  const { view } = useParams();
  const initialView = view && WORKSPACE_VIEWS.has(view as WorkspaceView)
    ? view as WorkspaceView
    : "requirements";
  return <EngineeringWorkspacePage key={initialView} initialView={initialView} />;
}

function EngineeringWorkspacePage({ initialView }: { initialView: WorkspaceView }) {
  const engineering = useEngineeringData();
  const navigate = useNavigate();
  const source = useMemo(() => workspaceSourceFromEngineering(engineering), [engineering]);
  const operations = useMemo(
    () => workspaceOperationsFromEngineering(engineering, {
      openCityLocation: (semanticLocation) => {
        navigate(`/?location=${encodeURIComponent(semanticLocation)}`);
      }
    }),
    [engineering, navigate]
  );
  return (
    <EngineeringWorkspace
      source={source}
      initialView={initialView}
      operations={operations}
      onReturnToCity={() => navigate("/")}
    />
  );
}

function SettingsPage() {
  return <section className="page settings-page"><PageHeader eyebrow="LOCAL OPERATIONS" title="Settings" /><ConnectionCenter /></section>;
}

function PageHeader({ eyebrow, title, action, onAction }: { eyebrow: string; title: string; action?: string; onAction?: () => void }) {
  return <div className="page-header"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1></div>{action && <button className="primary" type="button" onClick={onAction} disabled={!onAction}><Plus />{action}</button>}</div>;
}
function Projects() {
  const engineering = useEngineeringData();
  const navigate = useNavigate();
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
  return <section className="page"><PageHeader eyebrow="PORTFOLIO" title="Engineering projects" action="New project" onAction={() => navigate("/workspace/intake")} /><div className="filters"><div className="search"><Search /><input aria-label="Search projects" placeholder="Search projects" /></div><button type="button"><Filter /> Filters</button></div>{engineering.dataMode === "live" && <p className="advisory wide"><ShieldAlert />{engineering.parsers.length - unsupported.length} parser kinds available. {unsupported.length ? `${unsupported.length} unsupported kind(s): ${unsupported.map((item) => item.file_kind).join(", ")}.` : "Uploaded formats can be checked locally before review."}</p>}<div className="project-grid">{displayedProjects.map(p => <article className="project-card" key={p.id}><div className="project-card-head"><span>{p.id}</span><em className={p.progress === 100 ? "done" : ""}>{p.status}</em></div><h3>{p.name}</h3><p>{p.customer}</p><div className="progress"><i style={{ width: `${p.progress}%` }} /></div><div className="card-stats"><span><b>{p.progress}%</b> complete</span><span><b>{p.openFindings}</b> findings</span><span><b>{p.pendingApprovals}</b> approvals</span></div><footer><Clock3 /> Updated {p.lastActivity}<button type="button" onClick={() => navigate("/workspace/requirements")}>Open workspace →</button></footer></article>)}</div>{displayedProjects.length === 0 && <div className="empty-state"><FolderKanban /><h2>No project data available</h2><p>{engineering.fallbackReason}</p></div>}</section>;
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
