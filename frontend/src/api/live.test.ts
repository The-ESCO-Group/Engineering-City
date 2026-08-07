import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, engineeringApi } from "./client";
import type { EventEnvelope } from "./types";
import {
  deriveContiguousEventCursor,
  invalidationKeysForEvent,
  mergeContiguousEvents,
  persistContiguousEventHistory,
  retainEventHistory,
  useValidationWaiver
} from "./live";

function event(
  seq: number,
  eventType: EventEnvelope["event_type"] = "task.created"
): EventEnvelope {
  return {
    id: `event-${seq}`,
    schema_version: 1,
    seq,
    event_type: eventType,
    project_id: "project-1",
    payload: {},
    occurred_at: "2026-08-03T12:00:00Z"
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

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

describe("real-time cache convergence", () => {
  const downstreamKeys = [
    "approvals",
    "tasks",
    "workflow",
    "project",
    "validation-results",
    "release-readiness",
    "release-candidates",
    "release-artifacts"
  ];

  it.each([
    ["approval.decided", true],
    ["gate.status_changed", false]
  ] as const)("invalidates every downstream query for %s", (eventType, includesFindings) => {
    const keys = invalidationKeysForEvent(event(1, eventType), "project-1");

    downstreamKeys.forEach((resource) => {
      expect(keys).toContainEqual(["engineering", "project-1", resource]);
    });
    expect(keys).toContainEqual(["engineering", "projects"]);
    expect(keys).toHaveLength(downstreamKeys.length + 1 + (includesFindings ? 1 : 0));
    if (includesFindings) {
      expect(keys).toContainEqual(["engineering", "project-1", "findings"]);
    } else {
      expect(keys).not.toContainEqual(["engineering", "project-1", "findings"]);
    }
  });

  it("refetches validation results after a stale waiver conflict", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } }
    });
    const refetch = vi.spyOn(queryClient, "refetchQueries").mockResolvedValue(undefined);
    vi.spyOn(engineeringApi, "waiveValidationResult").mockRejectedValueOnce(
      new ApiError("The validation result changed.", 409, "conflict")
    );
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useValidationWaiver("project-1"), { wrapper });

    await act(async () => {
      await expect(
        result.current.mutateAsync({
          resultId: "validation-result-1",
          reason: "Reviewed against the revised evidence.",
          expectedUpdatedAt: "2026-08-05T12:00:00Z"
        })
      ).rejects.toMatchObject({ status: 409 });
    });

    expect(refetch).toHaveBeenCalledWith({
      queryKey: ["engineering", "project-1", "validation-results"],
      type: "all"
    });
    queryClient.clear();
  });
});
