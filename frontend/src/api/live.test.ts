import { describe, expect, it } from "vitest";
import type { EventEnvelope } from "./types";
import {
  deriveContiguousEventCursor,
  mergeContiguousEvents,
  persistContiguousEventHistory,
  retainEventHistory
} from "./live";

function event(seq: number): EventEnvelope {
  return {
    id: `event-${seq}`,
    schema_version: 1,
    seq,
    event_type: "task.created",
    project_id: "project-1",
    payload: {},
    occurred_at: "2026-08-03T12:00:00Z"
  };
}

describe("live event history", () => {
  it("retains the latest ten thousand events in sequence order", () => {
    const input = Array.from({ length: 10_002 }, (_, index) => event(index + 1)).reverse();

    const retained = retainEventHistory(input);

    expect(retained).toHaveLength(10_000);
    expect(retained[0]?.seq).toBe(3);
    expect(retained.at(-1)?.seq).toBe(10_002);
    expect(input[0]?.seq).toBe(10_002);
    expect(deriveContiguousEventCursor(retained)).toBe(10_002);
  });

  it("buffers a later live event until delayed replay closes the sequence gap", () => {
    const live = mergeContiguousEvents(10, new Map(), [event(12)]);

    expect(live.cursor).toBe(10);
    expect(live.committed).toEqual([]);
    expect([...live.pending.keys()]).toEqual([12]);

    const replay = mergeContiguousEvents(live.cursor, live.pending, [event(11), event(12)]);

    expect(replay.cursor).toBe(12);
    expect(replay.committed.map((item) => item.seq)).toEqual([11, 12]);
    expect(replay.pending.size).toBe(0);
  });

  it("does not trust a persisted partial history past its first gap", () => {
    expect(deriveContiguousEventCursor([event(1), event(2), event(4)])).toBe(2);
    expect(deriveContiguousEventCursor([event(12)])).toBe(0);
  });

  it("writes retained history before its cursor so quota failures replay safely", () => {
    const writes: string[] = [];
    const storage: Pick<Storage, "setItem"> = {
      setItem(key) {
        writes.push(key);
        if (key.includes("event-history")) throw new Error("quota exceeded");
      }
    };

    expect(persistContiguousEventHistory(storage, "project-1", [event(11)], 11)).toBe(false);
    expect(writes).toEqual(["esco:event-history:project-1"]);
  });
});
