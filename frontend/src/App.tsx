import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { NavLink, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from "./routing";
import { Activity, AlertTriangle, Archive, Bell, BellRing, Bot, Building2, ChevronDown, ClipboardCheck, Clock3, Database, FileText, Filter, FlaskConical, FolderKanban, ListChecks, LogOut, Menu, Monitor, Plus, Search, Settings, ShieldAlert, Wifi } from "lucide-react";
import { useEngineeringData } from "./api/live";
import { City } from "./components/City";
import { ConnectionCenter } from "./components/ConnectionCenter";
import { DesktopCenter } from "./components/DesktopCenter";
import { notifyOperatorEvent } from "./desktop/bridge";
import { agents as demoAgents, findings as demoFindings } from "./domain";
import { operationsSourceFromEngineering } from "./features/operations/integration";
import type { OperationsView } from "./features/operations/types";
import { releaseOperationsFromEngineering, releaseSourceFromEngineering } from "./features/release/integration";
import type { ReleaseCenterView } from "./features/release/ReleaseCenter";
import { workspaceOperationsFromEngineering, workspaceSourceFromEngineering } from "./features/workspace/integration";
import type { WorkspaceView } from "./features/workspace/types";

const EngineeringWorkspace = lazy(async () => {
  const feature = await import("./features/workspace/EngineeringWorkspace");
  return { default: feature.EngineeringWorkspace };
});

const ReleaseCenter = lazy(async () => {
  const feature = await import("./features/release/ReleaseCenter");
  return { default: feature.ReleaseCenter };
});

const OperationsCenter = lazy(async () => {
  const feature = await import("./features/operations/OperationsCenter");
  return { default: feature.OperationsCenter };
});

function FeatureLoading({ label }: { label: string }) {
  return <div className="feature-loading" role="status"><span className="brand-mark">E</span><b>Loading {label}…</b></div>;
}

function titleCase(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

const nav = [
  ["Engineering City", "/", Building2], ["Projects", "/projects", FolderKanban], ["Project Core", "/workspace/requirements", Database],
  ["Tasks", "/tasks", ListChecks], ["HMI", "/workspace/hmi", Monitor], ["Alarms", "/workspace/alarms", BellRing],
  ["Tests", "/workspace/tests", FlaskConical],
  ["Findings", "/findings", ShieldAlert], ["Approvals", "/approvals", ClipboardCheck], ["Reports", "/reports", FileText],
  ["Agents", "/agents", Bot], ["Audit Trail", "/audit", Archive], ["Settings", "/settings", Settings]
] as const;
function Shell() {
  const location = useLocation();
  const navigate = useNavigate();
  const isCityHome = location.pathname === "/";
  const isWorkspaceShell = location.pathname.startsWith("/workspace/") ||
    location.pathname === "/workspace" ||
    location.pathname === "/tasks" ||
    location.pathname === "/findings" ||
    location.pathname === "/approvals";
  const isReleaseShell = location.pathname === "/reports" ||
    location.pathname === "/release" ||
    location.pathname.startsWith("/release/");
  const isOperationsShell = location.pathname === "/agents" || location.pathname === "/audit";
  const [navigationCompact, setNavigationCompact] = useState(() =>
    typeof window.matchMedia === "function" && window.matchMedia("(max-width: 1179px)").matches
  );
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
  useEffect(() => {
    if (engineering.dataMode !== "live" || !engineering.projectId) return;

    const notifyOnce = (
      key: string,
      kind: "approval_required" | "workflow_failure",
      title: string,
      body: string
    ) => {
      try {
        if (window.sessionStorage.getItem(key)) return;
        window.sessionStorage.setItem(key, "pending");
      } catch {
        return;
      }
      void notifyOperatorEvent(kind, title, body)
        .then((sent) => {
          if (!sent) window.sessionStorage.removeItem(key);
          else window.sessionStorage.setItem(key, "sent");
        })
        .catch(() => window.sessionStorage.removeItem(key));
    };

    const pending = engineering.approvals.filter((approval) => approval.status === "pending");
    if (pending.length > 0) {
      const approvalIdentity = pending.map((approval) => approval.id).sort().join(":");
      notifyOnce(
        `esco:desktop-notification:${engineering.projectId}:approvals:${approvalIdentity}`,
        "approval_required",
        projectReference.slice(0, 100),
        `${pending.length} human approval${pending.length === 1 ? " is" : "s are"} waiting in the Approval Inbox.`
      );
    }

    const latestFailure = engineering.events
      .filter((event) => event.event_type === "task.failed")
      .sort((left, right) => right.seq - left.seq)[0];
    if (latestFailure) {
      notifyOnce(
        `esco:desktop-notification:${engineering.projectId}:failure:${latestFailure.seq}`,
        "workflow_failure",
        projectReference.slice(0, 100),
        "An engineering task failed. Review the Audit Trail and Findings before continuing."
      );
    }
  }, [engineering.approvals, engineering.dataMode, engineering.events, engineering.projectId, projectReference]);
  return <div className={`app${isCityHome ? " city-home" : ""}${isWorkspaceShell ? " engineering-workspace-shell" : ""}${isReleaseShell ? " release-center-shell" : ""}${isOperationsShell ? " operations-shell" : ""}${navigationCompact ? " nav-compact" : ""}`}>
    <header className="topbar">
      <button className="menu" type="button" aria-label="Primary navigation" aria-expanded={!navigationCompact} aria-controls="primary-navigation" onClick={() => setNavigationCompact((current) => !current)}><Menu aria-hidden="true" /></button>
      <div className="brand-mark">E</div><div className="brand"><strong>ESCO</strong><span>ENGINEERING CITY</span></div>
      <NavLink className="project-switch" to="/projects" aria-label="Open project portfolio"><span>Current project</span><b>{projectReference} · {projectName}</b><ChevronDown aria-hidden="true" /></NavLink>
      <div className="top-spacer" />
      <span className="demo-badge">{modeLabel}</span>
      <span className="connection"><Wifi aria-hidden="true" /> {isCityHome ? "System status" : "Local backend"} <b>{engineering.dataMode === "live" ? `${connectionLabel} · ${enabledProvider || "provider private"}` : connectionLabel}</b></span>
      <button className="icon-button" type="button" aria-label={approvalCount ? `Open ${approvalCount} pending approval notification${approvalCount === 1 ? "" : "s"}` : "Open Audit Trail"} onClick={() => navigate(approvalCount ? "/approvals" : "/audit")}><Bell aria-hidden="true" />{approvalCount > 0 && <i className="notification" />}</button>
      <NavLink className="user" to="/settings" aria-label="Open connection and account settings">{initials}</NavLink>
    </header>
    <aside className="sidebar" id="primary-navigation">
      <nav aria-label="Primary navigation">{nav.map(([label, to, Icon]) => <NavLink key={label} to={to} end={to === "/"} aria-label={label} title={label}><Icon aria-hidden="true" /><span>{label}</span>{label === "Findings" && <em>{findingCount}</em>}{label === "Approvals" && <em>{approvalCount}</em>}</NavLink>)}</nav>
      <div className="sidebar-foot"><div className="avatar">{initials}</div><div><b>{user?.display_name || (engineering.dataMode === "demo" ? "Demo reviewer" : "Not signed in")}</b><span>{user ? user.roles.map(titleCase).join(" · ") : engineering.dataMode === "demo" ? "Synthetic session" : "Local account required"}</span></div>{user && <button type="button" className="sidebar-logout" onClick={() => void engineering.logout()} aria-label="Sign out of local backend"><LogOut aria-hidden="true" /></button>}</div>
    </aside>
    <main tabIndex={0} aria-label="Application content">
      <Routes>
        <Route path="/" element={<City />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/tasks" element={<EngineeringWorkspacePage initialView="tasks" />} />
        <Route path="/findings" element={<EngineeringWorkspacePage initialView="findings" />} />
        <Route path="/approvals" element={<EngineeringWorkspacePage initialView="approvals" />} />
        <Route path="/workspace" element={<EngineeringWorkspacePage initialView="requirements" />} />
        <Route path="/workspace/:view" element={<WorkspaceRoute />} />
        <Route path="/reports" element={<ReleaseCenterPage initialView="reports" />} />
        <Route path="/release" element={<ReleaseCenterPage initialView="overview" />} />
        <Route path="/release/:view" element={<ReleaseRoutePage />} />
        <Route path="/agents" element={<OperationsCenterPage view="agents" />} />
        <Route path="/audit" element={<OperationsCenterPage view="audit" />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<SimplePage />} />
      </Routes>
    </main>
    {!isCityHome && !isWorkspaceShell && !isReleaseShell && !isOperationsShell && <ActivityPanel />}
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
  "hmi",
  "alarms",
  "tests",
  "documentation",
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
  const [searchParams] = useSearchParams();
  const initialRecordId = searchParams.get("record");
  const initialEvidenceId = searchParams.get("evidence");
  const source = useMemo(() => workspaceSourceFromEngineering(engineering), [engineering]);
  const operations = useMemo(
    () => workspaceOperationsFromEngineering(engineering, {
      openCityLocation: (semanticLocation) => {
        navigate(`/?location=${encodeURIComponent(semanticLocation)}`);
      },
      openReleaseCenter: () => navigate("/release")
    }),
    [engineering, navigate]
  );
  return (
    <Suspense fallback={<FeatureLoading label="engineering workspace" />}>
      <EngineeringWorkspace
        source={source}
        initialView={initialView}
        initialRecordId={initialRecordId}
        initialEvidenceId={initialEvidenceId}
        operations={operations}
        onReturnToCity={() => navigate("/")}
      />
    </Suspense>
  );
}

const RELEASE_VIEWS = new Set<ReleaseCenterView>(["overview", "artifacts", "reports"]);

function ReleaseRoutePage() {
  const { view } = useParams();
  const initialView = view && RELEASE_VIEWS.has(view as ReleaseCenterView)
    ? view as ReleaseCenterView
    : "overview";
  return <ReleaseCenterPage key={initialView} initialView={initialView} />;
}

function ReleaseCenterPage({ initialView }: { initialView: ReleaseCenterView }) {
  const engineering = useEngineeringData();
  const navigate = useNavigate();
  const source = useMemo(() => releaseSourceFromEngineering(engineering), [engineering]);
  const operations = useMemo(
    () => releaseOperationsFromEngineering({
      openCityLocation: (semanticLocation) => {
        navigate(`/?location=${encodeURIComponent(semanticLocation)}`);
      }
    }),
    [navigate]
  );
  return (
    <Suspense fallback={<FeatureLoading label="Release Center" />}>
      <ReleaseCenter
        source={source}
        operations={operations}
        initialView={initialView}
        onReturnToCity={() => navigate("/")}
      />
    </Suspense>
  );
}

function OperationsCenterPage({ view }: { view: OperationsView }) {
  const engineering = useEngineeringData();
  const navigate = useNavigate();
  const source = useMemo(() => operationsSourceFromEngineering(engineering), [engineering]);
  return (
    <Suspense fallback={<FeatureLoading label={view === "agents" ? "agent operations" : "Audit Trail"} />}>
      <OperationsCenter
        source={source}
        view={view}
        onOpenCityLocation={(semanticLocation) => navigate(`/?location=${encodeURIComponent(semanticLocation)}`)}
      />
    </Suspense>
  );
}

function SettingsPage() {
  return <section className="page settings-page"><PageHeader eyebrow="LOCAL OPERATIONS" title="Settings" /><div className="settings-page__grid"><ConnectionCenter /><DesktopCenter /></div></section>;
}

function PageHeader({ eyebrow, title, action, onAction }: { eyebrow: string; title: string; action?: string; onAction?: () => void }) {
  return <div className="page-header"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1></div>{action && <button className="primary" type="button" onClick={onAction} disabled={!onAction}><Plus />{action}</button>}</div>;
}
interface ProjectCardView {
  id: string;
  projectId: string;
  name: string;
  customer: string;
  status: string;
  progress: number | null;
  openFindings: number | null;
  pendingApprovals: number | null;
  lastActivity: string;
}

function Projects() {
  const engineering = useEngineeringData();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const liveProject = engineering.project;
  const allProjects = useMemo<ProjectCardView[]>(() => engineering.projects.map((project) => {
        const detail = liveProject?.id === project.id ? liveProject : null;
        const progress = detail?.counts.tasks
          ? Math.round((detail.counts.tasks_completed / detail.counts.tasks) * 100)
          : null;
        return {
          id: project.client_reference || project.id.slice(0, 8),
          projectId: project.id,
          name: project.name,
          customer: project.description || "Local engineering project",
          status: titleCase(project.status),
          progress,
          openFindings: detail?.counts.findings_open ?? null,
          pendingApprovals: detail?.counts.approvals_pending ?? null,
          lastActivity: new Date(project.updated_at).toLocaleString()
        };
      }), [engineering.projects, liveProject]);
  const statuses = useMemo(() => [...new Set(allProjects.map((project) => project.status))].sort(), [allProjects]);
  const projectTotal = Math.max(engineering.projectTotal, allProjects.length);
  const displayedProjects = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return allProjects.filter((project) => {
      const matchesQuery = !normalized || `${project.id} ${project.name} ${project.customer} ${project.status}`.toLowerCase().includes(normalized);
      return matchesQuery && (status === "all" || project.status === status);
    });
  }, [allProjects, query, status]);
  const unsupported = engineering.parsers.filter((parser) => !parser.available);
  const openProject = (projectId: string) => {
    if (engineering.dataMode !== "demo") engineering.selectProject(projectId);
    navigate("/workspace/requirements");
  };
  return <section className="page"><PageHeader eyebrow="PORTFOLIO" title="Engineering projects" action="New project" onAction={() => navigate("/workspace/intake")} /><div className="filters"><div className="search"><Search aria-hidden="true" /><input aria-label="Search projects" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search projects" /></div><label className="project-status-filter"><Filter aria-hidden="true" /><span className="sr-only">Filter project status</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All statuses</option>{statuses.map((value) => <option value={value} key={value}>{value}</option>)}</select></label><output>{displayedProjects.length} matching · {allProjects.length} loaded{projectTotal > allProjects.length ? ` of ${projectTotal}` : ""}</output></div>{engineering.dataMode === "live" && <p className="advisory wide"><ShieldAlert />{engineering.parsers.length - unsupported.length} parser kinds available. {unsupported.length ? `${unsupported.length} unsupported kind(s): ${unsupported.map((item) => item.file_kind).join(", ")}.` : "Uploaded formats can be checked locally before review."}{projectTotal > allProjects.length ? ` Portfolio loading is capped at ${allProjects.length.toLocaleString()} of ${projectTotal.toLocaleString()} authorized projects.` : ""}</p>}<div className="project-grid">{displayedProjects.map((project) => <article className="project-card" key={project.projectId}><div className="project-card-head"><span>{project.id}</span><em className={project.progress === 100 ? "done" : ""}>{project.status}</em></div><h3>{project.name}</h3><p>{project.customer}</p><div className="progress" aria-label={project.progress === null ? "Progress not loaded" : `${project.progress}% complete`}><i style={{ width: `${project.progress ?? 0}%` }} /></div><div className="card-stats"><span><b>{project.progress === null ? "—" : `${project.progress}%`}</b> complete</span><span><b>{project.openFindings ?? "—"}</b> findings</span><span><b>{project.pendingApprovals ?? "—"}</b> approvals</span></div><footer><Clock3 aria-hidden="true" /> Updated {project.lastActivity}<button type="button" onClick={() => openProject(project.projectId)}>Open workspace →</button></footer></article>)}</div>{displayedProjects.length === 0 && <div className="empty-state"><FolderKanban /><h2>{allProjects.length ? "No matching projects" : "No project data available"}</h2><p>{allProjects.length ? "Change the search or status filter. Authorized projects remain unchanged." : engineering.fallbackReason}</p></div>}</section>;
}
function SimplePage() { return <section className="page"><PageHeader eyebrow="WORKSPACE" title="Module overview" /><div className="empty-state"><Activity /><h2>Operational module</h2><p>This module is available from the project workspace once the backend contract is connected.</p></div></section>; }
function ActivityPanel() {
  const engineering = useEngineeringData();
  const navigate = useNavigate();
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
  return <aside className="activity-panel"><div className="activity-head"><div><span className="eyebrow">OPERATIONS</span><h2>{activityTitle}</h2></div><button type="button" aria-label="Open Audit Trail filters" onClick={() => navigate("/audit")}><Filter aria-hidden="true" /></button></div><div className="agent-summary"><span><b>{active}</b> active</span><span><b>{waiting}</b> waiting</span><span><b>{engineering.agents.filter((agent) => agent.status === "blocked").length}</b> blocked</span></div><div className="panel-section"><h3>Active agents <span>{visibleAgents.length}</span></h3>{visibleAgents.slice(0,4).map(agent=><div className="agent-row" key={agent.id}><div className={`agent-icon ${agent.state}`}>{agent.name.slice(0,2).toUpperCase()}</div><div><b>{agent.name}</b><span>{agent.task}</span></div><i /></div>)}</div><div className="panel-section"><h3>Recent events</h3>{recentEvents.map(([eventText,time,type])=><div className="event" key={`${eventText}-${time}`}><i className={type}/><div><b>{eventText}</b><span>{time}</span></div></div>)}</div>{pendingApprovals > 0 && <div className="warning-box"><AlertTriangle /><div><b>{pendingApprovals} approval{pendingApprovals === 1 ? "" : "s"} need attention</b><span>Human decisions are required before release.</span></div></div>}</aside>;
}

export function App() { return <Shell />; }
