import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorldTimeline, type WorldTimelineProps } from "./WorldTimeline";

const POINTS = [
  { sequence: 4, label: "Requirements accepted", occurredAt: "2026-07-31T15:00:00Z" },
  { sequence: 8, label: "PLC review started", occurredAt: "2026-07-31T15:04:00Z" },
  { sequence: 13, label: "Finding raised", occurredAt: "2026-07-31T15:08:00Z" }
] as const;

afterEach(cleanup);

function renderTimeline(overrides: Partial<WorldTimelineProps> = {}) {
  const props: WorldTimelineProps = {
    points: POINTS,
    currentIndex: 1,
    playbackState: "paused",
    playbackRate: 1,
    streamStatus: { state: "ready" },
    onPlaybackStateChange: vi.fn(),
    onPlaybackRateChange: vi.fn(),
    onScrub: vi.fn(),
    ...overrides
  };

  return { props, ...render(<WorldTimeline {...props} />) };
}

describe("WorldTimeline", () => {
  it("reports the selected index and canonical sequence for sparse event logs", () => {
    renderTimeline();

    expect(screen.getByText("2 / 3")).toBeInTheDocument();
    expect(screen.getByText("SEQ 8")).toBeInTheDocument();
    expect(screen.getByText("PLC review started")).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Replay event" })).toHaveAttribute(
      "aria-valuetext",
      "Event 2 of 3, sequence 8, PLC review started"
    );
  });

  it("emits controlled playback, speed, and scrub actions", () => {
    const { props } = renderTimeline();

    fireEvent.click(screen.getByRole("button", { name: "Play replay" }));
    fireEvent.click(screen.getByRole("button", { name: "Set playback speed to 2 times" }));
    fireEvent.change(screen.getByRole("slider", { name: "Replay event" }), {
      target: { value: "2" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Go live" }));

    expect(props.onPlaybackStateChange).toHaveBeenNthCalledWith(1, "playing");
    expect(props.onPlaybackStateChange).toHaveBeenNthCalledWith(2, "live");
    expect(props.onPlaybackRateChange).toHaveBeenCalledWith(2);
    expect(props.onScrub).toHaveBeenCalledWith({
      index: 2,
      sequence: 13,
      point: POINTS[2]
    });
  });

  it("turns the transport into a pause action while replay is playing", () => {
    const { props } = renderTimeline({ playbackState: "playing" });

    fireEvent.click(screen.getByRole("button", { name: "Pause replay" }));

    expect(props.onPlaybackStateChange).toHaveBeenCalledWith("paused");
  });

  it("seeks to the first canonical event", () => {
    const { props } = renderTimeline({ currentIndex: 2 });

    fireEvent.click(screen.getByRole("button", { name: "Go to first event" }));

    expect(props.onScrub).toHaveBeenCalledWith({
      index: 0,
      sequence: 4,
      point: POINTS[0]
    });
  });

  it("surfaces gap details without replacing the available replay", () => {
    renderTimeline({
      streamStatus: {
        state: "gap",
        expectedSequence: 9,
        firstBufferedSequence: 13,
        detail: "Catch-up is in progress."
      }
    });

    expect(screen.getByRole("status")).toHaveTextContent(
      "Waiting for sequence 9; first buffered event is sequence 13. Catch-up is in progress."
    );
    expect(screen.getByRole("slider", { name: "Replay event" })).toBeEnabled();
  });

  it("keeps live available but disables replay-only controls when no events exist", () => {
    const { props } = renderTimeline({
      points: [],
      currentIndex: -1,
      streamStatus: { state: "offline" }
    });

    expect(screen.getByText("0 / 0")).toBeInTheDocument();
    expect(screen.getByText("NO EVENTS")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go live" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Play replay" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Set playback speed to 1 times" })).toBeDisabled();
    expect(screen.getByRole("slider", { name: "Replay event" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("The event stream is offline");

    fireEvent.click(screen.getByRole("button", { name: "Go live" }));
    expect(props.onPlaybackStateChange).toHaveBeenCalledWith("live");
  });
});
