import type { CSSProperties } from "react";
import type { BuildingId } from "../domain";
import "./CityMapHud.css";

interface DistrictLabel {
  id: BuildingId;
  label: string;
  detail: string;
  x: string;
  y: string;
}

export interface CityOperationsSummary {
  activeReviews: number;
  openFindings: number;
  pendingApprovals: number;
  releaseReadiness: number;
}

export interface CityMapHudProps {
  selectedBuilding: BuildingId | null;
  selectBuilding: (id: BuildingId) => void;
  projectLabel?: string;
  summary?: Partial<CityOperationsSummary>;
  className?: string;
}

const districts: DistrictLabel[] = [
  { id: "intake", label: "North Review", detail: "Project intake · 1 queued", x: "18%", y: "24%" },
  { id: "automation", label: "Automation Core", detail: "Logic review · 1 active", x: "37%", y: "18%" },
  { id: "hmi", label: "HMI District", detail: "Tag review · 1 active", x: "68%", y: "25%" },
  { id: "electrical", label: "Electrical Works", detail: "I/O review · 1 active", x: "84%", y: "48%" },
  { id: "qa", label: "QA Campus", detail: "Validation · 1 waiting", x: "67%", y: "68%" },
  { id: "documentation", label: "Documentation Quarter", detail: "Reports · package ready", x: "39%", y: "76%" },
  { id: "approval", label: "Approval Plaza", detail: "Human gate · 2 pending", x: "18%", y: "61%" },
  { id: "release", label: "Release Logistics", detail: "Delivery · gated", x: "80%", y: "78%" },
];

const defaultSummary: CityOperationsSummary = {
  activeReviews: 3,
  openFindings: 12,
  pendingApprovals: 2,
  releaseReadiness: 68,
};

export function CityMapHud({
  selectedBuilding,
  selectBuilding,
  projectLabel = "Line 4 operations city",
  summary,
  className,
}: CityMapHudProps) {
  const operations = { ...defaultSummary, ...summary };
  const rootClassName = ["city-map-hud", className].filter(Boolean).join(" ");

  return (
    <div className={rootClassName} role="group" aria-label="Engineering city map controls">
      <nav className="city-map-hud__districts" aria-label="Engineering workflow districts">
        {districts.map((district) => {
          const selected = selectedBuilding === district.id;
          const position = {
            "--city-map-hud-x": district.x,
            "--city-map-hud-y": district.y,
          } as CSSProperties;

          return (
            <button
              key={district.id}
              type="button"
              className={`city-map-hud__district${selected ? " is-selected" : ""}`}
              style={position}
              aria-label={`${district.label}: ${district.detail}`}
              aria-pressed={selected}
              onClick={() => selectBuilding(district.id)}
            >
              <span className="city-map-hud__district-name">{district.label}</span>
              <span className="city-map-hud__district-detail">{district.detail}</span>
            </button>
          );
        })}
      </nav>

      <aside className="city-map-hud__operations" aria-label="City operations summary">
        <header>
          <span>City overview</span>
          <small>Demo</small>
        </header>
        <p title={projectLabel}>{projectLabel}</p>
        <dl>
          <div>
            <dt>Active reviews</dt>
            <dd>{operations.activeReviews}</dd>
          </div>
          <div className="is-alert">
            <dt>Open findings</dt>
            <dd>{operations.openFindings}</dd>
          </div>
          <div>
            <dt>Pending approvals</dt>
            <dd>{operations.pendingApprovals}</dd>
          </div>
          <div>
            <dt>Release readiness</dt>
            <dd>{operations.releaseReadiness}%</dd>
          </div>
        </dl>
        <div
          className="city-map-hud__readiness"
          role="img"
          aria-label={`Release readiness ${operations.releaseReadiness} percent`}
        >
          <span style={{ width: `${Math.max(0, Math.min(100, operations.releaseReadiness))}%` }} />
        </div>
        <button
          type="button"
          className="city-map-hud__action"
          onClick={() => selectBuilding("qa")}
          aria-label="View active alerts in QA Campus"
        >
          Review alerts
        </button>
      </aside>
    </div>
  );
}
