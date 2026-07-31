import {
  AlertTriangle,
  ArrowRight,
  Bot,
  Check,
  CheckCircle2,
  CircleHelp,
  Clock3,
  ExternalLink,
  Filter,
  GitCompareArrows,
  GitCommitHorizontal,
  ListChecks,
  MapPin,
  MessageSquareText,
  Search,
  SearchX,
  ShieldAlert,
  X
} from "lucide-react";
import { useMemo, useState } from "react";
import { EvidenceViewer } from "./EvidenceViewer";
import type {
  WorkspaceCapability,
  WorkspaceChangeOperation,
  WorkspaceChangeSet,
  WorkspaceOperations,
  WorkspacePlan,
  WorkspaceRevision
} from "./types";

export type ChangeReviewView = "planning" | "changesets" | "revisions";

export interface ChangeReviewWorkspaceProps {
  view: ChangeReviewView;
  plans: WorkspacePlan[];
  changesets: WorkspaceChangeSet[];
  revisions: WorkspaceRevision[];
  changesetCapability: WorkspaceCapability;
  revisionCapability: WorkspaceCapability;
  operations?: WorkspaceOperations;
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

export function ChangeReviewWorkspace(props: ChangeReviewWorkspaceProps) {
  if (props.view === "planning") return <PlanningView plans={props.plans} onOpenCityLocation={props.operations?.openCityLocation} />;
  if (props.view === "revisions") return <RevisionView revisions={props.revisions} capability={props.revisionCapability} onCompare={props.operations?.compareRevisions} />;
  return <ChangeSetsView changesets={props.changesets} capability={props.changesetCapability} operations={props.operations} />;
}

function PlanningView({ plans, onOpenCityLocation }: { plans: WorkspacePlan[]; onOpenCityLocation?: (location: string) => void }) {
  return (
    <section className="eng-plan-workspace" aria-labelledby="plans-title">
      <header className="eng-workspace-header"><div><span className="eng-eyebrow">PROJECT MANAGER OUTPUT</span><h2 id="plans-title">Workflow planning</h2><p>Published scope counts, risks, exclusions, and clarification blockers. Task details stay in the task workspace.</p></div></header>
      {plans.length === 0 ? <div className="eng-large-empty"><SearchX aria-hidden="true" /><h3>No workflow plan published</h3><p>The event stream has not provided a workflow.planned summary.</p></div> : (
        <div className="eng-plan-list">{plans.map((plan) => (
          <article key={plan.id}>
            <header><div><span className="eng-eyebrow">RUN {plan.runId?.slice(0, 8) || "UNASSIGNED"}</span><h3>{plan.summary}</h3></div><span><Clock3 aria-hidden="true" />{formatTime(plan.occurredAt)}</span></header>
            <div className="eng-plan-stats"><div><b>{plan.taskCount}</b><span>planned tasks</span></div><div className={plan.riskCount ? "attention" : ""}><b>{plan.riskCount}</b><span>declared risks</span></div><div><b>{plan.outOfScopeCount}</b><span>out of scope</span></div><div className={plan.blockers.length ? "attention" : ""}><b>{plan.blockers.length}</b><span>blocking questions</span></div></div>
            <div className="eng-plan-body"><div><h4><ListChecks aria-hidden="true" />Work by discipline</h4>{Object.keys(plan.taskCountsByKind).length ? <ul>{Object.entries(plan.taskCountsByKind).map(([kind, count]) => <li key={kind}><span>{humanize(kind)}</span><b>{count}</b></li>)}</ul> : <p>Task kind counts were not published.</p>}</div><div><h4><ShieldAlert aria-hidden="true" />Clarification blockers</h4>{plan.blockers.length ? <ul>{plan.blockers.map((blocker) => <li key={blocker}><code>{blocker}</code></li>)}</ul> : <p>No blocking question reference was published.</p>}</div></div>
            <footer><span><MapPin aria-hidden="true" />{humanize(plan.location)}</span><button type="button" disabled={!onOpenCityLocation} onClick={() => onOpenCityLocation?.(plan.location)} title={!onOpenCityLocation ? "City navigation is not connected." : undefined}><ExternalLink aria-hidden="true" /> Focus planning district</button></footer>
          </article>
        ))}</div>
      )}
    </section>
  );
}

function ChangeSetsView({ changesets, capability, operations }: { changesets: WorkspaceChangeSet[]; capability: WorkspaceCapability; operations?: WorkspaceOperations }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [selectedId, setSelectedId] = useState<string | null>(changesets[0]?.id ?? null);
  const statuses = useMemo(() => [...new Set(changesets.map((item) => item.status))].sort(), [changesets]);
  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return changesets.filter((changeset) => `${changeset.title} ${changeset.author} ${changeset.id}`.toLowerCase().includes(normalized) && (status === "all" || changeset.status === status));
  }, [changesets, query, status]);
  const selected = visible.find((item) => item.id === selectedId) ?? visible[0] ?? null;
  return (
    <section className="eng-changeset-workspace" aria-labelledby="changesets-title">
      <header className="eng-workspace-header"><div><span className="eng-eyebrow">STRUCTURED HUMAN REVIEW</span><h2 id="changesets-title">Change sets & operation diffs</h2><p>Before, proposed after, evidence, impact, risk, validation, and reviewer authority.</p></div></header>
      {capability.level !== "available" && <div className="eng-capability-notice"><CircleHelp aria-hidden="true" /><p>{capability.reason}</p></div>}
      <div className="eng-toolbar"><label className="eng-search"><Search aria-hidden="true" /><span className="sr-only">Search change sets</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search title, ID, or author…" /></label><label className="eng-select"><Filter aria-hidden="true" /><span className="sr-only">Filter change set status</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All statuses</option>{statuses.map((item) => <option key={item}>{item}</option>)}</select></label><span className="eng-result-count">{visible.length} of {changesets.length}</span></div>
      {changesets.length === 0 ? <div className="eng-large-empty"><SearchX aria-hidden="true" /><h3>No change set event published</h3><p>{capability.reason}</p></div> : (
        <div className="eng-change-layout">
          <nav className="eng-change-list" aria-label="Change sets">{visible.map((changeset) => <button type="button" key={changeset.id} className={selected?.id === changeset.id ? "selected" : ""} onClick={() => setSelectedId(changeset.id)}><span><i className={`eng-state-dot ${changeset.status}`} /><b>{changeset.title}</b></span><small>r{changeset.baseRevision} · {changeset.operationCount} operation{changeset.operationCount === 1 ? "" : "s"}</small><em>{humanize(changeset.status)}</em></button>)}</nav>
          {selected && <ChangeSetDetail key={selected.id} changeset={selected} operations={operations} capability={capability} />}
        </div>
      )}
    </section>
  );
}

function ChangeSetDetail({ changeset, operations, capability }: { changeset: WorkspaceChangeSet; operations?: WorkspaceOperations; capability: WorkspaceCapability }) {
  const [selectedOperationId, setSelectedOperationId] = useState<string | null>(changeset.operations[0]?.id ?? null);
  const [selectedOperations, setSelectedOperations] = useState<string[]>(changeset.operations.map((operation) => operation.id));
  const [comment, setComment] = useState("");
  const [reviewState, setReviewState] = useState<"idle" | "busy" | "success" | "error">("idle");
  const [message, setMessage] = useState("");
  const selectedOperation = changeset.operations.find((operation) => operation.id === selectedOperationId) ?? changeset.operations[0] ?? null;
  const canReview = Boolean(operations?.reviewChangeSet && changeset.operationsAvailable);
  const partial = operations?.allowPartialChangeSetReview === true;

  const decide = async (decision: "approve" | "reject" | "request_changes") => {
    if (!operations?.reviewChangeSet) return;
    if ((decision === "reject" || decision === "request_changes") && !comment.trim()) {
      setReviewState("error");
      setMessage("A reviewer comment is required for rejection or requested changes.");
      return;
    }
    setReviewState("busy");
    setMessage("");
    try {
      await operations.reviewChangeSet(
        changeset,
        decision,
        comment.trim(),
        partial && selectedOperations.length < changeset.operations.length ? selectedOperations : undefined
      );
      setReviewState("success");
      setMessage("The review handler recorded the decision.");
    } catch (error) {
      setReviewState("error");
      setMessage(error instanceof Error ? error.message : "The review request failed.");
    }
  };

  return (
    <article className="eng-change-detail">
      <header><div><span className={`eng-status ${changeset.status}`}>{humanize(changeset.status)}</span><h3>{changeset.title}</h3><code>{changeset.id}</code></div><dl><div><dt>Base</dt><dd>r{changeset.baseRevision}</dd></div><div><dt>Confidence</dt><dd>{changeset.confidence === null ? "Not published" : `${Math.round(changeset.confidence * 100)}%`}</dd></div><div><dt>Warnings</dt><dd>{changeset.warningCount}</dd></div></dl></header>
      <div className="eng-change-meta"><span><Bot aria-hidden="true" />{humanize(changeset.author)}</span><span><MapPin aria-hidden="true" />{humanize(changeset.semanticLocation)}</span><span><Clock3 aria-hidden="true" />{formatTime(changeset.createdAt)}</span></div>
      {changeset.requiresHumanApproval && <p className="eng-alert-block"><ShieldAlert aria-hidden="true" />Human approval is required before this proposal can merge.</p>}
      {changeset.restrictedDomains.length > 0 && <p className="eng-alert-block danger"><AlertTriangle aria-hidden="true" />Restricted domain: {changeset.restrictedDomains.map(humanize).join(", ")}</p>}
      <section className="eng-validation-summary"><h4><CheckCircle2 aria-hidden="true" />Validation summary</h4><p>{changeset.validationSummary}</p>{changeset.concerns.map((concern) => <span key={concern}>{concern}</span>)}</section>
      {!changeset.operationsAvailable ? (
        <div className="eng-operations-unavailable"><CircleHelp aria-hidden="true" /><div><h4>Operation-level diff unavailable</h4><p>{capability.reason} The {changeset.operationCount} published operation count is shown, but no before/after values are invented.</p></div></div>
      ) : (
        <div className="eng-operation-layout">
          <nav aria-label="Change operations">{changeset.operations.map((operation) => <div key={operation.id} className={selectedOperation?.id === operation.id ? "selected" : ""}>{partial && <input type="checkbox" aria-label={`Include ${operation.objectLabel}`} checked={selectedOperations.includes(operation.id)} onChange={(event) => setSelectedOperations((current) => event.target.checked ? [...current, operation.id] : current.filter((id) => id !== operation.id))} />}<button type="button" onClick={() => setSelectedOperationId(operation.id)}><span>{humanize(operation.type)}</span><b>{operation.objectLabel}</b><small>{humanize(operation.objectType)} · {operation.field}</small></button></div>)}</nav>
          {selectedOperation && <OperationDetail operation={selectedOperation} />}
        </div>
      )}
      <section className="eng-review-decision"><h4><MessageSquareText aria-hidden="true" />Reviewer decision</h4><label>Comment<textarea rows={3} value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Record evidence considered, requested correction, or approval basis…" /></label><div><button type="button" disabled={!canReview || reviewState === "busy"} onClick={() => void decide("approve")}><Check aria-hidden="true" /> Approve{partial && selectedOperations.length < changeset.operations.length ? " selected" : ""}</button><button type="button" disabled={!canReview || reviewState === "busy"} onClick={() => void decide("request_changes")}><MessageSquareText aria-hidden="true" /> Request changes</button><button type="button" className="danger" disabled={!canReview || reviewState === "busy"} onClick={() => void decide("reject")}><X aria-hidden="true" /> Reject</button></div>{!canReview && <p>Review controls are disabled: {changeset.operationsAvailable ? "no backend review handler is connected." : capability.reason}</p>}{message && <p className={reviewState} role="status">{message}</p>}</section>
    </article>
  );
}

function OperationDetail({ operation }: { operation: WorkspaceChangeOperation }) {
  return (
    <section className="eng-operation-detail">
      <header><span>{humanize(operation.type)} · {humanize(operation.objectType)}</span><h4>{operation.objectLabel}</h4><code>{operation.field}</code></header>
      <div className="eng-diff"><div className="before"><span>Before</span><pre>{operation.before || "∅"}</pre></div><ArrowRight aria-hidden="true" /><div className="after"><span>Proposed after</span><pre>{operation.after || "∅"}</pre></div></div>
      <div className="eng-operation-grid"><div><span>Agent rationale</span><p>{operation.rationale || "No rationale published."}</p></div><div><span>Template basis</span><p>{operation.templateBasis || "No template basis published."}</p></div><div><span>Risk</span><p>{operation.risk || "No risk statement published."}</p></div><div><span>Confidence</span><p>{operation.confidence === null ? "Not published" : `${Math.round(operation.confidence * 100)}%`}</p></div></div>
      <section className="eng-impact-list"><h5>Impacted objects</h5>{operation.impactedObjects.length ? <table><thead><tr><th>Discipline</th><th>Object</th><th>Reference</th></tr></thead><tbody>{operation.impactedObjects.map((impact) => <tr key={`${impact.type}-${impact.reference}`}><td>{impact.type}</td><td>{impact.label}</td><td><code>{impact.reference}</code></td></tr>)}</tbody></table> : <p>No impacted object list was published.</p>}</section>
      <div className="eng-link-columns"><TextList title="Validation results" values={operation.validationResults} /><TextList title="Reviewer comments" values={operation.reviewerComments} /></div>
      <EvidenceViewer evidence={operation.sourceEvidence} title="Operation source evidence" />
    </section>
  );
}

function TextList({ title, values }: { title: string; values: string[] }) {
  return <div className="eng-record-list"><h5>{title}</h5>{values.length ? <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul> : <p>None published.</p>}</div>;
}

function RevisionView({ revisions, capability, onCompare }: { revisions: WorkspaceRevision[]; capability: WorkspaceCapability; onCompare?: (base: number, target: number) => Promise<void> }) {
  const sorted = [...revisions].sort((left, right) => right.number - left.number);
  const [base, setBase] = useState<number | "">(sorted[1]?.number ?? "");
  const [target, setTarget] = useState<number | "">(sorted[0]?.number ?? "");
  const [state, setState] = useState("");
  const compare = async () => {
    if (base === "" || target === "" || !onCompare) return;
    setState("Comparing revisions…");
    try { await onCompare(base, target); setState("Revision comparison opened."); }
    catch (error) { setState(error instanceof Error ? error.message : "Revision comparison failed."); }
  };
  return (
    <section className="eng-revision-workspace" aria-labelledby="revisions-title">
      <header className="eng-workspace-header"><div><span className="eng-eyebrow">VERSIONED PROJECT CORE</span><h2 id="revisions-title">Revision history</h2><p>Numbered, immutable project points with parentage, object counts, and producing change set.</p></div></header>
      {capability.level !== "available" && <div className="eng-capability-notice"><CircleHelp aria-hidden="true" /><p>{capability.reason}</p></div>}
      <div className="eng-revision-compare"><GitCompareArrows aria-hidden="true" /><label>Base revision<select value={base} onChange={(event) => setBase(event.target.value ? Number(event.target.value) : "")}><option value="">Select…</option>{sorted.map((revision) => <option value={revision.number} key={revision.id}>Revision {revision.number}</option>)}</select></label><ArrowRight aria-hidden="true" /><label>Target revision<select value={target} onChange={(event) => setTarget(event.target.value ? Number(event.target.value) : "")}><option value="">Select…</option>{sorted.map((revision) => <option value={revision.number} key={revision.id}>Revision {revision.number}</option>)}</select></label><button type="button" disabled={!onCompare || base === "" || target === "" || base === target} onClick={() => void compare()} title={!onCompare ? capability.reason : undefined}>Compare structured revisions</button><span role="status">{state}</span></div>
      {sorted.length === 0 ? <div className="eng-large-empty"><SearchX aria-hidden="true" /><h3>No revision event published</h3><p>{capability.reason}</p></div> : <ol className="eng-revision-timeline">{sorted.map((revision) => <li key={revision.id}><i><GitCommitHorizontal aria-hidden="true" /></i><article><header><div><span className="eng-eyebrow">REVISION {revision.number}</span><h3>{revision.summary}</h3></div><span>{formatTime(revision.occurredAt)}</span></header>{revision.isReleaseBaseline && <p className="eng-alert-block"><ShieldAlert aria-hidden="true" />Immutable release baseline</p>}<dl><div><dt>Parent</dt><dd>{revision.parentNumber === null ? "Initial" : `Revision ${revision.parentNumber}`}</dd></div><div><dt>Operations</dt><dd>{revision.operationCount}</dd></div><div><dt>Created by</dt><dd>{humanize(revision.createdBy)}</dd></div><div><dt>Change set</dt><dd>{revision.changesetId || "Not linked"}</dd></div></dl><div className="eng-object-counts">{Object.entries(revision.objectCounts).map(([objectType, count]) => <span key={objectType}>{humanize(objectType)} <b>{count}</b></span>)}</div></article></li>)}</ol>}
    </section>
  );
}
