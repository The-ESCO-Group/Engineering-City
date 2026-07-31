import { useId, type ChangeEvent } from "react";
import type {
  ResolvedWorldQualityMode,
  WorldQualityMode,
  WorldQualityProfile
} from "../world/quality";
import "./WorldQualityControl.css";

export interface WorldQualityFpsStatus {
  readonly currentFps: number;
}

export interface WorldQualityControlProps {
  readonly requested: WorldQualityMode;
  readonly resolved: ResolvedWorldQualityMode;
  readonly profile: WorldQualityProfile;
  readonly fpsStatus?: WorldQualityFpsStatus | null;
  readonly onChange: (mode: WorldQualityMode) => void;
  readonly disabled?: boolean;
  readonly autoFocus?: boolean;
  readonly className?: string;
}

const MODE_LABELS: Record<WorldQualityMode, string> = {
  auto: "Auto",
  high: "High",
  balanced: "Balanced",
  performance: "Performance"
};

type FpsTone = "target" | "fallback" | "below-fallback" | "unknown";

function fpsPresentation(
  fpsStatus: WorldQualityFpsStatus | null | undefined,
  profile: WorldQualityProfile
): { tone: FpsTone; label: string } {
  const current = fpsStatus?.currentFps;
  if (current === undefined || !Number.isFinite(current) || current < 0) {
    return { tone: "unknown", label: "FPS not sampled" };
  }

  const rounded = Math.round(current);
  if (current >= profile.targetFps) {
    return { tone: "target", label: `${rounded} FPS · on target` };
  }
  if (current >= profile.fallbackFps) {
    return { tone: "fallback", label: `${rounded} FPS · fallback range` };
  }
  return { tone: "below-fallback", label: `${rounded} FPS · below fallback` };
}

export function WorldQualityControl({
  requested,
  resolved,
  profile,
  fpsStatus,
  onChange,
  disabled = false,
  autoFocus = false,
  className = ""
}: WorldQualityControlProps) {
  const controlId = useId();
  const descriptionId = `${controlId}-description`;
  const fps = fpsPresentation(fpsStatus, profile);
  const resolvedLabel = MODE_LABELS[resolved];
  const summary = `${resolvedLabel} · ${profile.pixelRatioCap}× pixels · ${profile.shadows} shadows`;

  const changeMode = (event: ChangeEvent<HTMLSelectElement>) => {
    onChange(event.currentTarget.value as WorldQualityMode);
  };

  return (
    <section
      className={`world-quality-control ${className}`.trim()}
      aria-label="World rendering quality"
      data-quality-mode={resolved}
      data-fps-state={fps.tone}
    >
      <div className="world-quality-control__field">
        <label htmlFor={controlId}>World quality</label>
        <select
          id={controlId}
          value={requested}
          onChange={changeMode}
          aria-describedby={descriptionId}
          disabled={disabled}
          autoFocus={autoFocus}
        >
          <option value="auto">Auto</option>
          <option value="high">High</option>
          <option value="balanced">Balanced</option>
          <option value="performance">Performance</option>
        </select>
      </div>

      <div className="world-quality-control__status" id={descriptionId}>
        <span className="world-quality-control__resolved">
          {requested === "auto" ? `Auto resolved to ${resolvedLabel}` : summary}
        </span>
        {requested === "auto" && (
          <span className="world-quality-control__profile-summary">{summary}</span>
        )}
        <span className="world-quality-control__budget">
          Target {profile.targetFps} FPS · fallback {profile.fallbackFps} FPS
        </span>
        {fpsStatus ? (
          <span className="world-quality-control__fps">
            <i aria-hidden="true" />
            {fps.label}
          </span>
        ) : null}
      </div>
    </section>
  );
}
