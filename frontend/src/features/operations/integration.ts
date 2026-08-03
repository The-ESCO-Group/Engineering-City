import type { EngineeringData } from "../../api/live";
import type { OperationsSource } from "./types";

type OperationsEngineeringData = Pick<
  EngineeringData,
  "dataMode" | "fallbackReason" | "project" | "agentDefinitions" | "agents" | "agentRuns" | "tasks" | "messages" | "events"
>;

export function operationsSourceFromEngineering(data: OperationsEngineeringData): OperationsSource {
  return {
    dataMode: data.dataMode,
    fallbackReason: data.fallbackReason,
    project: data.project,
    agentDefinitions: data.agentDefinitions,
    agents: data.agents,
    agentRuns: data.agentRuns,
    tasks: data.tasks,
    messages: data.messages,
    events: data.events
  };
}
