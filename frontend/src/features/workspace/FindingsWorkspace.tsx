import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronRight,
  CircleHelp,
  Filter,
  GitPullRequestArrow,
  Link2,
  MessageSquareText,
  Search,
  SearchX,
  ShieldAlert,
  UserRoundCheck
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { mergeFindingDetail } from "./adapters";
import { EvidenceViewer } from "./EvidenceViewer";
import type { FindingDetail } from "../../api/types";
import type { WorkspaceCapability, WorkspaceFinding } from "./types";

export interface FindingsWorkspaceProps {
  findings: WorkspaceFinding[];
  initialFindingId?: string;
  initialEvidenceId?: string;
  capability: WorkspaceCapability;
  loadFindingDetail?: (findingId: string) => Promise<FindingDetail>;
  onOpenChangeset?: (changesetId: string) => void;
  onOpenApprovals?: (findingId: string) => void;
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function FindingsWorkspace({ findings, initialFindingId, initialEvidenceId, capability, loadFindingDetail, onOpenChangeset, onOpenApprovals }: FindingsWorkspaceProps) {
  const [query, setQuery] = useState("");
  const [severity, setSeverity] = useState("all");
  const [status, setStatus] = useState("all");
  const initialMatch = Boolean(initialFindingId && findings.some((item) => item.id === initialFindingId));
  const [selectedId, setSelectedId] = useState<string | null>(() => initialMatch ? initialFindingId! : initialFindingId ? null : findings[0]?.id ?? null);
  const [renderLimit, setRenderLimit] = useState(200);
  const [details, setDetails] = useState<Map<string, WorkspaceFinding>>(() => new Map());
  const [loadState, setLoadState] = useState<"idle" | "loading" | "error">("idle");
  const [loadError, setLoadError] = useState("");
  const [loadVersion, setLoadVersion] = useState(0);
  const appliedRouteId = useRef<string | null>(initialMatch ? initialFindingId! : null);
  const lastRouteId = useRef<string | undefined>(initialFindingId);
  const severities = useMemo(() => [...new Set(findings.map((item) => item.severity))].sort(), [findings]);
  const statuses = useMemo(() => [...new Set(findings.map((item) => item.status))].sort(), [findings]);
  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return findings.filter((finding) => {
      const haystack = `${finding.id} ${finding.title} ${finding.category} ${finding.source} ${finding.agent} ${finding.discipline}`.toLowerCase();
      return haystack.includes(normalized) && (severity === "all" || finding.severity === severity) && (status === "all" || finding.status === status);
    });
  }, [findings, query, severity, status]);
  useEffect(() => {
    if (lastRouteId.current !== initialFindingId) {
      lastRouteId.current = initialFindingId;
      appliedRouteId.current = null;
    }
    if (initialFindingId && appliedRouteId.current !== initialFindingId && findings.some((item) => item.id === initialFindingId)) {
      setSelectedId(initialFindingId);
      appliedRouteId.current = initialFindingId;
    }
  }, [findings, initialFindingId]);
  useEffect(() => setRenderLimit(200), [query, severity, status]);
  const requestedMissing = Boolean(initialFindingId && !findings.some((item) => item.id === initialFindingId));
  const rendered = visible.slice(0, renderLimit);
  const baseSelected = requestedMissing ? null : visible.find((item) => item.id === selectedId) ?? visible[0] ?? null;
  const selected = baseSelected ? details.get(baseSelected.id) ?? baseSelected : null;

  useEffect(() => {
    const finding = baseSelected;
    if (!finding || finding.detailLoaded || details.has(finding.id) || !loadFindingDetail) {
      setLoadState("idle");
      return;
    }
    let active = true;
    setLoadError("");
    setLoadState("loading");
    void loadFindingDetail(finding.id).then((detail) => {
      if (!active) return;
      setDetails((current) => new Map(current).set(finding.id, mergeFindingDetail(finding, detail)));
      setLoadState("idle");
    }).catch((error: unknown) => {
      if (!active) return;
      setLoadState("error");
      setLoadError(error instanceof Error ? error.message : "Finding evidence could not be loaded.");
    });
    return () => { active = false; };
  }, [baseSelected, details, loadFindingDetail, loadVersion]);

  const selectFinding = (finding: WorkspaceFinding) => {
    setSelectedId(finding.id);
    setLoadError("");
    if (finding.id === selectedId && loadState === "error") setLoadVersion((current) => current + 1);
  };

  return (
    <section className="eng-findings-workspace" aria-labelledby="findings-title">
      <header className="eng-workspace-header"><div><span className="eng-eyebrow">INDEPENDENT ENGINEERING REVIEW</span><h2 id="findings-title">Findings & evidence</h2><p>Direct facts, bounded inference, expected-but-missing evidence, exact citations, risk, and human disposition.</p></div></header>
      {capability.level !== "available" && <div className="eng-capability-notice"><CircleHelp aria-hidden="true" /><p>{capability.reason}</p></div>}
      <div className="eng-toolbar">
        <label className="eng-search"><Search aria-hidden="true" /><span className="sr-only">Search findings</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search ID, title, source, discipline, or agent…" /></label>
        <label className="eng-select"><Filter aria-hidden="true" /><span className="sr-only">Filter severity</span><select value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="all">All severities</option>{severities.map((item) => <option key={item}>{item}</option>)}</select></label>
        <label className="eng-select"><Filter aria-hidden="true" /><span className="sr-only">Filter status</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All statuses</option>{statuses.map((item) => <option key={item}>{item}</option>)}</select></label>
        <span className="eng-result-count">{visible.length} of {findings.length}</span>
      </div>
      {requestedMissing ? <div className="eng-large-empty" role="alert"><SearchX aria-hidden="true" /><h3>Finding link not found</h3><p>No finding with ID <code>{initialFindingId}</code> exists in the loaded project. No substitute finding is shown.</p></div> : findings.length === 0 ? <div className="eng-large-empty"><SearchX aria-hidden="true" /><h3>No findings recorded</h3><p>No finding is invented when the backend has not reported one.</p></div> : (
        <div className="eng-finding-layout">
          <div className="eng-table-scroll"><table className="eng-data-table eng-finding-table"><thead><tr><th>Severity</th><th>Finding</th><th>Source / exact location</th><th>Agent</th><th>Confidence</th><th>Evidence</th><th>Reviewer / approval</th></tr></thead><tbody>{rendered.map((finding) => {
            const hydrated = details.get(finding.id) ?? finding;
            return <tr key={finding.id} id={`finding-${finding.id}`} className={selected?.id === finding.id ? "selected" : ""}><td><span className={`eng-severity ${finding.severity}`}>{humanize(finding.severity)}</span></td><td><button type="button" className="eng-cell-button" onClick={() => selectFinding(finding)}><b>{finding.id} · {finding.title}</b><small>{humanize(finding.category)} · {humanize(finding.status)}</small></button></td><td>{hydrated.source}<small className="eng-cell-note">{hydrated.exactLocation}</small></td><td>{humanize(finding.agent)}</td><td>{finding.confidence === null ? "Not published" : `${Math.round(finding.confidence * 100)}%`}</td><td>{finding.evidenceCount}</td><td>{finding.reviewer}<small className="eng-cell-note">{finding.approvalStatus}</small></td></tr>;
          })}</tbody></table>{rendered.length < visible.length && <button type="button" className="eng-load-more" onClick={() => setRenderLimit((current) => Math.min(current + 200, visible.length))}>Load 200 more findings <span>{rendered.length} of {visible.length} shown</span></button>}</div>
          {selected && <FindingDetailPanel finding={selected} initialEvidenceId={selected.id === initialFindingId ? initialEvidenceId : undefined} loadState={loadState} loadError={loadError} canLoad={Boolean(loadFindingDetail)} onReload={() => setLoadVersion((current) => current + 1)} onOpenChangeset={onOpenChangeset} onOpenApprovals={onOpenApprovals} />}
        </div>
      )}
    </section>
  );
}

function FindingDetailPanel({ finding, initialEvidenceId, loadState, loadError, canLoad, onReload, onOpenChangeset, onOpenApprovals }: {
  finding: WorkspaceFinding;
  initialEvidenceId?: string;
  loadState: "idle" | "loading" | "error";
  loadError: string;
  canLoad: boolean;
  onReload: () => void;
  onOpenChangeset?: (changesetId: string) => void;
  onOpenApprovals?: (findingId: string) => void;
}) {
  return (
    <article className="eng-finding-detail">
      <header><div><span className={`eng-severity ${finding.severity}`}>{humanize(finding.severity)}</span><span className={`eng-status ${finding.status}`}>{humanize(finding.status)}</span></div><code>{finding.id}</code></header>
      <h3>{finding.title}</h3>
      <p className="eng-finding-meta"><Bot aria-hidden="true" />{humanize(finding.agent)}<span>·</span><UserRoundCheck aria-hidden="true" />{finding.reviewer}</p>
      {finding.requiresApproval && <p className="eng-alert-block"><ShieldAlert aria-hidden="true" />This finding requires a named human decision before dependent release work can proceed.</p>}
      <div className="eng-finding-statements"><Statement title="Observed fact" text={finding.fact} tone="fact" /><Statement title="Engineering inference" text={finding.inference} tone="inference" /><Statement title="Expected but missing" text={finding.expectedMissingEvidence} tone="missing" /></div>
      {(finding.requirementSide || finding.implementationSide) && <section className="eng-discrepancy"><h4>Requirement ↔ implementation discrepancy</h4><div><article><span>Requirement side</span><p>{finding.requirementSide || "No requirement-side statement published."}</p></article><ChevronRight aria-hidden="true" /><article><span>Implementation side</span><p>{finding.implementationSide || "No implementation-side statement published."}</p></article></div></section>}
      <div className="eng-operation-grid"><div><span>Impact</span><p>{finding.impact || "No impact statement published."}</p></div><div><span>Suggested action</span><p>{finding.suggestedAction || "No action published."}</p></div><div><span>Risk</span><p>{finding.risk || "No risk statement published."}</p></div><div><span>Confidence</span><p>{finding.confidence === null ? "Not published" : `${Math.round(finding.confidence * 100)}%`}</p></div></div>
      {finding.parserWarnings.length > 0 && <div className="eng-parser-warnings"><h4><AlertTriangle aria-hidden="true" />Parser warnings</h4>{finding.parserWarnings.map((warning) => <p key={warning}>{warning}</p>)}</div>}
      <section className="eng-limitations"><span>Analysis limitations</span><p>{finding.limitations || "No limitation statement was published."}</p></section>
      {loadState === "loading" && <p className="eng-loading-line" role="status">Loading exact citations from the local backend…</p>}
      {loadState === "error" && <div className="eng-alert-block danger"><AlertTriangle aria-hidden="true" /><div><b>Evidence load failed</b><p>{loadError}</p><button type="button" onClick={onReload}>Retry</button></div></div>}
      {!finding.detailLoaded && loadState === "idle" && <div className="eng-capability-notice"><CircleHelp aria-hidden="true" /><p>{canLoad ? "Select this finding to load exact citations." : "No finding-detail loader is connected; exact citations remain unavailable."}</p></div>}
      <EvidenceViewer
        evidence={finding.evidence}
        initialSelectedId={finding.detailLoaded ? initialEvidenceId : undefined}
        deepLinkFor={(evidenceId) => `#/findings?record=${encodeURIComponent(finding.id)}&evidence=${encodeURIComponent(evidenceId)}`}
      />
      <div className="eng-link-columns"><TextGroup icon={<MessageSquareText />} title="Comments" values={finding.comments} /><TextGroup icon={<CheckCircle2 />} title="History" values={finding.history} /></div>
      <div className="eng-linked-records"><span>Linked records</span>{finding.linkedChangesetId && <button type="button" disabled={!onOpenChangeset} onClick={() => onOpenChangeset?.(finding.linkedChangesetId!)}><GitPullRequestArrow aria-hidden="true" /> Change set {finding.linkedChangesetId}</button>}{finding.linkedTests.map((test) => <code key={test}>{test}</code>)}{!finding.linkedChangesetId && finding.linkedTests.length === 0 && <p>None published.</p>}</div>
      <footer><a href={`#/findings?record=${encodeURIComponent(finding.id)}`}><Link2 aria-hidden="true" /> Finding deep link</a>{finding.requiresApproval && <button type="button" disabled={!onOpenApprovals} onClick={() => onOpenApprovals?.(finding.id)}><ShieldAlert aria-hidden="true" /> Open approval decision</button>}</footer>
    </article>
  );
}

function Statement({ title, text, tone }: { title: string; text: string; tone: string }) {
  return <article className={tone}><span>{title}</span><p>{text || "Not published."}</p></article>;
}

function TextGroup({ icon, title, values }: { icon: ReactNode; title: string; values: string[] }) {
  return <section className="eng-record-list"><h5>{icon}{title}</h5>{values.length ? <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul> : <p>None published.</p>}</section>;
}
