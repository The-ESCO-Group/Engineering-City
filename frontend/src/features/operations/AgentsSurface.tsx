import {
  Activity,
  AlertOctagon,
  Bot,
  CalendarClock,
  CircleOff,
  MapPin,
  MessageSquareText,
  Route,
  Search,
  SearchX
} from "lucide-react";
import { useMemo, useState } from "react";
import { humanizeOperationsCode } from "./adapters";
import type { OperationsAgentRecord } from "./types";

export interface AgentsSurfaceProps {
  agents: OperationsAgentRecord[];
  onOpenCityLocation?: (semanticLocation: string) => void;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function eventDetail(event: OperationsAgentRecord["recentEvents"][number]): string {
  const payload = event.payload;
  for (const key of ["title", "summary", "detail", "current_activity", "status"]) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return humanizeOperationsCode(event.event_type);
}

export function AgentsSurface({ agents, onOpenCityLocation }: AgentsSurfaceProps) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<"all" | OperationsAgentRecord["status"]>("all");
  const [selectedId, setSelectedId] = useState<string | null>(agents[0]?.id ?? null);
  const statuses = useMemo(
    () => [...new Set(agents.map((agent) => agent.status))].sort(),
    [agents]
  );
  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return agents.filter((agent) => {
      const matchesStatus = status === "all" || agent.status === status;
      const haystack = [agent.name, agent.kind, agent.location, agent.currentActivity, agent.currentTask?.title]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return matchesStatus && (!normalized || haystack.includes(normalized));
    });
  }, [agents, query, status]);
  const selected = visible.find((agent) => agent.id === selectedId) ?? visible[0] ?? null;
  const active = agents.filter((agent) => agent.status === "working" || agent.status === "walking").length;
  const waiting = agents.filter((agent) => agent.status === "waiting").length;
  const blocked = agents.filter((agent) => agent.status === "blocked").length;
  const offline = agents.filter((agent) => agent.status === "offline").length;

  return (
    <section className="ops-surface" aria-labelledby="ops-agents-title">
      <header className="ops-surface__heading">
        <div>
          <span>REPORTED AGENT SNAPSHOTS</span>
          <h2 id="ops-agents-title">Agent operations</h2>
          <p>Current backend-reported state, assigned work, messages, and recent events. This view has no agent-control actions.</p>
        </div>
        <span className="ops-readonly-badge"><CircleOff aria-hidden="true" /> Read only</span>
      </header>

      <dl className="ops-summary-grid" aria-label="Agent status summary">
        <div><dt>Reported agents</dt><dd>{agents.length}</dd></div>
        <div><dt>Working or walking</dt><dd>{active}</dd></div>
        <div><dt>Waiting</dt><dd>{waiting}</dd></div>
        <div className={blocked ? "is-attention" : ""}><dt>Blocked</dt><dd>{blocked}</dd></div>
        <div className={offline ? "is-muted" : ""}><dt>Offline</dt><dd>{offline}</dd></div>
      </dl>

      <div className="ops-toolbar" role="search">
        <label className="ops-search">
          <Search aria-hidden="true" />
          <span className="sr-only">Search agents</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search agent, role, task, or location…" />
        </label>
        <label className="ops-select">
          <span>Status</span>
          <select value={status} onChange={(event) => setStatus(event.target.value as typeof status)}>
            <option value="all">All statuses</option>
            {statuses.map((value) => <option value={value} key={value}>{humanizeOperationsCode(value)}</option>)}
          </select>
        </label>
        <output>{visible.length} of {agents.length}</output>
      </div>

      {visible.length === 0 ? (
        <div className="ops-empty"><SearchX aria-hidden="true" /><h3>No matching agents</h3><p>Change the search or status filter. Nothing is hidden without an active filter.</p></div>
      ) : (
        <div className="ops-split-layout">
          <ul className="ops-agent-list" aria-label="Reported agents">
            {visible.map((agent) => (
              <li key={agent.id}>
                <button type="button" aria-pressed={selected?.id === agent.id} onClick={() => setSelectedId(agent.id)}>
                  <span className={`ops-agent-avatar is-${agent.status}`}><Bot aria-hidden="true" /></span>
                  <span className="ops-agent-copy"><strong>{agent.name}</strong><small>{humanizeOperationsCode(agent.kind)}</small><span>{agent.currentActivity || "No current activity published"}</span></span>
                  <span className={`ops-status is-${agent.status}`}><i aria-hidden="true" />{humanizeOperationsCode(agent.status)}</span>
                </button>
              </li>
            ))}
          </ul>

          {selected && (
            <article className="ops-agent-detail" aria-labelledby="ops-selected-agent">
              <header>
                <div className={`ops-agent-avatar large is-${selected.status}`}><Bot aria-hidden="true" /></div>
                <div><span>{humanizeOperationsCode(selected.kind)}</span><h3 id="ops-selected-agent">{selected.name}</h3><code>{selected.id}</code></div>
                <span className={`ops-status is-${selected.status}`}><i aria-hidden="true" />{humanizeOperationsCode(selected.status)}</span>
              </header>

              <dl className="ops-detail-grid">
                <div><dt><MapPin aria-hidden="true" />Semantic location</dt><dd>{selected.location ? humanizeOperationsCode(selected.location) : "Not published"}</dd></div>
                <div><dt><CalendarClock aria-hidden="true" />Snapshot updated</dt><dd>{formatTime(selected.updatedAt)}</dd></div>
                <div><dt><Activity aria-hidden="true" />Current activity</dt><dd>{selected.currentActivity || "Not published"}</dd></div>
                <div><dt><Route aria-hidden="true" />Related tasks</dt><dd>{selected.relatedTaskCount}</dd></div>
                <div><dt>Agent definition</dt><dd>{selected.definition?.description || "No definition metadata published"}</dd></div>
                <div><dt>Definition status</dt><dd>{selected.definition ? (selected.definition.is_available === false ? selected.definition.unavailable_reason || "Unavailable" : "Available") : "Not loaded"}</dd></div>
              </dl>

              <section className="ops-detail-section" aria-labelledby="ops-current-task">
                <h4 id="ops-current-task">Current task</h4>
                {selected.currentTask ? (
                  <div className="ops-task-readout">
                    <div><strong>{selected.currentTask.title}</strong><span className={`ops-task-state is-${selected.currentTask.status}`}>{humanizeOperationsCode(selected.currentTask.status)}</span></div>
                    <p>{selected.currentTask.description || "No task description was published."}</p>
                    <dl><div><dt>Kind</dt><dd>{humanizeOperationsCode(selected.currentTask.kind)}</dd></div><div><dt>Attempts</dt><dd>{selected.currentTask.attempts} of {selected.currentTask.max_attempts}</dd></div><div><dt>Priority</dt><dd>{selected.currentTask.priority}</dd></div></dl>
                    {selected.currentTask.blocked_reason && <p className="ops-inline-warning"><AlertOctagon aria-hidden="true" />{selected.currentTask.blocked_reason}</p>}
                    {selected.currentTask.error_message && <p className="ops-inline-warning is-danger"><AlertOctagon aria-hidden="true" />{selected.currentTask.error_message}</p>}
                  </div>
                ) : <p className="ops-muted-note">No current task record is linked by the published agent snapshot.</p>}
              </section>

              <div className="ops-detail-columns">
                <section className="ops-detail-section" aria-labelledby="ops-agent-events">
                  <h4 id="ops-agent-events">Recent exact events <span>{selected.recentEvents.length}</span></h4>
                  {selected.recentEvents.length ? <ol className="ops-compact-log">{selected.recentEvents.map((event) => <li key={event.id}><i aria-hidden="true" /><div><strong>{humanizeOperationsCode(event.event_type)}</strong><p>{eventDetail(event)}</p><time dateTime={event.occurred_at}>Seq {event.seq} · {formatTime(event.occurred_at)}</time></div></li>)}</ol> : <p className="ops-muted-note">No instance-linked event is present in the recent stream window.</p>}
                </section>
                <section className="ops-detail-section" aria-labelledby="ops-agent-messages">
                  <h4 id="ops-agent-messages"><MessageSquareText aria-hidden="true" />Latest {Math.min(5, selected.messages.length)} of {selected.messages.length} loaded instance messages</h4>
                  {selected.messages.length ? <ol className="ops-message-list">{selected.messages.slice(0, 5).map((message) => <li key={message.id}><strong>{humanizeOperationsCode(message.role)}</strong><p>{message.body}</p><time dateTime={message.created_at}>{formatTime(message.created_at)}</time></li>)}</ol> : <p className="ops-muted-note">No instance-linked message is present in the loaded project message window.</p>}
                </section>
              </div>

              <section className="ops-detail-section" aria-labelledby="ops-agent-runs">
                <h4 id="ops-agent-runs">Latest {selected.recentRuns.length} of {selected.matchedRunCount} instance runs <span>{selected.runWindowSize} project runs loaded</span></h4>
                {selected.recentRuns.length ? <ol className="ops-message-list">{selected.recentRuns.map((run) => <li key={run.id}><strong>{humanizeOperationsCode(run.status)}</strong><p>{run.rationale || "No human-facing rationale was published."}</p><time dateTime={run.started_at ?? undefined}>{[run.provider, run.model, run.prompt_tokens !== undefined || run.completion_tokens !== undefined ? `${run.prompt_tokens ?? 0} in / ${run.completion_tokens ?? 0} out tokens` : null, run.started_at ? formatTime(run.started_at) : null].filter(Boolean).join(" · ") || "Run metadata not published"}</time></li>)}</ol> : <p className="ops-muted-note">No instance-linked run is present in the loaded {selected.runWindowSize}-run project window.</p>}
              </section>

              <footer>
                <p>Provider, model, and token usage are shown only when published by an instance-linked run. Cost and private reasoning are not part of the contract and are not inferred here.</p>
                <button type="button" disabled={!selected.location || !onOpenCityLocation} onClick={() => selected.location && onOpenCityLocation?.(selected.location)} title={!onOpenCityLocation ? "City navigation is not connected in this host." : undefined}><MapPin aria-hidden="true" /> Focus location in city</button>
              </footer>
            </article>
          )}
        </div>
      )}
    </section>
  );
}
