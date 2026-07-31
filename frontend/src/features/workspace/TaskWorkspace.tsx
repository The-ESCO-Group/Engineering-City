import {
  AlertOctagon,
  Bot,
  CalendarClock,
  Columns3,
  ExternalLink,
  Filter,
  List,
  MapPin,
  Search,
  SearchX
} from "lucide-react";
import { useMemo, useState } from "react";
import type { WorkspaceTask, WorkspaceTaskState } from "./types";

export interface TaskWorkspaceProps {
  tasks: WorkspaceTask[];
  onOpenCityLocation?: (semanticLocation: string) => void;
}

const TASK_COLUMNS: Array<{ state: WorkspaceTaskState; label: string }> = [
  { state: "queued", label: "Queued" },
  { state: "running", label: "Running" },
  { state: "blocked", label: "Blocked" },
  { state: "awaiting_approval", label: "Awaiting approval" },
  { state: "completed", label: "Completed" },
  { state: "failed", label: "Failed" }
];

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatTime(value: string | null): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

export function TaskWorkspace({ tasks, onOpenCityLocation }: TaskWorkspaceProps) {
  const [layout, setLayout] = useState<"board" | "list">("board");
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState<"all" | WorkspaceTaskState>("all");
  const [selectedId, setSelectedId] = useState<string | null>(tasks[0]?.id ?? null);

  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return tasks.filter((task) => {
      const matchesState = stateFilter === "all" || task.state === stateFilter;
      const haystack = `${task.title} ${task.description} ${task.kind} ${task.agent ?? ""}`.toLowerCase();
      return matchesState && (!normalized || haystack.includes(normalized));
    });
  }, [query, stateFilter, tasks]);
  const selected = visible.find((task) => task.id === selectedId) ?? visible[0] ?? null;

  return (
    <section className="eng-task-workspace" aria-labelledby="tasks-title">
      <header className="eng-workspace-header">
        <div><span className="eng-eyebrow">WORKFLOW EXECUTION</span><h2 id="tasks-title">Task board & list</h2><p>Backend task state only. Unknown progress is never estimated.</p></div>
        <div className="eng-segmented" aria-label="Task layout">
          <button type="button" className={layout === "board" ? "active" : ""} onClick={() => setLayout("board")} aria-pressed={layout === "board"}><Columns3 aria-hidden="true" /> Board</button>
          <button type="button" className={layout === "list" ? "active" : ""} onClick={() => setLayout("list")} aria-pressed={layout === "list"}><List aria-hidden="true" /> List</button>
        </div>
      </header>

      <div className="eng-toolbar">
        <label className="eng-search"><Search aria-hidden="true" /><span className="sr-only">Search tasks</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search task, kind, or agent…" /></label>
        <label className="eng-select"><Filter aria-hidden="true" /><span className="sr-only">Filter task status</span><select value={stateFilter} onChange={(event) => setStateFilter(event.target.value as "all" | WorkspaceTaskState)}><option value="all">All task states</option>{TASK_COLUMNS.map((column) => <option key={column.state} value={column.state}>{column.label}</option>)}</select></label>
        <span className="eng-result-count">{visible.length} of {tasks.length}</span>
      </div>

      {visible.length === 0 ? (
        <div className="eng-large-empty"><SearchX aria-hidden="true" /><h3>No matching tasks</h3><p>Change the search or state filter. No tasks were hidden silently.</p></div>
      ) : (
        <div className="eng-task-layout">
          <div className="eng-task-main">
            {layout === "board" ? (
              <div className="eng-task-board">
                {TASK_COLUMNS.map((column) => {
                  const columnTasks = visible.filter((task) => task.state === column.state);
                  return (
                    <section key={column.state} aria-labelledby={`task-column-${column.state}`}>
                      <h3 id={`task-column-${column.state}`}><span className={`eng-state-dot ${column.state}`} />{column.label}<b>{columnTasks.length}</b></h3>
                      <div>
                        {columnTasks.length === 0 && <p className="eng-column-empty">No tasks</p>}
                        {columnTasks.map((task) => <TaskCard task={task} selected={selected?.id === task.id} onSelect={() => setSelectedId(task.id)} key={task.id} />)}
                      </div>
                    </section>
                  );
                })}
              </div>
            ) : (
              <div className="eng-table-scroll">
                <table className="eng-data-table">
                  <thead><tr><th>Task</th><th>State</th><th>Agent</th><th>Priority</th><th>Location</th><th>Updated</th></tr></thead>
                  <tbody>{visible.map((task) => (
                    <tr key={task.id} className={selected?.id === task.id ? "selected" : ""}>
                      <td><button type="button" className="eng-cell-button" onClick={() => setSelectedId(task.id)}><b>{task.title}</b><small>{humanize(task.kind)}</small></button></td>
                      <td><span className={`eng-status ${task.state}`}>{humanize(task.state)}</span></td>
                      <td>{task.agent ? humanize(task.agent) : "Unassigned"}</td>
                      <td>{task.priority}</td>
                      <td>{task.semanticLocation ? humanize(task.semanticLocation) : "Not published"}</td>
                      <td>{formatTime(task.updatedAt)}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            )}
          </div>
          {selected && (
            <aside className="eng-record-inspector" aria-label={`Task details: ${selected.title}`}>
              <span className={`eng-status ${selected.state}`}>{humanize(selected.state)}</span>
              <h3>{selected.title}</h3>
              <code>{selected.id}</code>
              <p>{selected.description || "No task description was published."}</p>
              {selected.blockedReason && <div className="eng-alert-block"><AlertOctagon aria-hidden="true" /><div><b>Blocked</b><p>{selected.blockedReason}</p></div></div>}
              {selected.error && <div className="eng-alert-block danger"><AlertOctagon aria-hidden="true" /><div><b>Task error</b><p>{selected.error}</p></div></div>}
              <dl className="eng-definition-list">
                <div><dt><Bot aria-hidden="true" />Assigned agent</dt><dd>{selected.agent ? humanize(selected.agent) : "Unassigned"}</dd></div>
                <div><dt><MapPin aria-hidden="true" />Semantic location</dt><dd>{selected.semanticLocation ? humanize(selected.semanticLocation) : "Not published"}</dd></div>
                <div><dt><CalendarClock aria-hidden="true" />Started</dt><dd>{formatTime(selected.startedAt)}</dd></div>
                <div><dt>Attempts</dt><dd>{selected.attempts} of {selected.maxAttempts}</dd></div>
              </dl>
              {selected.progress !== null ? (
                <div className="eng-progress-readout"><span>Published progress</span><b>{selected.progress}%</b><div><i style={{ width: `${Math.max(0, Math.min(100, selected.progress))}%` }} /></div></div>
              ) : <p className="eng-muted-note">Progress was not published; no estimate is shown.</p>}
              <button
                type="button"
                className="eng-full-button"
                disabled={!selected.semanticLocation || !onOpenCityLocation}
                onClick={() => selected.semanticLocation && onOpenCityLocation?.(selected.semanticLocation)}
                title={!onOpenCityLocation ? "City navigation is not connected in this workspace host." : undefined}
              ><ExternalLink aria-hidden="true" /> Focus district in city</button>
            </aside>
          )}
        </div>
      )}
    </section>
  );
}

function TaskCard({ task, selected, onSelect }: { task: WorkspaceTask; selected: boolean; onSelect: () => void }) {
  return (
    <button type="button" className={`eng-task-card ${selected ? "selected" : ""}`} onClick={onSelect} aria-pressed={selected}>
      <span className={`eng-task-priority ${task.priority >= 90 ? "urgent" : ""}`}>P{task.priority}</span>
      <h4>{task.title}</h4>
      <p>{task.agent ? humanize(task.agent) : "Unassigned"}</p>
      {task.progress !== null ? <div className="eng-mini-progress"><i style={{ width: `${task.progress}%` }} /><span>{task.progress}%</span></div> : <small>Progress not published</small>}
      {task.blockedReason && <span className="eng-card-warning"><AlertOctagon aria-hidden="true" />Blocked reason available</span>}
    </button>
  );
}
