import {
  AlertTriangle,
  CalendarClock,
  Check,
  CircleHelp,
  Clock3,
  Filter,
  GitPullRequestArrow,
  Gavel,
  MessageSquareText,
  Pause,
  Search,
  SearchX,
  ShieldAlert,
  UserRoundCheck,
  X
} from "lucide-react";
import { useMemo, useState } from "react";
import { EvidenceViewer } from "./EvidenceViewer";
import { approvalDecisionRequiresComment } from "./approvalPolicy";
import { ValidationBlockersPanel } from "./ValidationBlockersPanel";
import type {
  WorkspaceApproval,
  WorkspaceApprovalDecision,
  WorkspaceCapability
} from "./types";

export interface ApprovalInboxProps {
  approvals: WorkspaceApproval[];
  capability: WorkspaceCapability;
  onDecision?: (
    approval: WorkspaceApproval,
    decision: WorkspaceApprovalDecision,
    comment: string
  ) => Promise<void>;
  onOpenChangeset?: (changesetId: string) => void;
  validationProjectId?: string | null;
  canWaiveValidation?: boolean;
}

const DECISIONS: Array<{ value: WorkspaceApprovalDecision; label: string }> = [
  { value: "approve", label: "Approve" },
  { value: "reject", label: "Reject" },
  { value: "request_changes", label: "Request changes" },
  { value: "accept_risk", label: "Accept risk" },
  { value: "defer", label: "Defer" }
];

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function ApprovalInbox({ approvals, capability, onDecision, onOpenChangeset, validationProjectId, canWaiveValidation = false }: ApprovalInboxProps) {
  const [query, setQuery] = useState("");
  const [gate, setGate] = useState("all");
  const [status, setStatus] = useState("pending");
  const [selectedId, setSelectedId] = useState<string | null>(approvals[0]?.id ?? null);
  const [decisionMessage, setDecisionMessage] = useState("");
  const gates = useMemo(() => [...new Set(approvals.map((item) => item.gate))].sort(), [approvals]);
  const statuses = useMemo(() => [...new Set(approvals.map((item) => item.status))].sort(), [approvals]);
  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return approvals.filter((approval) => {
      const haystack = `${approval.title} ${approval.kind} ${approval.reviewer} ${approval.risk} ${approval.findingId ?? ""}`.toLowerCase();
      return haystack.includes(normalized) && (gate === "all" || approval.gate === gate) && (status === "all" || approval.status === status);
    });
  }, [approvals, gate, query, status]);
  const selected = visible.find((item) => item.id === selectedId) ?? visible[0] ?? null;
  const decide = onDecision ? async (
    approval: WorkspaceApproval,
    decision: WorkspaceApprovalDecision,
    comment: string
  ) => {
    await onDecision(approval, decision, comment);
    setDecisionMessage(`${humanize(decision)} recorded for ${approval.title}.`);
  } : undefined;
  return (
    <section className="eng-approval-workspace" aria-labelledby="approvals-title">
      <header className="eng-workspace-header"><div><span className="eng-eyebrow">APPROVAL PLAZA</span><h2 id="approvals-title">Human decision inbox</h2><p>Named authority, affected revision, evidence, validation, risk, comments, and downstream impact.</p></div><div className="eng-approval-count"><b>{approvals.filter((item) => item.status === "pending").length}</b><span>pending decisions</span></div></header>
      {capability.level !== "available" && <div className="eng-capability-notice"><CircleHelp aria-hidden="true" /><p>{capability.reason}</p></div>}
      {validationProjectId && <ValidationBlockersPanel projectId={validationProjectId} canWaive={canWaiveValidation} />}
      <div className="eng-toolbar"><label className="eng-search"><Search aria-hidden="true" /><span className="sr-only">Search approvals</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search decision, reviewer, risk, or finding…" /></label><label className="eng-select"><Filter aria-hidden="true" /><span className="sr-only">Filter gate</span><select value={gate} onChange={(event) => setGate(event.target.value)}><option value="all">All gates</option>{gates.map((item) => <option key={item}>{item}</option>)}</select></label><label className="eng-select"><Filter aria-hidden="true" /><span className="sr-only">Filter approval status</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All statuses</option>{statuses.map((item) => <option key={item}>{item}</option>)}</select></label><span className="eng-result-count">{visible.length} of {approvals.length}</span></div>
      {approvals.length === 0 ? <div className="eng-large-empty"><SearchX aria-hidden="true" /><h3>No approval requests</h3><p>No human decision is fabricated when the backend has not requested one.</p></div> : (
        <div className="eng-approval-layout">
          <nav className="eng-approval-list" aria-label="Approval requests">{visible.map((approval) => <button type="button" key={approval.id} className={selected?.id === approval.id ? "selected" : ""} onClick={() => setSelectedId(approval.id)}><span><i className={`eng-severity-dot ${approval.severity || "unknown"}`} /><b>{approval.title}</b></span><small>{humanize(approval.gate)} · {approval.findingId || approval.changesetId || "Project decision"}</small><em>{humanize(approval.status)}</em></button>)}</nav>
          {selected && <ApprovalDetail key={selected.id} approval={selected} capability={capability} onDecision={decide} onOpenChangeset={onOpenChangeset} />}
        </div>
      )}
      {decisionMessage && <p className="eng-submit-message success" role="status">{decisionMessage}</p>}
    </section>
  );
}

function ApprovalDetail({ approval, capability, onDecision, onOpenChangeset }: {
  approval: WorkspaceApproval;
  capability: WorkspaceCapability;
  onDecision?: ApprovalInboxProps["onDecision"];
  onOpenChangeset?: (changesetId: string) => void;
}) {
  const [decision, setDecision] = useState<WorkspaceApprovalDecision | null>(null);
  const [comment, setComment] = useState("");
  const [submitState, setSubmitState] = useState<"idle" | "busy" | "success" | "error">("idle");
  const [message, setMessage] = useState("");
  const required = decision ? approvalDecisionRequiresComment(approval, decision) : false;
  const allowed = decision ? approval.allowedDecisions.includes(decision) : false;
  const canSubmit = Boolean(onDecision && decision && allowed && (!required || comment.trim()) && approval.status === "pending");

  const submit = async () => {
    if (!decision || !onDecision || !canSubmit) return;
    setSubmitState("busy");
    setMessage("");
    try {
      await onDecision(approval, decision, comment.trim());
      setSubmitState("success");
      setMessage("The local decision handler recorded the reviewer action.");
    } catch (error) {
      setSubmitState("error");
      setMessage(error instanceof Error ? error.message : "The approval decision failed.");
    }
  };

  return (
    <article className="eng-approval-detail">
      <header><div><span className={`eng-status ${approval.status}`}>{humanize(approval.status)}</span>{approval.severity && <span className={`eng-severity ${approval.severity}`}>{humanize(approval.severity)}</span>}</div><code>{approval.id}</code></header>
      <h3>{approval.title}</h3><p className="eng-requested-decision">{approval.requestedDecision}</p>
      <div className="eng-approval-meta"><span><UserRoundCheck aria-hidden="true" />{approval.reviewer}</span><span><Clock3 aria-hidden="true" />{approval.requestedAt ? new Date(approval.requestedAt).toLocaleString() : "Request time not published"}</span><span><CalendarClock aria-hidden="true" />{approval.dueDate || "No due date"}</span></div>
      {(approval.restrictedDomain || approval.finalRelease || approval.severity === "critical") && <p className="eng-alert-block danger"><ShieldAlert aria-hidden="true" />{approval.risk || "Restricted or high-consequence decision. A qualified named reviewer is required."}</p>}
      <div className="eng-operation-grid"><div><span>Affected revision</span><p>{approval.affectedRevision === null ? "Not published" : `Revision ${approval.affectedRevision}`}</p></div><div><span>Gate</span><p>{humanize(approval.gate)}</p></div><div><span>Requested by</span><p>{humanize(approval.requestedBy)}</p></div><div><span>Downstream impact</span><p>{approval.downstreamImpact || "Not published"}</p></div></div>
      <section className="eng-approval-rationale"><h4>Agent rationale</h4><p>{approval.rationale || "No rationale was published."}</p></section>
      <div className="eng-link-columns"><TextList title="Affected objects" values={approval.affectedObjects} /><TextList title="Validation results" values={approval.validationResults} /></div>
      <EvidenceViewer evidence={approval.evidence} title="Decision evidence" />
      {approval.changesetId && <button type="button" className="eng-linked-changeset" disabled={!onOpenChangeset} onClick={() => onOpenChangeset?.(approval.changesetId!)}><GitPullRequestArrow aria-hidden="true" /> Open structured diff · {approval.changesetId}</button>}
      <div className="eng-link-columns"><TextList title="Prior comments" values={approval.comments} /><TextList title="Prior decisions" values={approval.priorDecisions} /></div>
      <section className="eng-decision-form" aria-labelledby={`decision-title-${approval.id}`}>
        <h4 id={`decision-title-${approval.id}`}><Gavel aria-hidden="true" />Record named reviewer decision</h4>
        <div className="eng-decision-options">{DECISIONS.map((item) => {
          const isAllowed = approval.allowedDecisions.includes(item.value);
          return <button type="button" key={item.value} className={decision === item.value ? "selected" : item.value === "reject" ? "danger" : ""} disabled={!isAllowed || approval.status !== "pending"} onClick={() => setDecision(item.value)} aria-pressed={decision === item.value} title={!isAllowed ? approval.decisionDisabledReason || (approval.kind === "gate_decision" ? `${item.label} is unavailable for this gate.` : "Only workflow gate approvals support this decision.") : undefined}>{item.value === "approve" ? <Check aria-hidden="true" /> : item.value === "reject" ? <X aria-hidden="true" /> : item.value === "defer" ? <Pause aria-hidden="true" /> : item.value === "accept_risk" ? <AlertTriangle aria-hidden="true" /> : <MessageSquareText aria-hidden="true" />}{item.label}</button>;
        })}</div>
        <label>Engineering comment {required && <b>(required)</b>}<textarea rows={4} value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Record evidence considered, engineering basis, limitations, and downstream conditions…" /></label>
        {decision && required && !comment.trim() && <p className="eng-field-error"><AlertTriangle aria-hidden="true" />A comment is required for this decision.</p>}
        <button type="button" className="primary" disabled={!canSubmit || submitState === "busy"} onClick={() => void submit()}>{submitState === "busy" ? "Recording decision…" : "Submit signed decision"}</button>
        {!onDecision && <p className="eng-disabled-reason">Decision submission is disabled: {capability.reason}</p>}
        {approval.decisionDisabledReason && <p className="eng-disabled-reason">Decision submission is disabled: {approval.decisionDisabledReason}</p>}
        {message && <p className={`eng-submit-message ${submitState}`} role="status">{message}</p>}
      </section>
      <p className="eng-advisory"><ShieldAlert aria-hidden="true" /><b>AI output is advisory.</b> The named reviewer remains responsible for the engineering decision.</p>
    </article>
  );
}

function TextList({ title, values }: { title: string; values: string[] }) {
  return <section className="eng-record-list"><h5>{title}</h5>{values.length ? <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul> : <p>None published.</p>}</section>;
}
