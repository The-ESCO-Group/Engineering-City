import { CheckCircle2, CircleHelp, LockKeyhole } from "lucide-react";
import { useState } from "react";
import type { ClarificationInbox as ClarificationInboxData } from "../../api/types";

export interface ClarificationInboxProps {
  inbox: ClarificationInboxData | null;
  onResolve?: (kind: "project_question" | "clarification", id: string, answer: string) => Promise<void>;
}

interface InboxItem {
  id: string;
  kind: "project_question" | "clarification";
  reference: string;
  category: string;
  question: string;
  context: string;
  status: string;
  blocking: boolean;
  answer: string;
}

function itemsFrom(inbox: ClarificationInboxData | null): InboxItem[] {
  if (!inbox) return [];
  return [
    ...inbox.project_questions.map((item) => ({
      id: item.id,
      kind: "project_question" as const,
      reference: item.reference,
      category: item.category,
      question: item.question,
      context: item.why_it_matters,
      status: item.status,
      blocking: item.is_blocking,
      answer: item.answer
    })),
    ...inbox.clarifications.map((item) => ({
      id: item.id,
      kind: "clarification" as const,
      reference: item.requirement_id ? `Requirement ${item.requirement_id.slice(0, 8)}` : item.object_type || "Project detail",
      category: item.object_type || "requirement ambiguity",
      question: item.question || item.ambiguity,
      context: item.ambiguity,
      status: item.status,
      blocking: item.blocks_generation && ["open", "deferred"].includes(item.status),
      answer: item.resolution
    }))
  ].sort((left, right) => Number(right.blocking) - Number(left.blocking));
}

export function ClarificationInbox({ inbox, onResolve }: ClarificationInboxProps) {
  const items = itemsFrom(inbox);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  async function submit(item: InboxItem): Promise<void> {
    const answer = (drafts[item.id] || "").trim();
    if (!answer || !onResolve) return;
    setSubmitting(item.id);
    setMessage("");
    try {
      await onResolve(item.kind, item.id, answer);
      setDrafts((current) => ({ ...current, [item.id]: "" }));
      setMessage("Answer recorded as a named engineering decision. Dependent work was re-queued when eligible.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The local clarification request failed.");
    } finally {
      setSubmitting(null);
    }
  }

  return (
    <section className="eng-clarifications" aria-labelledby="clarifications-title">
      <header className="eng-workspace-header">
        <div><span className="eng-eyebrow">HUMAN INPUT</span><h2 id="clarifications-title">Clarifications</h2><p>Agents stop here instead of guessing. Every submitted answer becomes a named, auditable engineering decision.</p></div>
        <div className={inbox?.blocking_count ? "eng-question-count blocking" : "eng-question-count"}><LockKeyhole aria-hidden="true" /><b>{inbox?.blocking_count || 0}</b><span>blocking</span></div>
      </header>
      {message && <p className="eng-alert-block" role="status">{message}</p>}
      {!inbox && <div className="eng-large-empty"><CircleHelp aria-hidden="true" /><h3>Clarification state unavailable</h3><p>The authorized local API has not returned this project’s question inbox.</p></div>}
      {inbox && items.length === 0 && <div className="eng-large-empty"><CheckCircle2 aria-hidden="true" /><h3>No clarifications</h3><p>No agent or deterministic gate is waiting for a human answer.</p></div>}
      <div className="eng-question-list">
        {items.map((item) => {
          const resolved = !["open", "deferred"].includes(item.status);
          return <article key={`${item.kind}-${item.id}`} className={item.blocking ? "blocking" : resolved ? "resolved" : ""}>
            <header><div><span>{item.reference} · {item.category.replaceAll("_", " ")}</span><h3>{item.question}</h3></div><b>{resolved ? item.status : item.blocking ? "BLOCKING" : "OPEN"}</b></header>
            {item.context && item.context !== item.question && <p>{item.context}</p>}
            {resolved ? <div className="eng-question-answer"><CheckCircle2 aria-hidden="true" /><span><b>Recorded answer</b>{item.answer || "Resolved by an authorized user."}</span></div> : <div className="eng-question-response">
              <label>Engineering answer<textarea rows={4} value={drafts[item.id] || ""} onChange={(event) => setDrafts((current) => ({ ...current, [item.id]: event.target.value }))} placeholder="State the decision, value, or scope boundary precisely…" /></label>
              <button type="button" className="eng-primary-action" disabled={!onResolve || !(drafts[item.id] || "").trim() || submitting === item.id} onClick={() => void submit(item)}>{submitting === item.id ? "Recording…" : "Record answer & continue"}</button>
            </div>}
          </article>;
        })}
      </div>
    </section>
  );
}
