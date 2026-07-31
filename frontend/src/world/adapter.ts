import type { EventEnvelope } from "../generated";
import { createWorldState, reduceWorldEvent, reduceWorldEvents } from "./reducer";
import {
  replayWorldEvents,
  selectWorldReplayView,
  type WorldReplayOptions,
  type WorldReplayView
} from "./replay";
import {
  restoreWorldStateSnapshot,
  serializeWorldStateSnapshot
} from "./snapshot";
import type { WorldState, WorldStateOptions } from "./types";

export interface WorldStateAdapterOptions extends WorldStateOptions {
  /** Optional validated local snapshot used before REST/WebSocket catch-up. */
  readonly snapshot?: string;
}

export interface WorldStateAdapter {
  readonly projectId: string;
  getState(): WorldState;
  getView(): WorldReplayView;
  ingest(envelope: EventEnvelope): WorldState;
  ingestMany(envelopes: readonly EventEnvelope[]): WorldState;
  /** Authoritative replacement used after a gap/conflict resync response. */
  replaceFromReplay(
    envelopes: readonly EventEnvelope[],
    options?: Omit<WorldReplayOptions, "limits">
  ): WorldState;
  serializeSnapshot(): string;
  restoreSnapshot(snapshot: string): boolean;
  subscribe(listener: () => void): () => void;
}

/**
 * Framework-neutral adapter. It notifies once per batch and keeps snapshots
 * referentially stable, so React can bridge it with useSyncExternalStore.
 */
export function createWorldStateAdapter(
  projectId: string,
  options: WorldStateAdapterOptions = {}
): WorldStateAdapter {
  const limits = options.limits;
  const restored = options.snapshot
    ? restoreWorldStateSnapshot(options.snapshot, { limits })
    : null;
  let state =
    restored !== null && restored.projectId === projectId
      ? restored
      : createWorldState(projectId, {
          afterSequence: options.afterSequence,
          limits
        });
  const listeners = new Set<() => void>();

  const publish = (next: WorldState) => {
    if (next === state) return state;
    state = next;
    listeners.forEach((listener) => listener());
    return state;
  };

  return {
    projectId,
    getState: () => state,
    getView: () => selectWorldReplayView(state),
    ingest: (envelope) => publish(reduceWorldEvent(state, envelope)),
    ingestMany: (envelopes) => publish(reduceWorldEvents(state, envelopes)),
    replaceFromReplay: (envelopes, replayOptions = {}) =>
      publish(
        replayWorldEvents(projectId, envelopes, {
          ...replayOptions,
          limits
        })
      ),
    serializeSnapshot: () => serializeWorldStateSnapshot(state),
    restoreSnapshot: (snapshot) => {
      const candidate = restoreWorldStateSnapshot(snapshot, { limits });
      if (candidate === null || candidate.projectId !== projectId) return false;
      publish(candidate);
      return true;
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    }
  };
}
