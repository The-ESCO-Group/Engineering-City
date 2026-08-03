import {
  ArrowDownUp,
  Bot,
  Braces,
  Clock3,
  Fingerprint,
  Search,
  SearchX,
  ShieldAlert,
  UserRound
} from "lucide-react";
import { useMemo, useState } from "react";
import { humanizeOperationsCode } from "./adapters";
import type { OperationsAuditRecord } from "./types";

export interface AuditTrailSurfaceProps {
  events: OperationsAuditRecord[];
  firstSequence: number | null;
  latestSequence: number | null;
  sequenceGapCount: number;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function payloadValue(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "[unserializable value]";
  }
}

export function AuditTrailSurface({ events, firstSequence, latestSequence, sequenceGapCount }: AuditTrailSurfaceProps) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const [newestFirst, setNewestFirst] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(events.at(-1)?.id ?? null);
  const categories = useMemo(() => [...new Set(events.map((event) => event.category))].sort(), [events]);
  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    const filtered = events.filter((event) => (
      (category === "all" || event.category === category) &&
      (!normalized || event.searchableText.includes(normalized))
    ));
    return newestFirst ? [...filtered].reverse() : filtered;
  }, [category, events, newestFirst, query]);
  const selected = visible.find((event) => event.id === selectedId) ?? visible[0] ?? null;
  const payloadEntries = selected ? Object.entries(selected.payload).sort(([left], [right]) => left.localeCompare(right)) : [];

  return (
    <section className="ops-surface" aria-labelledby="ops-audit-title">
      <header className="ops-surface__heading">
        <div>
          <span>ORDERED PROJECT EVENT STREAM</span>
          <h2 id="ops-audit-title">Audit Trail</h2>
          <p>Read-only inspection of the exact recent event envelopes supplied by the local backend or synthetic demo adapter.</p>
        </div>
        <span className="ops-readonly-badge"><Fingerprint aria-hidden="true" /> Exact envelopes</span>
      </header>

      <div className="ops-contract-note" role="note">
        <ShieldAlert aria-hidden="true" />
        <div><strong>Recent stream window—not a separate audit-read service</strong><p>The current contract supplies ordered project events but no paginated audit endpoint. History before the first loaded sequence is not claimed complete.</p></div>
      </div>

      <dl className="ops-summary-grid is-audit" aria-label="Event stream summary">
        <div><dt>Loaded envelopes</dt><dd>{events.length}</dd></div>
        <div><dt>First loaded sequence</dt><dd>{firstSequence ?? "—"}</dd></div>
        <div><dt>Latest sequence</dt><dd>{latestSequence ?? "—"}</dd></div>
        <div className={sequenceGapCount ? "is-attention" : ""}><dt>Internal gaps</dt><dd>{sequenceGapCount}</dd></div>
      </dl>

      <div className="ops-toolbar" role="search">
        <label className="ops-search">
          <Search aria-hidden="true" />
          <span className="sr-only">Search event stream</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search event type, sequence, IDs, or payload…" />
        </label>
        <label className="ops-select"><span>Category</span><select value={category} onChange={(event) => setCategory(event.target.value)}><option value="all">All categories</option>{categories.map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
        <button type="button" className="ops-order" aria-pressed={newestFirst} onClick={() => setNewestFirst((current) => !current)}><ArrowDownUp aria-hidden="true" />{newestFirst ? "Newest first" : "Oldest first"}</button>
        <output>{visible.length} of {events.length}</output>
      </div>

      {visible.length === 0 ? (
        <div className="ops-empty"><SearchX aria-hidden="true" /><h3>No matching events</h3><p>Change the search or category filter. Loaded envelopes remain unchanged.</p></div>
      ) : (
        <div className="ops-split-layout is-audit">
          <ol className="ops-audit-list" aria-label="Recent project events">
            {visible.map((event) => (
              <li key={event.id}>
                <button type="button" aria-pressed={selected?.id === event.id} onClick={() => setSelectedId(event.id)}>
                  <span className={`ops-event-signal is-${event.signal}`} aria-hidden="true" />
                  <span className="ops-event-sequence">SEQ {event.sequence}</span>
                  <span className="ops-event-copy"><strong>{humanizeOperationsCode(event.eventType)}</strong><span>{event.summary}</span><time dateTime={event.occurredAt}>{formatTime(event.occurredAt)}</time></span>
                  <span className="ops-event-category">{event.category}</span>
                </button>
              </li>
            ))}
          </ol>

          {selected && (
            <article className="ops-audit-detail" aria-labelledby="ops-selected-event">
              <header>
                <div><span>{selected.category} · Sequence {selected.sequence}</span><h3 id="ops-selected-event">{humanizeOperationsCode(selected.eventType)}</h3><p>{selected.summary}</p></div>
                <span className={`ops-status is-${selected.signal}`}><i aria-hidden="true" />{selected.signal}</span>
              </header>

              {selected.gapBefore && <p className="ops-inline-warning"><ShieldAlert aria-hidden="true" />The preceding loaded envelope is not the immediately previous sequence.</p>}

              <dl className="ops-envelope-grid">
                <div><dt><Clock3 aria-hidden="true" />Occurred</dt><dd><time dateTime={selected.occurredAt}>{formatTime(selected.occurredAt)}</time></dd></div>
                <div><dt><Braces aria-hidden="true" />Schema</dt><dd>Envelope v{selected.schemaVersion}</dd></div>
                <div><dt><Fingerprint aria-hidden="true" />Event ID</dt><dd><code>{selected.id}</code></dd></div>
                <div><dt>Project ID</dt><dd><code>{selected.projectId}</code></dd></div>
                <div><dt><Bot aria-hidden="true" />Agent ID</dt><dd>{selected.agentId ? <code>{selected.agentId}</code> : "Not attributed"}</dd></div>
                <div><dt>Task ID</dt><dd>{selected.taskId ? <code>{selected.taskId}</code> : "Not attributed"}</dd></div>
                <div><dt><UserRound aria-hidden="true" />Human actor</dt><dd>{selected.actorUserId ? <code>{selected.actorUserId}</code> : "Not attributed"}</dd></div>
              </dl>

              <section className="ops-payload" aria-labelledby="ops-event-payload">
                <h4 id="ops-event-payload">Published payload <span>{payloadEntries.length} fields</span></h4>
                {payloadEntries.length ? (
                  <dl>{payloadEntries.map(([key, value]) => <div key={key}><dt>{key}</dt><dd><pre>{payloadValue(value)}</pre></dd></div>)}</dl>
                ) : <p className="ops-muted-note">This envelope contains an empty payload object.</p>}
              </section>

              <footer>No mutation, approval, export, or replay action is available from this read-only stream inspector.</footer>
            </article>
          )}
        </div>
      )}
    </section>
  );
}
