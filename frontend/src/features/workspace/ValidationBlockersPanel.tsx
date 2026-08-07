import { AlertTriangle, CheckCircle2, CircleHelp, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { useValidationResults, useValidationWaiver } from "../../api/live";
import type { ValidationResultRead } from "../../api/types";

export interface ValidationBlockersPanelProps {
  projectId: string;
  canWaive: boolean;
}

function isWaiverEligible(result: ValidationResultRead): boolean {
  return result.severity === "error" ||
    result.severity === "human_review_required" ||
    result.requires_human_decision;
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function ValidationBlockersPanel({ projectId, canWaive }: ValidationBlockersPanelProps) {
  const results = useValidationResults(projectId, true);
  const waiver = useValidationWaiver(projectId);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [success, setSuccess] = useState("");
  const selected = results.data?.find((result) => result.id === selectedId) ?? null;
  const eligibleCount = results.data?.filter((result) => isWaiverEligible(result) && !result.is_waived).length ?? 0;
  const canSubmit = Boolean(selected && canWaive && confirmed && reason.trim() && isWaiverEligible(selected) && !selected.is_waived && !waiver.isPending);

  const openWaiver = (result: ValidationResultRead) => {
    setSelectedId(result.id);
    setReason("");
    setConfirmed(false);
    setSuccess("");
    waiver.reset();
  };

  const submit = async () => {
    if (!selected || !canWaive || !confirmed || !reason.trim() || !isWaiverEligible(selected) || selected.is_waived) return;
    try {
      const response = await waiver.mutateAsync({
        resultId: selected.id,
        reason: reason.trim(),
        expectedUpdatedAt: selected.updated_at
      });
      setSuccess(response.decision_replayed
        ? `The existing waiver for ${selected.code} was confirmed.`
        : `Waiver recorded for ${selected.code}. ${response.unblocked_task_count} task(s) unblocked.`);
      setSelectedId(null);
      setReason("");
      setConfirmed(false);
    } catch {
      // The mutation retains the typed API error and the form stays open for review.
    }
  };

  return (
    <section className="eng-validation-panel" aria-labelledby="validation-blockers-title">
      <header>
        <div><span className="eng-eyebrow">CURRENT VALIDATION RUN</span><h3 id="validation-blockers-title">Validation blockers &amp; waivers</h3><p>Deterministic results remain visible after waiver; a waiver records accepted risk and does not erase the finding.</p></div>
        <span className={eligibleCount ? "attention" : "clear"}><b>{eligibleCount}</b> eligible blocker{eligibleCount === 1 ? "" : "s"}</span>
      </header>
      {results.isPending && <p className="eng-validation-state"><CircleHelp aria-hidden="true" />Loading current validation results…</p>}
      {results.isError && <p className="eng-validation-state error" role="alert"><AlertTriangle aria-hidden="true" />Validation results are unavailable. No waiver controls are enabled.</p>}
      {!results.isPending && !results.isError && results.data?.length === 0 && <p className="eng-validation-state"><CheckCircle2 aria-hidden="true" />No current validation results were returned by the backend.</p>}
      {results.data && results.data.length > 0 && <div className="eng-validation-results">{results.data.map((result) => {
        const eligible = isWaiverEligible(result);
        return <article key={result.id} className={`eng-validation-result ${result.severity} ${result.is_waived ? "waived" : ""}`}>
          <div><span className="eng-status">{humanize(result.severity)}</span><code>{result.code}</code><small>{humanize(result.discipline)}</small></div>
          <h4>{result.message}</h4>
          <p>{result.object_label || "Project-level validation result"}{result.suggested_action ? ` · ${result.suggested_action}` : ""}</p>
          {result.is_waived ? <p className="eng-waiver-record"><ShieldCheck aria-hidden="true" /><b>Waived:</b> {result.waiver_reason}</p> : eligible ? <button type="button" disabled={!canWaive || waiver.isPending} onClick={() => openWaiver(result)} title={!canWaive ? "A reviewer role on this project is required." : undefined}>Review waiver</button> : <small>This result is informational and cannot be waived.</small>}
        </article>;
      })}</div>}
      {selected && <div className="eng-waiver-form">
        <h4><AlertTriangle aria-hidden="true" />Accept validation risk for {selected.code}</h4>
        <p>This decision is immutable for the current validation run and will name the signed-in reviewer.</p>
        <label htmlFor={`waiver-reason-${selected.id}`}>Engineering reason <b>(required)</b></label>
        <textarea id={`waiver-reason-${selected.id}`} rows={3} maxLength={2000} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="State the evidence reviewed, risk accepted, limitations, and required follow-up." />
        <label className="eng-waiver-confirm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />I confirm this result and accept responsibility for the recorded validation risk.</label>
        <div><button type="button" onClick={() => setSelectedId(null)} disabled={waiver.isPending}>Cancel</button><button type="button" className="danger" disabled={!canSubmit} onClick={() => void submit()}>{waiver.isPending ? "Recording waiver…" : "Confirm waiver"}</button></div>
        {waiver.isError && <p className="eng-submit-message error" role="alert">{waiver.error instanceof Error ? waiver.error.message : "The validation waiver failed."}</p>}
      </div>}
      {success && <p className="eng-submit-message success" role="status">{success}</p>}
      {!canWaive && !results.isError && <p className="eng-validation-state"><ShieldCheck aria-hidden="true" />Waiver controls require a reviewer or administrator assigned to this project.</p>}
    </section>
  );
}
