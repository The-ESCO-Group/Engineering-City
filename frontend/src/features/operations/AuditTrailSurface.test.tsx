import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AuditTrailSurface } from "./AuditTrailSurface";
import type { OperationsAuditRecord } from "./types";

function auditEvent(sequence: number): OperationsAuditRecord {
  const reviewEvent = sequence % 2 === 0;
  return {
    id: `event-${sequence}`,
    sequence,
    schemaVersion: 1,
    eventType: reviewEvent ? "approval.requested" : "task.created",
    category: reviewEvent ? "Review" : "Tasks",
    summary: reviewEvent ? `review-only ${sequence}` : `task-only ${sequence}`,
    signal: reviewEvent ? "attention" : "info",
    occurredAt: "2026-08-03T12:00:00Z",
    projectId: "project-1",
    agentId: null,
    taskId: null,
    actorUserId: null,
    payload: { sequence },
    searchableText: reviewEvent ? `approval.requested review-only ${sequence}` : `task.created task-only ${sequence}`,
    gapBefore: false
  };
}

function eventRows(): HTMLElement[] {
  const list = screen.getByRole("list", { name: "Recent project events" });
  return within(list).getAllByText(/^SEQ /).map((sequence) => sequence.closest("li")!);
}

afterEach(cleanup);

describe("AuditTrailSurface pagination", () => {
  it("renders two hundred rows at a time, keeps total filter counts, and resets the window when its inputs change", () => {
    const events = Array.from({ length: 450 }, (_, index) => auditEvent(index + 1));
    const { rerender } = render(
      <AuditTrailSurface events={events} firstSequence={1} latestSequence={450} sequenceGapCount={0} />
    );

    expect(eventRows()).toHaveLength(200);
    expect(screen.getByText("450 of 450", { selector: "output" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Load 200 more/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Load 200 more/ }));
    expect(eventRows()).toHaveLength(400);
    expect(screen.getByRole("button", { name: /Load 50 more/ })).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/Search event type, sequence, IDs, or payload/), {
      target: { value: "review-only" }
    });
    expect(eventRows()).toHaveLength(200);
    expect(screen.getByText("225 of 450", { selector: "output" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Load 25 more/ })).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/Search event type, sequence, IDs, or payload/), {
      target: { value: "" }
    });
    expect(eventRows()).toHaveLength(200);
    fireEvent.click(screen.getByRole("button", { name: /Load 200 more/ }));
    expect(eventRows()).toHaveLength(400);

    fireEvent.click(screen.getByRole("button", { name: "Newest first" }));
    expect(eventRows()).toHaveLength(200);
    expect(eventRows()[0]).toHaveTextContent("SEQ 1");

    fireEvent.click(screen.getByRole("button", { name: /Load 200 more/ }));
    expect(eventRows()).toHaveLength(400);
    const updatedEvents = [...events, auditEvent(451)];
    rerender(
      <AuditTrailSurface events={updatedEvents} firstSequence={1} latestSequence={451} sequenceGapCount={0} />
    );
    expect(eventRows()).toHaveLength(400);
    expect(screen.getByText("451 of 451", { selector: "output" })).toBeInTheDocument();
  });
});
