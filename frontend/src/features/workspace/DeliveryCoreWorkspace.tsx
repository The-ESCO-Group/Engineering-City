import {
  BellRing,
  BookOpen,
  ExternalLink,
  FlaskConical,
  Link as LinkIcon,
  Monitor,
  Search,
  ShieldAlert
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type {
  WorkspaceAlarm,
  WorkspaceCapability,
  WorkspaceDocumentRecord,
  WorkspaceFinding,
  WorkspaceHmiScreen,
  WorkspaceTask,
  WorkspaceTestProcedure,
  WorkspaceView
} from "./types";
import "./DeliveryCoreWorkspace.css";

type DeliveryView = Extract<WorkspaceView, "hmi" | "alarms" | "tests" | "documentation">;

interface DeliveryCoreWorkspaceProps {
  view: DeliveryView;
  capability: WorkspaceCapability;
  hmiScreens: WorkspaceHmiScreen[];
  alarms: WorkspaceAlarm[];
  tests: WorkspaceTestProcedure[];
  documents: WorkspaceDocumentRecord[];
  tasks: WorkspaceTask[];
  findings: WorkspaceFinding[];
  onOpenReleaseCenter?: () => void;
}

const VIEW_META = {
  hmi: {
    eyebrow: "HMI DISTRICT",
    title: "HMI engineering",
    description: "Screens, approved templates, navigation, bindings, and tag-reference validity.",
    icon: Monitor,
    taskKinds: ["hmi"],
    findingTerms: ["hmi", "screen", "display", "binding"]
  },
  alarms: {
    eyebrow: "ALARM CENTER",
    title: "Alarm engineering",
    description: "Trigger, severity, acknowledgement, timing, operator response, and cross-discipline coverage.",
    icon: BellRing,
    taskKinds: ["alarm", "hmi"],
    findingTerms: ["alarm", "trigger", "acknowledgement"]
  },
  tests: {
    eyebrow: "TESTING GROUNDS",
    title: "IAT and FAT procedures",
    description: "Evidence-backed test conditions, actions, expected results, safety precautions, and human sign-off.",
    icon: FlaskConical,
    taskKinds: ["test", "qa", "commission"],
    findingTerms: ["test", "fat", "iat", "verification"]
  },
  documentation: {
    eyebrow: "DOCUMENTATION QUARTER",
    title: "Engineering documentation",
    description: "Revision-bound reports, test documents, audit exports, and their human-approval state.",
    icon: BookOpen,
    taskKinds: ["document", "report", "release"],
    findingTerms: ["document", "report", "release"]
  }
} as const;

function includesAny(value: string, terms: readonly string[]): boolean {
  const normalized = value.toLowerCase();
  return terms.some((term) => normalized.includes(term));
}

function statusTone(value: string): string {
  const normalized = value.toLowerCase();
  if (/(blocked|unresolved|failed|critical|high)/.test(normalized)) return "danger";
  if (/(await|review|proposed|draft|medium|queued)/.test(normalized)) return "warning";
  if (/(valid|ready|generated|complete|accepted|current)/.test(normalized)) return "success";
  return "neutral";
}

function EmptyCanonicalState({ capability, relatedTasks, relatedFindings }: {
  capability: WorkspaceCapability;
  relatedTasks: WorkspaceTask[];
  relatedFindings: WorkspaceFinding[];
}) {
  return (
    <div className="delivery-empty">
      <div className="delivery-empty__explanation">
        <ShieldAlert aria-hidden="true" />
        <div>
          <b>Canonical records are not published by the current local contract.</b>
          <p>{capability.reason}</p>
        </div>
      </div>
      <div className="delivery-related" aria-label="Related contracted activity">
        <h3>Related contracted activity</h3>
        {relatedTasks.length === 0 && relatedFindings.length === 0 ? (
          <p>No related task or finding has been reported for this project.</p>
        ) : (
          <>
            {relatedTasks.map((task) => (
              <article key={task.id}>
                <span className={`delivery-status ${statusTone(task.state)}`}>{task.state.replaceAll("_", " ")}</span>
                <b>{task.title}</b>
                <small>{task.agent || "Agent not assigned"} · {task.semanticLocation?.replaceAll("_", " ") || "Location not reported"}</small>
              </article>
            ))}
            {relatedFindings.map((finding) => (
              <article key={finding.id}>
                <span className={`delivery-status ${statusTone(finding.severity)}`}>{finding.severity}</span>
                <b>{finding.title}</b>
                <small>{finding.evidenceCount} evidence reference{finding.evidenceCount === 1 ? "" : "s"} · {finding.status}</small>
              </article>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

function HmiDetail({ item }: { item: WorkspaceHmiScreen }) {
  return <>
    <DetailHeader title={item.name} subtitle={`${item.displayType} · ${item.resolution}`} status={item.validation} />
    <FactGrid facts={[["Template", item.template], ["Approval", item.approvalState], ["Source", item.source]]} />
    <section className="delivery-detail__section"><h3>Navigation</h3><p><b>From:</b> {item.navigationFrom.join(", ") || "Not reported"}</p><p><b>To:</b> {item.navigationTo.join(", ") || "Not reported"}</p></section>
    <section className="delivery-detail__section"><h3>Bindings</h3>{item.bindings.map((binding) => <article className="delivery-binding" key={binding.id}><span className={`delivery-status ${statusTone(binding.status)}`}>{binding.status}</span><b>{binding.object} · {binding.property}</b><code>{binding.tag}</code><small>{binding.evidence}</small></article>)}</section>
  </>;
}

function AlarmDetail({ item }: { item: WorkspaceAlarm }) {
  return <>
    <DetailHeader title={item.alarmId} subtitle={item.message} status={`${item.severity} · ${item.validation}`} />
    <FactGrid facts={[["Trigger", item.trigger], ["Acknowledgement", item.acknowledgement], ["Delay / deadband", `${item.delay} / ${item.deadband}`], ["Screen", item.linkedScreen], ["Device", item.linkedDevice], ["Source", item.source]]} />
    <section className="delivery-detail__section"><h3>Operator response</h3><p>{item.operatorResponse}</p></section>
    <section className="delivery-detail__section"><h3>Linked tests</h3><p>{item.linkedTests.join(", ") || "No linked test is published."}</p></section>
  </>;
}

function TestDetail({ item }: { item: WorkspaceTestProcedure }) {
  return <>
    <DetailHeader title={`${item.testId} · ${item.title}`} subtitle={item.testType} status={item.status} />
    <FactGrid facts={[["Approval", item.approvalState], ["Requirements", item.requirementLinks.join(", ") || "None"], ["Findings", item.findingLinks.join(", ") || "None"]]} />
    <section className="delivery-detail__section"><h3>Prerequisites</h3><ul>{item.prerequisites.map((value) => <li key={value}>{value}</li>)}</ul><h3>Initial conditions</h3><ul>{item.initialConditions.map((value) => <li key={value}>{value}</li>)}</ul></section>
    <section className="delivery-detail__section delivery-safety"><h3><ShieldAlert aria-hidden="true" /> Safety precautions</h3><ul>{item.safetyPrecautions.map((value) => <li key={value}>{value}</li>)}</ul></section>
    <section className="delivery-detail__section"><h3>Procedure</h3><ol className="delivery-steps">{item.steps.map((step) => <li key={step.sequence}><b>{step.action}</b><span>Expected: {step.expectedResult}</span><small>Evidence: {step.evidenceRequired}</small></li>)}</ol></section>
  </>;
}

function DocumentDetail({ item, onOpenReleaseCenter }: { item: WorkspaceDocumentRecord; onOpenReleaseCenter?: () => void }) {
  return <>
    <DetailHeader title={item.title} subtitle={`${item.documentType} · ${item.format}`} status={item.status} />
    <FactGrid facts={[["Revision", item.revision === null ? "Not published" : String(item.revision)], ["Evidence", item.evidenceCount === null ? "Not reported" : `${item.evidenceCount} references`], ["Approval", item.approvalState], ["Generated", item.generatedAt ? new Date(item.generatedAt).toLocaleString() : "Not generated"], ["Source", item.source]]} />
    <section className="delivery-detail__section"><h3>Artifact access</h3><p>Downloads remain in Release Center and are enabled only when the local API supplies a real authorized artifact.</p><button className="delivery-primary-action" type="button" disabled={!onOpenReleaseCenter} onClick={onOpenReleaseCenter}><ExternalLink aria-hidden="true" /> Open Release Center</button></section>
  </>;
}

function DetailHeader({ title, subtitle, status }: { title: string; subtitle: string; status: string }) {
  return <header className="delivery-detail__header"><div><span className="delivery-eyebrow">SELECTED RECORD</span><h2>{title}</h2><p>{subtitle}</p></div><span className={`delivery-status ${statusTone(status)}`}>{status}</span></header>;
}

function FactGrid({ facts }: { facts: Array<[string, string]> }) {
  return <dl className="delivery-facts">{facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

export function DeliveryCoreWorkspace({ view, capability, hmiScreens, alarms, tests, documents, tasks, findings, onOpenReleaseCenter }: DeliveryCoreWorkspaceProps) {
  const meta = VIEW_META[view];
  const Icon = meta.icon;
  const [query, setQuery] = useState("");
  const items = view === "hmi" ? hmiScreens : view === "alarms" ? alarms : view === "tests" ? tests : documents;
  const filtered = useMemo(() => items.filter((item) => JSON.stringify(item).toLowerCase().includes(query.trim().toLowerCase())), [items, query]);
  const [selectedId, setSelectedId] = useState<string | null>(items[0]?.id ?? null);
  useEffect(() => {
    setQuery("");
    setSelectedId(items[0]?.id ?? null);
  }, [view, items]);
  const selected = items.find((item) => item.id === selectedId) ?? filtered[0] ?? null;
  const relatedTasks = tasks.filter((task) => includesAny(`${task.kind} ${task.title}`, meta.taskKinds));
  const relatedFindings = findings.filter((finding) => includesAny(`${finding.category} ${finding.title} ${finding.discipline}`, meta.findingTerms));

  return (
    <section className="delivery-core">
      <header className="delivery-core__header">
        <div className="delivery-core__identity"><span><Icon aria-hidden="true" /></span><div><small>{meta.eyebrow}</small><h2>{meta.title}</h2><p>{meta.description}</p></div></div>
        <div className="delivery-core__metrics"><span><b>{items.length}</b> records</span><span><b>{relatedTasks.length}</b> tasks</span><span><b>{relatedFindings.length}</b> findings</span></div>
      </header>
      {capability.level !== "available" && <div className="delivery-capability"><ShieldAlert aria-hidden="true" /><div><b>{capability.level === "summary_only" ? "Contracted activity only" : "Delivery data unavailable"}</b><span>{capability.reason}</span></div></div>}
      {items.length === 0 ? <EmptyCanonicalState capability={capability} relatedTasks={relatedTasks} relatedFindings={relatedFindings} /> : (
        <div className="delivery-core__body">
          <aside className="delivery-records" aria-label={`${meta.title} records`}>
            <label><Search aria-hidden="true" /><span className="sr-only">Search {meta.title}</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`Search ${meta.title.toLowerCase()}`} /></label>
            <span className="delivery-records__count">{filtered.length} of {items.length}</span>
            <div>
              {filtered.map((item) => {
                const primary = "name" in item ? item.name : "alarmId" in item ? item.alarmId : "testId" in item ? item.testId : item.title;
                const secondary = "displayType" in item ? item.displayType : "message" in item ? item.message : "testType" in item ? item.testType : item.documentType;
                const status = "validation" in item ? item.validation : item.status;
                return <button key={item.id} type="button" className={selected?.id === item.id ? "active" : ""} onClick={() => setSelectedId(item.id)} aria-pressed={selected?.id === item.id}><span className={`delivery-record-marker ${statusTone(status)}`} /><b>{primary}</b><small>{secondary}</small><em>{status}</em></button>;
              })}
              {filtered.length === 0 && <p className="delivery-records__empty">No records match “{query}”.</p>}
            </div>
          </aside>
          <article className="delivery-detail">
            {selected && view === "hmi" && <HmiDetail item={selected as WorkspaceHmiScreen} />}
            {selected && view === "alarms" && <AlarmDetail item={selected as WorkspaceAlarm} />}
            {selected && view === "tests" && <TestDetail item={selected as WorkspaceTestProcedure} />}
            {selected && view === "documentation" && <DocumentDetail item={selected as WorkspaceDocumentRecord} onOpenReleaseCenter={onOpenReleaseCenter} />}
            <footer><LinkIcon aria-hidden="true" /> Records remain revision-bound proposals until the backend reports validation and named human approval.</footer>
          </article>
        </div>
      )}
    </section>
  );
}
