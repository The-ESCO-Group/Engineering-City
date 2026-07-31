import { useMemo, useSyncExternalStore } from "react";
import type { EventEnvelope } from "../generated";
import type { WorldStateAdapter } from "./adapter";
import {
  deriveWorldReplayView,
  selectWorldReplayView,
  type WorldReplayOptions,
  type WorldReplayView
} from "./replay";
import type { WorldState } from "./types";

/** Stable React bridge for incremental live ingestion. */
export function useWorldAdapterState(adapter: WorldStateAdapter): WorldState {
  return useSyncExternalStore(adapter.subscribe, adapter.getState, adapter.getState);
}

export function useWorldAdapterView(adapter: WorldStateAdapter): WorldReplayView {
  const state = useWorldAdapterState(adapter);
  return useMemo(() => selectWorldReplayView(state), [state]);
}

/** Deterministically derives a timeline view from an inclusive selected sequence. */
export function useWorldReplayView(
  projectId: string,
  envelopes: readonly EventEnvelope[],
  options: WorldReplayOptions = {}
): WorldReplayView {
  const afterSequence = options.afterSequence;
  const throughSequence = options.throughSequence;
  const limits = options.limits;
  return useMemo(
    () =>
      deriveWorldReplayView(projectId, envelopes, {
        afterSequence,
        throughSequence,
        limits: limits
          ? {
              maxPendingEvents: limits.maxPendingEvents,
              maxRecentEvents: limits.maxRecentEvents,
              maxAgents: limits.maxAgents,
              maxTasks: limits.maxTasks,
              maxLocations: limits.maxLocations
            }
          : undefined
      }),
    [
      projectId,
      envelopes,
      afterSequence,
      throughSequence,
      limits
    ]
  );
}
