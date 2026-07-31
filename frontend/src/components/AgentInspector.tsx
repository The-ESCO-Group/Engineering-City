import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  ClipboardList,
  Clock3,
  Cpu,
  FileCheck2,
  FolderKanban,
  Gauge,
  ShieldQuestion,
  X
} from "lucide-react";
import { useEffect, useId, useRef } from "react";
import type { WorldAgent, WorldTask } from "../world/types";
import "./AgentInspector.css";

export interface AgentInspectorProject {
  readonly id: string;
  readonly name?: string;
}

export interface AgentInspectorProgress {
  readonly current: number;
  readonly total: number;
  readonly label?: string;
}

export interface AgentInspectorEvent {
  readonly id: string;
  readonly label: string;
  readonly detail?: string;
  readonly occurredAt?: string;
}

export interface AgentInspectorPendingApproval {
  readonly title: string;
  readonly detail?: string;
  readonly requestedAt?: string;
}

/**
 * Optional facts supplied by authoritative workflow data. This interface
 * intentionally has no field for private reasoning or chain-of-thought.
 */
export interface AgentInspectorSupplemental {
  readonly role?: string;
  readonly locationName?: string;
  readonly progress?: AgentInspectorProgress;
  readonly blocker?: string;
  readonly confidencePercent?: number;
  readonly evidence?: readonly string[];
  readonly model?: string;
  readonly provider?: string;
  readonly reportedCost?: string;
  readonly costBasis?: string;
  readonly lastAction?: string;
  readonly recentEvents?: readonly AgentInspectorEvent[];
  readonly pendingApproval?: AgentInspectorPendingApproval | null;
  /** A concise, user-facing operational summary; never hidden model reasoning. */
  readonly publicRationale?: string;
  readonly warnings?: readonly string[];
}

export interface AgentInspectorProps {
  readonly open: boolean;
  readonly agent: WorldAgent | null;
  readonly task: WorldTask | null;
  readonly project: AgentInspectorProject | null;
  readonly supplemental?: AgentInspectorSupplemental;
  readonly onClose: () => void;
  readonly className?: string;
}

function present(value: string | undefined): value is string {
  return value !== undefined && value.trim().length > 0;
}

function validProgress(progress: AgentInspectorProgress | undefined) {
  return (
    progress !== undefined &&
    Number.isFinite(progress.current) &&
    Number.isFinite(progress.total) &&
    progress.current >= 0 &&
    progress.total > 0 &&
    progress.current <= progress.total
  );
}

function validConfidence(value: number | undefined): value is number {
  return value !== undefined && Number.isFinite(value) && value >= 0 && value <= 100;
}

function DetailRow({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export function AgentInspector({
  open,
  agent,
  task,
  project,
  supplemental = {},
  onClose,
  className = ""
}: AgentInspectorProps) {
  const headingId = useId();
  const descriptionId = useId();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  const agentId = agent?.id;

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open || agentId === undefined) return;

    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    closeButtonRef.current?.focus({ preventScroll: true });

    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      onCloseRef.current();
    };

    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
      if (previousFocus !== null && document.contains(previousFocus)) {
        previousFocus.focus({ preventScroll: true });
      }
    };
  }, [agentId, open]);

  if (!open || agent === null) return null;

  const role = present(supplemental.role) ? supplemental.role : agent.agentKind;
  const location = present(supplemental.locationName)
    ? supplemental.locationName
    : agent.locationId === null
      ? "Not reported"
      : String(agent.locationId);
  const status = present(agent.sourceStatus)
    ? agent.sourceStatus
    : present(agent.status)
      ? agent.status
      : "Not reported";
  const evidence = supplemental.evidence?.filter(present) ?? [];
  const recentEvents = supplemental.recentEvents?.filter((event) => present(event.label)) ?? [];
  const warnings = supplemental.warnings?.filter(present) ?? [];
  const hasProgress = validProgress(supplemental.progress);
  const confidence = validConfidence(supplemental.confidencePercent)
    ? supplemental.confidencePercent
    : null;
  const hasExecutionFacts =
    confidence !== null ||
    evidence.length > 0 ||
    present(supplemental.model) ||
    present(supplemental.provider) ||
    present(supplemental.reportedCost);
  const hasActivity =
    present(agent.currentActivity) || present(supplemental.lastAction) || recentEvents.length > 0;

  return (
    <aside
      className={`agent-inspector ${className}`.trim()}
      role="dialog"
      aria-modal="false"
      aria-labelledby={headingId}
      aria-describedby={descriptionId}
      data-agent-id={String(agent.id)}
    >
      <header className="agent-inspector__header">
        <div>
          <span className="agent-inspector__eyebrow">Agent inspector</span>
          <h2 id={headingId}>{present(agent.displayName) ? agent.displayName : String(agent.id)}</h2>
          <p id={descriptionId}>{present(role) ? role : "Role not reported"}</p>
        </div>
        <button
          ref={closeButtonRef}
          className="agent-inspector__close"
          type="button"
          aria-label="Close agent inspector"
          onClick={onClose}
        >
          <X aria-hidden="true" />
        </button>
      </header>

      <div className="agent-inspector__body">
        <section className="agent-inspector__section" aria-label="Agent state">
          <div className="agent-inspector__identity-icon" aria-hidden="true">
            <Bot />
          </div>
          <dl className="agent-inspector__state-grid">
            <DetailRow label="Status" value={status} />
            <DetailRow label="Semantic location" value={location} />
          </dl>
          <p className="agent-inspector__updated">
            <Clock3 aria-hidden="true" />
            State event {agent.lastEvent.sequence}
            {present(agent.lastEvent.occurredAt) ? (
              <> · <time dateTime={agent.lastEvent.occurredAt}>{agent.lastEvent.occurredAt}</time></>
            ) : null}
          </p>
        </section>

        <section className="agent-inspector__section" aria-labelledby={`${headingId}-assignment`}>
          <h3 id={`${headingId}-assignment`}>
            <ClipboardList aria-hidden="true" /> Assignment
          </h3>
          <div className="agent-inspector__assignment">
            <div>
              <FolderKanban aria-hidden="true" />
              <span>Project</span>
              {project === null ? (
                <strong>No project context reported</strong>
              ) : (
                <strong title={project.id}>{present(project.name) ? project.name : project.id}</strong>
              )}
            </div>
            <div>
              <CheckCircle2 aria-hidden="true" />
              <span>Task</span>
              {task !== null ? (
                <>
                  <strong title={String(task.id)}>{present(task.title) ? task.title : String(task.id)}</strong>
                  <small>
                    {present(task.sourceStatus)
                      ? task.sourceStatus
                      : present(task.status)
                        ? task.status
                        : "Status not reported"}
                  </small>
                </>
              ) : agent.taskId !== null ? (
                <>
                  <strong>{String(agent.taskId)}</strong>
                  <small>Task details not loaded</small>
                </>
              ) : (
                <strong>No task assigned</strong>
              )}
            </div>
          </div>

          {hasProgress && supplemental.progress ? (
            <div className="agent-inspector__progress">
              <div>
                <span>{present(supplemental.progress.label) ? supplemental.progress.label : "Progress"}</span>
                <strong>{supplemental.progress.current} of {supplemental.progress.total}</strong>
              </div>
              <div
                className="agent-inspector__progress-track"
                role="progressbar"
                aria-label={present(supplemental.progress.label) ? supplemental.progress.label : "Task progress"}
                aria-valuemin={0}
                aria-valuemax={supplemental.progress.total}
                aria-valuenow={supplemental.progress.current}
              >
                <i
                  aria-hidden="true"
                  style={{ width: `${(supplemental.progress.current / supplemental.progress.total) * 100}%` }}
                />
              </div>
            </div>
          ) : null}
        </section>

        {present(supplemental.blocker) ? (
          <section className="agent-inspector__callout is-blocker" aria-label="Current blocker">
            <AlertTriangle aria-hidden="true" />
            <div>
              <strong>Blocker</strong>
              <p>{supplemental.blocker}</p>
            </div>
          </section>
        ) : null}

        {hasExecutionFacts ? (
          <section className="agent-inspector__section" aria-labelledby={`${headingId}-facts`}>
            <h3 id={`${headingId}-facts`}>
              <Gauge aria-hidden="true" /> Reported execution facts
            </h3>
            <dl className="agent-inspector__fact-list">
              {confidence !== null ? <DetailRow label="Confidence" value={`${confidence}%`} /> : null}
              {present(supplemental.provider) ? <DetailRow label="Provider" value={supplemental.provider} /> : null}
              {present(supplemental.model) ? <DetailRow label="Model" value={supplemental.model} /> : null}
              {present(supplemental.reportedCost) ? (
                <DetailRow
                  label={present(supplemental.costBasis) ? `Cost · ${supplemental.costBasis}` : "Cost"}
                  value={supplemental.reportedCost}
                />
              ) : null}
            </dl>
            {evidence.length > 0 ? (
              <div className="agent-inspector__evidence">
                <span><FileCheck2 aria-hidden="true" /> Evidence</span>
                <ul>
                  {evidence.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
                </ul>
              </div>
            ) : null}
          </section>
        ) : null}

        {hasActivity ? (
          <section className="agent-inspector__section" aria-labelledby={`${headingId}-activity`}>
            <h3 id={`${headingId}-activity`}>
              <Activity aria-hidden="true" /> Activity
            </h3>
            {present(agent.currentActivity) ? (
              <div className="agent-inspector__activity-summary">
                <span>Current activity</span>
                <p>{agent.currentActivity}</p>
              </div>
            ) : null}
            {present(supplemental.lastAction) ? (
              <div className="agent-inspector__activity-summary">
                <span>Last action</span>
                <p>{supplemental.lastAction}</p>
              </div>
            ) : null}
            {recentEvents.length > 0 ? (
              <ol className="agent-inspector__events" aria-label="Recent agent events">
                {recentEvents.map((event) => (
                  <li key={event.id}>
                    <i aria-hidden="true" />
                    <div>
                      <strong>{event.label}</strong>
                      {present(event.detail) ? <p>{event.detail}</p> : null}
                      {present(event.occurredAt) ? (
                        <time dateTime={event.occurredAt}>{event.occurredAt}</time>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ol>
            ) : null}
          </section>
        ) : null}

        {supplemental.pendingApproval ? (
          <section className="agent-inspector__callout is-approval" aria-label="Pending approval" role="status">
            <ShieldQuestion aria-hidden="true" />
            <div>
              <strong>Pending approval · {supplemental.pendingApproval.title}</strong>
              {present(supplemental.pendingApproval.detail) ? (
                <p>{supplemental.pendingApproval.detail}</p>
              ) : null}
              {present(supplemental.pendingApproval.requestedAt) ? (
                <time dateTime={supplemental.pendingApproval.requestedAt}>
                  Requested {supplemental.pendingApproval.requestedAt}
                </time>
              ) : null}
            </div>
          </section>
        ) : null}

        {present(supplemental.publicRationale) || warnings.length > 0 ? (
          <section className="agent-inspector__section" aria-labelledby={`${headingId}-notes`}>
            <h3 id={`${headingId}-notes`}>
              <Cpu aria-hidden="true" /> Operational notes
            </h3>
            {present(supplemental.publicRationale) ? (
              <div className="agent-inspector__rationale">
                <span>Reported rationale</span>
                <p>{supplemental.publicRationale}</p>
              </div>
            ) : null}
            {warnings.length > 0 ? (
              <ul className="agent-inspector__warnings" aria-label="Agent warnings">
                {warnings.map((warning, index) => (
                  <li key={`${index}-${warning}`}>
                    <AlertTriangle aria-hidden="true" /> {warning}
                  </li>
                ))}
              </ul>
            ) : null}
          </section>
        ) : null}
      </div>

      <footer className="agent-inspector__footer">
        This view shows reported operational summaries only. Private chain-of-thought is never exposed.
      </footer>
    </aside>
  );
}

export default AgentInspector;
