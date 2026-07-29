import { useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { Activity, AlertTriangle, Archive, Bell, Bot, Building2, CheckCircle2, ChevronDown, ClipboardCheck, Clock3, FileText, Filter, FolderKanban, LayoutDashboard, ListChecks, LogOut, Menu, Plus, Search, Settings, ShieldAlert, Upload, Users, Wifi } from "lucide-react";
import { City } from "./components/City";
import { agents, findings, projects, tasks } from "./domain";
import { useUI } from "./store";

const nav = [
  ["Engineering City", "/", Building2], ["Projects", "/projects", FolderKanban], ["Tasks", "/tasks", ListChecks],
  ["Findings", "/findings", ShieldAlert], ["Approvals", "/approvals", ClipboardCheck], ["Reports", "/reports", FileText],
  ["Agents", "/agents", Bot], ["Audit Trail", "/audit", Archive], ["Settings", "/settings", Settings]
] as const;
const tabs = ["Overview", "Files", "Tasks", "Findings", "Evidence", "Agent Messages", "Approvals", "Reports", "Audit"];

function Shell() {
  const [activeTab, setActiveTab] = useState("Overview");
  return <div className="app">
    <header className="topbar">
      <button className="menu"><Menu /></button>
      <div className="brand-mark">E</div><div className="brand"><strong>ESCO</strong><span>ENGINEERING CITY</span></div>
      <div className="project-switch"><span>Current project</span><b>PRJ-0241 · Line 4 Cartoner Upgrade</b><ChevronDown /></div>
      <div className="top-spacer" />
      <span className="demo-badge">DEMO DATA</span>
      <span className="connection"><Wifi /> Local backend <b>Mock mode</b></span>
      <button className="icon-button"><Bell /><i className="notification" /></button>
      <button className="user">NM</button>
    </header>
    <aside className="sidebar">
      <nav aria-label="Primary navigation">{nav.map(([label, to, Icon]) => <NavLink key={label} to={to} end={to === "/"}><Icon /><span>{label}</span>{label === "Findings" && <em>12</em>}{label === "Approvals" && <em>2</em>}</NavLink>)}</nav>
      <div className="sidebar-foot"><div className="avatar">NM</div><div><b>N. Modha</b><span>Engineering reviewer</span></div><LogOut /></div>
    </aside>
    <main>
      <Routes>
        <Route path="/" element={<><City /><BottomTabs active={activeTab} setActive={setActiveTab} /></>} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/tasks" element={<Tasks />} />
        <Route path="/findings" element={<Findings />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/reports" element={<Reports />} />
        <Route path="*" element={<SimplePage />} />
      </Routes>
    </main>
    <ActivityPanel />
  </div>;
}

function BottomTabs({ active, setActive }: { active: string; setActive: (value: string) => void }) {
  return <section className="workspace">
    <div className="tabs">{tabs.map((tab) => <button key={tab} className={active === tab ? "active" : ""} onClick={() => setActive(tab)}>{tab}{tab === "Findings" && <em>12</em>}</button>)}</div>
    <div className="overview-grid">
      <div><span className="eyebrow">PROJECT HEALTH</span><div className="health"><strong>68%</strong><div><b>Engineering review in progress</b><span>3 of 5 discipline reviews active</span></div></div><div className="progress"><i style={{ width: "68%" }} /></div></div>
      <div><span className="eyebrow">OPEN FINDINGS</span><div className="metric-line"><strong>12</strong><span className="critical">1 critical</span><span className="high">3 high</span><span>8 medium / low</span></div></div>
      <div><span className="eyebrow">NEXT GATE</span><div className="gate"><Clock3 /><div><b>Senior engineering review</b><span>Blocked by 2 incomplete tasks</span></div></div></div>
    </div>
  </section>;
}

function PageHeader({ eyebrow, title, action }: { eyebrow: string; title: string; action?: string }) {
  return <div className="page-header"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1></div>{action && <button className="primary"><Plus />{action}</button>}</div>;
}
function Projects() { return <section className="page"><PageHeader eyebrow="PORTFOLIO" title="Engineering projects" action="New project" /><div className="filters"><div className="search"><Search /><input placeholder="Search projects" /></div><button><Filter /> Filters</button></div><div className="project-grid">{projects.map(p => <article className="project-card" key={p.id}><div className="project-card-head"><span>{p.id}</span><em className={p.progress === 100 ? "done" : ""}>{p.status}</em></div><h3>{p.name}</h3><p>{p.customer}</p><div className="progress"><i style={{ width: `${p.progress}%` }} /></div><div className="card-stats"><span><b>{p.progress}%</b> complete</span><span><b>{p.openFindings}</b> findings</span><span><b>{p.pendingApprovals}</b> approvals</span></div><footer><Clock3 /> Updated {p.lastActivity}<button>Open workspace →</button></footer></article>)}</div></section>; }
function Tasks() { return <section className="page"><PageHeader eyebrow="WORKFLOW" title="Task board" /><div className="task-board">{["Queued","Running","Blocked","Awaiting approval","Completed"].map(state => <div className="task-column" key={state}><h3>{state}<span>{tasks.filter(t=>t.state===state).length}</span></h3>{tasks.filter(t=>t.state===state).map(t=><article key={t.name}><div className={`status-bar ${t.state.toLowerCase().replace(" ","-")}`} /><h4>{t.name}</h4><p><Bot />{t.owner}</p><div className="progress"><i style={{width:`${t.progress}%`}} /></div></article>)}</div>)}</div></section>; }
function Findings() {
  const { selectedFinding, selectFinding } = useUI(); const selected = findings.find(f => f.id === selectedFinding) ?? findings[0];
  return <section className="page findings-page"><PageHeader eyebrow="ENGINEERING REVIEW" title="Findings & evidence" /><div className="findings-layout"><div className="table-wrap"><div className="filters"><div className="search"><Search /><input placeholder="Search findings" /></div><button><Filter /> Severity: All</button></div><table><thead><tr><th>Severity</th><th>Finding</th><th>Source</th><th>Agent</th><th>Confidence</th><th>Approval</th></tr></thead><tbody>{findings.map(f=><tr key={f.id} className={selected.id===f.id?"selected":""} onClick={()=>selectFinding(f.id)}><td><span className={`severity ${f.severity.toLowerCase()}`}>{f.severity}</span></td><td><b>{f.id} · {f.title}</b><small>{f.category} · {f.status}</small></td><td>{f.source}<small>{f.location}</small></td><td>{f.agent}</td><td>{Math.round(f.confidence*100)}%</td><td>{f.approval}</td></tr>)}</tbody></table></div><FindingDetail finding={selected} /></div></section>;
}
function FindingDetail({ finding: f }: { finding: typeof findings[number] }) { return <aside className="detail-panel"><div className="detail-title"><span className={`severity ${f.severity.toLowerCase()}`}>{f.severity}</span><span>{f.id}</span></div><h2>{f.title}</h2><p className="risk"><AlertTriangle />{f.risk}</p><Detail label="Direct evidence" text={f.evidence} /><div className="evidence-compare"><Detail label="Requirement evidence" text={f.requirement} /><Detail label="Implementation evidence" text={`${f.source} · ${f.location}`} /></div><Detail label="Suggested correction" text={f.suggestion} /><div className="confidence"><span>Agent confidence</span><b>{Math.round(f.confidence*100)}%</b><div className="progress"><i style={{width:`${f.confidence*100}%`}} /></div></div><p className="advisory"><ShieldAlert />AI output is advisory. Engineering responsibility remains with the authorized human reviewer.</p><div className="button-row"><button>Request clarification</button><button className="primary">Review finding</button></div></aside>; }
function Detail({label,text}:{label:string;text:string}) { return <div className="detail"><span className="eyebrow">{label}</span><p>{text}</p></div>; }
function Approvals() { const [decision,setDecision]=useState(""); return <section className="page"><PageHeader eyebrow="HUMAN AUTHORITY" title="Approval inbox" /><div className="approval-layout"><article className="approval-list"><span className="severity critical">Critical</span><h3>Corrective action for guard reset logic</h3><p>PRJ-0241 · Finding F-104 · Requested by Senior Review</p><div><Clock3 /> Due today <span>Assigned to N. Modha</span></div></article><article className="approval-decision"><span className="eyebrow">DECISION REQUIRED</span><h2>Review corrective action plan</h2><p>The proposed change affects a safety-related reset sequence. Confirm the evidence and validation plan before deciding.</p><label>Decision<select value={decision} onChange={e=>setDecision(e.target.value)}><option value="">Select decision…</option><option>Accept</option><option>Reject</option><option>Request clarification</option></select></label><label>Engineering rationale (required)<textarea placeholder="Record the evidence considered and basis for this decision…" /></label><button className="primary" disabled={!decision}>Submit signed decision</button><p className="advisory"><ShieldAlert />This decision is recorded in the audit trail. AI analysis does not constitute engineering approval.</p></article></div></section>; }
function Reports() { const download=(name:string)=>{const blob=new Blob([JSON.stringify({project:"PRJ-0241",report:name,notice:"Synthetic demo data only"},null,2)],{type:"application/json"});const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=`${name.toLowerCase().replaceAll(" ","-")}.json`;a.click();URL.revokeObjectURL(a.href)}; return <section className="page"><PageHeader eyebrow="DELIVERY" title="Reports & exports" /><div className="report-grid">{["Detailed discrepancy report","Executive summary","Findings register","FAT test plan","Audit export"].map((r,i)=><article className="report-card" key={r}><FileText /><div><span>{["PDF · HTML","DOCX · PDF","XLSX · CSV","DOCX · PDF","JSON · CSV"][i]}</span><h3>{r}</h3><p>Generated from synthetic evidence · Version 0.{i+3}</p></div><button onClick={()=>download(r)}>Generate demo export</button></article>)}</div><p className="advisory wide"><ShieldAlert />Demo exports are clearly marked and contain no real engineering analysis. Real artifact generation requires the backend ReportProvider contract.</p></section>; }
function SimplePage() { return <section className="page"><PageHeader eyebrow="WORKSPACE" title="Module overview" /><div className="empty-state"><Activity /><h2>Operational module</h2><p>This module is available from the project workspace once the backend contract is connected.</p></div></section>; }
function ActivityPanel() { return <aside className="activity-panel"><div className="activity-head"><div><span className="eyebrow">OPERATIONS</span><h2>Live activity</h2></div><button><Filter /></button></div><div className="agent-summary"><span><b>3</b> active</span><span><b>1</b> waiting</span><span><b>0</b> failed</span></div><div className="panel-section"><h3>Active agents <span>{agents.length}</span></h3>{agents.slice(0,4).map(a=><div className="agent-row" key={a.id}><div className={`agent-icon ${a.state}`}>{a.name.slice(0,2).toUpperCase()}</div><div><b>{a.name}</b><span>{a.task}</span></div><i /></div>)}</div><div className="panel-section"><h3>Recent events</h3>{[["F-104 escalated for review","12:42","warning"],["HMI tag scan 56% complete","12:39","active"],["Electrical evidence indexed","12:34","done"],["Senior review waiting","12:31","waiting"]].map(([text,time,type])=><div className="event" key={text}><i className={type}/><div><b>{text}</b><span>{time}</span></div></div>)}</div><div className="warning-box"><AlertTriangle /><div><b>2 approvals need attention</b><span>One critical decision is due today.</span></div></div></aside>; }

export function App() { return <Shell />; }
