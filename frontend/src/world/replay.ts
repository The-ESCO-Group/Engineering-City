import type { EventEnvelope } from "../generated";
import {
  createWorldState,
  reduceWorldEvents,
  selectWorldAgents,
  selectWorldLocations,
  selectWorldPackages,
  selectWorldTasks,
  worldSyncStatus
} from "./reducer";
import type { WorldState, WorldStateOptions } from "./types";

export interface WorldReplayOptions extends WorldStateOptions {
  /** Inclusive sequence cursor used by timeline scrubbers. */
  readonly throughSequence?: number;
}

export interface WorldReplayView {
  readonly state: WorldState;
  readonly agents: ReturnType<typeof selectWorldAgents>;
  readonly tasks: ReturnType<typeof selectWorldTasks>;
  readonly packages: ReturnType<typeof selectWorldPackages>;
  readonly locations: ReturnType<typeof selectWorldLocations>;
  readonly sync: ReturnType<typeof worldSyncStatus>;
}

export function selectWorldReplayView(state: WorldState): WorldReplayView {
  return {
    state,
    agents: selectWorldAgents(state),
    tasks: selectWorldTasks(state),
    packages: selectWorldPackages(state),
    locations: selectWorldLocations(state),
    sync: worldSyncStatus(state)
  };
}

/** Replays a copied, canonically ordered log without mutating the caller's array. */
export function replayWorldEvents(
  projectId: string,
  envelopes: readonly EventEnvelope[],
  options: WorldReplayOptions = {}
): WorldState {
  const throughSequence = options.throughSequence ?? Number.MAX_SAFE_INTEGER;
  if (!Number.isSafeInteger(throughSequence) || throughSequence < 0) {
    throw new TypeError("throughSequence must be a non-negative safe integer.");
  }
  const ordered = [...envelopes]
    .filter((event) => event.project_id === projectId && event.seq <= throughSequence)
    .sort((left, right) => left.seq - right.seq || left.id.localeCompare(right.id));
  return reduceWorldEvents(
    createWorldState(projectId, {
      afterSequence: options.afterSequence,
      limits: options.limits
    }),
    ordered
  );
}

/** Projection used by both the live city and a timeline-selected event prefix. */
export function deriveWorldReplayView(
  projectId: string,
  envelopes: readonly EventEnvelope[],
  options: WorldReplayOptions = {}
): WorldReplayView {
  const state = replayWorldEvents(projectId, envelopes, options);
  return selectWorldReplayView(state);
}
