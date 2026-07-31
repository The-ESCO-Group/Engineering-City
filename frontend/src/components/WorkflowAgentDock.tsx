import type { AgentId } from "../world/ids";
import type { WorldAgent } from "../world/types";
import "./WorkflowAgentDock.css";

export interface WorkflowAgentDockProps {
  readonly agents: readonly WorldAgent[];
  readonly selectedAgentId: AgentId | null;
  readonly onSelectAgent: (agentId: AgentId) => void;
}

function initials(name: string) {
  const value = name.trim();
  if (!value) return "AI";
  return value
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

export function WorkflowAgentDock({
  agents,
  selectedAgentId,
  onSelectAgent
}: WorkflowAgentDockProps) {
  if (agents.length === 0) return null;
  return (
    <nav className="workflow-agent-dock" aria-label="Workflow agents">
      <span className="workflow-agent-dock__label">Agents</span>
      {agents.map((agent) => (
        <button
          key={agent.id}
          type="button"
          className={`is-${agent.status}`}
          aria-label={`Inspect ${agent.displayName || agent.agentKind}, ${agent.status}`}
          aria-pressed={selectedAgentId === agent.id}
          onClick={() => onSelectAgent(agent.id)}
        >
          <b aria-hidden="true">{initials(agent.displayName || agent.agentKind)}</b>
          <span role="tooltip">
            <strong>{agent.displayName || agent.agentKind}</strong>
            <small>{agent.currentActivity || agent.status}</small>
          </span>
        </button>
      ))}
    </nav>
  );
}
