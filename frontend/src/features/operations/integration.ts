import type { EngineeringData } from "../../api/live";
import type { OperationsSource } from "./types";

type OperationsEngineeringData = Pick<
  EngineeringData,
  "dataMode" | "fallbackReason" | "project" | "agents" | "tasks" | "messages" | "events"
>;

export function operationsSourceFromEngineering(data: OperationsEngineeringData): OperationsSource {
  return {
    dataMode: data.dataMode,
    fallbackReason: data.fallbackReason,
    project: data.project,
    agents: data.agents,
    tasks: data.tasks,
    messages: data.messages,
    events: data.events
  };
}
