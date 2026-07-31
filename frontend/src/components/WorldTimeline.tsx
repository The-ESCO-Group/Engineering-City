import { Pause, Play, Radio, SkipBack } from "lucide-react";
import type { ChangeEvent } from "react";
import "./WorldTimeline.css";

export type WorldTimelinePlaybackState = "live" | "playing" | "paused";

export type WorldTimelinePlaybackRate = 0.5 | 1 | 2;

export interface WorldTimelinePoint {
  /** Canonical event sequence from the project event stream. */
  readonly sequence: number;
  /** Optional concise description supplied by the caller. */
  readonly label?: string;
  /** Optional source timestamp; rendered as provided so the timeline stays locale-neutral. */
  readonly occurredAt?: string;
}

export type WorldTimelineStreamStatus =
  | { readonly state: "ready" }
  | {
      readonly state: "gap";
      readonly expectedSequence: number;
      readonly firstBufferedSequence: number;
      readonly detail?: string;
    }
  | { readonly state: "stale"; readonly detail?: string }
  | { readonly state: "offline"; readonly detail?: string };

export interface WorldTimelineScrubTarget {
  readonly index: number;
  readonly sequence: number;
  readonly point: WorldTimelinePoint;
}

export interface WorldTimelineProps {
  /** Points must be in the order the caller wants to replay them. */
  readonly points: readonly WorldTimelinePoint[];
  /** Zero-based point index. Use -1 while a non-empty log has not applied an event yet. */
  readonly currentIndex: number;
  readonly playbackState: WorldTimelinePlaybackState;
  readonly playbackRate: WorldTimelinePlaybackRate;
  readonly streamStatus: WorldTimelineStreamStatus;
  readonly onPlaybackStateChange: (state: WorldTimelinePlaybackState) => void;
  readonly onPlaybackRateChange: (rate: WorldTimelinePlaybackRate) => void;
  readonly onScrub: (target: WorldTimelineScrubTarget) => void;
  readonly disabled?: boolean;
  readonly className?: string;
  readonly ariaLabel?: string;
}

const PLAYBACK_RATES: readonly WorldTimelinePlaybackRate[] = [0.5, 1, 2];

function clampIndex(index: number, total: number): number {
  if (total === 0) return 0;
  return Math.min(Math.max(Math.round(index), 0), total - 1);
}

function describePoint(
  point: WorldTimelinePoint | undefined,
  displayIndex: number,
  total: number
): string {
  if (!point) return "No events available";

  const parts = [`Event ${displayIndex + 1} of ${total}`, `sequence ${point.sequence}`];
  if (point.label) parts.push(point.label);
  return parts.join(", ");
}

function statusNotice(status: WorldTimelineStreamStatus): string | null {
  switch (status.state) {
    case "ready":
      return null;
    case "gap":
      return [
        `Waiting for sequence ${status.expectedSequence}; first buffered event is sequence ${status.firstBufferedSequence}.`,
        status.detail
      ]
        .filter(Boolean)
        .join(" ");
    case "stale":
      return ["The event stream is stale. Replay remains available.", status.detail]
        .filter(Boolean)
        .join(" ");
    case "offline":
      return ["The event stream is offline. Recorded events remain available.", status.detail]
        .filter(Boolean)
        .join(" ");
  }
}

export function WorldTimeline({
  points,
  currentIndex,
  playbackState,
  playbackRate,
  streamStatus,
  onPlaybackStateChange,
  onPlaybackRateChange,
  onScrub,
  disabled = false,
  className = "",
  ariaLabel = "World event timeline"
}: WorldTimelineProps) {
  const total = points.length;
  const safeIndex = clampIndex(currentIndex, total);
  const currentPoint = points[safeIndex];
  const notice = statusNotice(streamStatus);
  const replayDisabled = disabled || total === 0;
  const scrubDisabled = disabled || total < 2;

  const scrubTo = (event: ChangeEvent<HTMLInputElement>) => {
    const index = clampIndex(Number(event.currentTarget.value), total);
    const point = points[index];
    if (!point) return;
    onScrub({ index, sequence: point.sequence, point });
  };

  const togglePlayback = () => {
    onPlaybackStateChange(playbackState === "playing" ? "paused" : "playing");
  };

  return (
    <section
      className={`world-timeline ${className}`.trim()}
      aria-label={ariaLabel}
      data-playback-state={playbackState}
      data-stream-state={streamStatus.state}
    >
      <div className="world-timeline__transport" aria-label="Timeline playback controls">
        <button
          type="button"
          className="world-timeline__control world-timeline__control--live"
          aria-label="Go live"
          aria-pressed={playbackState === "live"}
          disabled={disabled}
          onClick={() => onPlaybackStateChange("live")}
        >
          <Radio aria-hidden="true" size={14} />
          Live
        </button>

        <button
          type="button"
          className="world-timeline__icon-control"
          aria-label={playbackState === "playing" ? "Pause replay" : "Play replay"}
          disabled={replayDisabled}
          onClick={togglePlayback}
        >
          {playbackState === "playing" ? (
            <Pause aria-hidden="true" size={15} />
          ) : (
            <Play aria-hidden="true" size={15} />
          )}
        </button>

        <button
          type="button"
          className="world-timeline__icon-control"
          aria-label="Go to first event"
          disabled={replayDisabled || safeIndex === 0}
          onClick={() => {
            const point = points[0];
            if (point) onScrub({ index: 0, sequence: point.sequence, point });
          }}
        >
          <SkipBack aria-hidden="true" size={15} />
        </button>
      </div>

      <div className="world-timeline__track">
        <div className="world-timeline__track-heading">
          <span className="world-timeline__position" aria-live="polite">
            {currentPoint ? `${safeIndex + 1} / ${total}` : "0 / 0"}
          </span>
          <span className="world-timeline__sequence">
            {currentPoint ? `SEQ ${currentPoint.sequence}` : "NO EVENTS"}
          </span>
        </div>

        <input
          className="world-timeline__scrubber"
          type="range"
          min={0}
          max={Math.max(total - 1, 0)}
          step={1}
          value={safeIndex}
          disabled={scrubDisabled}
          aria-label="Replay event"
          aria-valuetext={describePoint(currentPoint, safeIndex, total)}
          onChange={scrubTo}
        />

        <div className="world-timeline__point-detail">
          <span>{currentPoint?.label ?? "No recorded event selected"}</span>
          {currentPoint?.occurredAt ? <time dateTime={currentPoint.occurredAt}>{currentPoint.occurredAt}</time> : null}
        </div>
      </div>

      <div className="world-timeline__rates" aria-label="Playback speed">
        {PLAYBACK_RATES.map((rate) => (
          <button
            key={rate}
            type="button"
            aria-label={`Set playback speed to ${rate} times`}
            aria-pressed={playbackRate === rate}
            disabled={replayDisabled}
            onClick={() => onPlaybackRateChange(rate)}
          >
            {rate}×
          </button>
        ))}
      </div>

      {notice ? (
        <p className="world-timeline__notice" role="status">
          {notice}
        </p>
      ) : null}
    </section>
  );
}
