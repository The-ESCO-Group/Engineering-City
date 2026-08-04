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

interface ScenicLabel {
  label: string;
  x: string;
  y: string;
  align?: "right";
}

export interface CityOperationsSummary {
  activeAgents: number;
  openFindings: number;
  pendingApprovals: number;
  releaseReadiness: number | null;
}

export interface CityMapHudProps {
  selectedBuilding: BuildingId | null;
  selectBuilding: (id: BuildingId) => void;
  projectLabel?: string;
  summary?: Partial<CityOperationsSummary>;
  dataMode?: "demo" | "live" | "unavailable";
  className?: string;
  labelDensity?: number;
  labelPositions?: Record<string, { deltaX: number; deltaY: number; visible: boolean }>;
}

const districts: DistrictLabel[] = [
  { id: "intake", label: "Project Intake", detail: "Arrival gate · 1 queued", x: "28.6%", y: "37.4%" },
  { id: "automation", label: "Automation Core", detail: "Logic review · 1 active", x: "41.8%", y: "33.4%" },
  { id: "hmi", label: "HMI District", detail: "Tag review · 1 active", x: "64.8%", y: "35%" },
  { id: "electrical", label: "Electrical Works", detail: "I/O review · 1 active", x: "76.7%", y: "40.7%" },
  { id: "qa", label: "QA Campus", detail: "Validation · 1 waiting", x: "64.1%", y: "50.5%" },
  { id: "documentation", label: "Documentation Quarter", detail: "Reports · package ready", x: "40.7%", y: "54.9%" },
  { id: "approval", label: "Approval Plaza", detail: "Human gate · 2 pending", x: "26.9%", y: "46.9%" },
  { id: "release", label: "Release Logistics", detail: "Delivery · gated", x: "73.8%", y: "57.5%" },
];

const scenicLabels: ScenicLabel[] = [
  { label: "North Gateway", x: "17.9%", y: "21.7%" },
  { label: "Research Park", x: "30.9%", y: "23.6%" },
  { label: "Template Library", x: "34.1%", y: "27.3%" },
  { label: "Requirements Hall", x: "33.4%", y: "32.1%" },
  { label: "Innovation District", x: "45.4%", y: "20%" },
  { label: "Tag Foundry", x: "48.2%", y: "27.1%" },
  { label: "Ladder Logic Lab", x: "52.6%", y: "32.2%" },
  { label: "ESCO Project Core", x: "50.5%", y: "43.8%" },
  { label: "Energy Plant", x: "65.3%", y: "20.4%" },
  { label: "Water Treatment", x: "85.6%", y: "22.7%" },
  { label: "Alarm Center", x: "75.4%", y: "43.8%" },
  { label: "Industrial Zone", x: "89.7%", y: "36.2%" },
  { label: "South Gateway", x: "96.2%", y: "49.8%", align: "right" },
  { label: "Central Park", x: "51.8%", y: "59.9%" },
  { label: "Maintenance Depot", x: "92.3%", y: "64.6%", align: "right" },
  { label: "Design District", x: "22.4%", y: "65.8%" },
  { label: "Training Academy", x: "36.2%", y: "65.7%" },
  { label: "Supply Chain Hub", x: "67%", y: "70.1%" },
  { label: "Wind Farm", x: "5%", y: "68%" },
  { label: "Testing Grounds", x: "29.5%", y: "75.4%" },
  { label: "Data Center", x: "52.8%", y: "79.7%" },
  { label: "Port Terminal", x: "79.6%", y: "82.1%" },
];

const coreScenicLabelNames = new Set([
  "Template Library",
  "Requirements Hall",
  "Tag Foundry",
  "Ladder Logic Lab",
  "ESCO Project Core",
  "Alarm Center",
  "Central Park",
  "Training Academy",
  "Testing Grounds",
  "Data Center"
]);

const prioritizedScenicLabels = [
  ...scenicLabels.filter((label) => coreScenicLabelNames.has(label.label)),
  ...scenicLabels.filter((label) => !coreScenicLabelNames.has(label.label))
];

const defaultSummary: CityOperationsSummary = {
  activeAgents: 5,
  openFindings: 12,
  pendingApprovals: 7,
  releaseReadiness: 68,
};

const districtPurposes: Record<BuildingId, string> = {
  intake: "Project intake",
  automation: "PLC and logic",
  hmi: "HMI engineering",
  electrical: "Devices and I/O",
  qa: "Independent review",
  documentation: "Reports and records",
  approval: "Named human decisions",
  release: "Release logistics"
};

export function CityMapHud({
  selectedBuilding,
  selectBuilding,
  projectLabel = "Line 4 operations city",
  summary,
  dataMode = "demo",
  className,
  labelDensity = 1,
  labelPositions,
}: CityMapHudProps) {
  const operations = dataMode === "demo"
    ? { ...defaultSummary, ...summary }
    : { activeAgents: 0, openFindings: 0, pendingApprovals: 0, releaseReadiness: null, ...summary };
  const hasProjectData = dataMode !== "unavailable";
  const rootClassName = ["city-map-hud", className].filter(Boolean).join(" ");
  const scenicLabelCount = Math.max(
    coreScenicLabelNames.size,
    Math.min(scenicLabels.length, Math.round(scenicLabels.length * labelDensity))
  );

  return (
    <div className={rootClassName} role="group" aria-label="Engineering city map controls">
      <nav className="city-map-hud__districts" aria-label="Engineering workflow districts">
        {districts.map((district) => {
          const selected = selectedBuilding === district.id;
          const detail = dataMode === "demo"
            ? district.detail
            : dataMode === "live"
              ? districtPurposes[district.id]
              : "Project data unavailable";
          const projection = labelPositions?.[district.id];
          const position = {
            "--city-map-hud-x": projection
              ? `calc(${district.x} + ${projection.deltaX.toFixed(3)}%)`
              : district.x,
            "--city-map-hud-y": projection
              ? `calc(${district.y} + ${projection.deltaY.toFixed(3)}%)`
              : district.y,
          } as CSSProperties;

          return (
            <button
              key={district.id}
              type="button"
              className={`city-map-hud__district${selected ? " is-selected" : ""}${projection && !projection.visible ? " is-offscreen" : ""}`}
              style={position}
              aria-label={`${district.label}: ${detail}`}
              aria-pressed={selected}
              onClick={() => selectBuilding(district.id)}
            >
              <span className="city-map-hud__district-name">{district.label}</span>
              <span className="city-map-hud__district-detail">{detail}</span>
            </button>
          );
        })}
      </nav>

      <div className="city-map-hud__regions" aria-hidden="true">
        {prioritizedScenicLabels.slice(0, scenicLabelCount).map((region) => {
          const projection = labelPositions?.[region.label];
          return (
            <span
              key={region.label}
              className={`city-map-hud__region${region.align === "right" ? " is-right-aligned" : ""}${projection && !projection.visible ? " is-offscreen" : ""}`}
              style={{
                "--city-map-hud-x": projection
                  ? `calc(${region.x} + ${projection.deltaX.toFixed(3)}%)`
                  : region.x,
                "--city-map-hud-y": projection
                  ? `calc(${region.y} + ${projection.deltaY.toFixed(3)}%)`
                  : region.y,
              } as CSSProperties}
            >
              {region.label}
            </span>
          );
        })}
      </div>

      <aside className="city-map-hud__operations" aria-label="City operations summary">
        <header>
          <span>City overview</span>
          <small>{dataMode === "demo" ? "Demo" : dataMode === "live" ? "Local" : "Offline"}</small>
        </header>
        <p title={projectLabel}>{projectLabel}</p>
        <dl>
          <div>
            <dt>Agents deployed</dt>
            <dd>{hasProjectData ? operations.activeAgents : "—"}</dd>
          </div>
          <div className="is-alert">
            <dt>Open findings</dt>
            <dd>{hasProjectData ? operations.openFindings : "—"}</dd>
          </div>
          <div>
            <dt>Pending approvals</dt>
            <dd>{hasProjectData ? operations.pendingApprovals : "—"}</dd>
          </div>
          <div>
            <dt>Release readiness</dt>
            <dd>{operations.releaseReadiness === null ? "Not reported" : `${operations.releaseReadiness}%`}</dd>
          </div>
        </dl>
        <div
          className="city-map-hud__readiness"
          role="img"
          aria-label={operations.releaseReadiness === null ? "Release readiness not reported" : `Release readiness ${operations.releaseReadiness} percent`}
        >
          <span style={{ width: `${operations.releaseReadiness === null ? 0 : Math.max(0, Math.min(100, operations.releaseReadiness))}%` }} />
        </div>
        <button
          type="button"
          className="city-map-hud__action"
          onClick={() => selectBuilding("qa")}
          disabled={!hasProjectData || operations.openFindings === 0}
          aria-label="View active alerts in QA Campus"
        >
          Review alerts
        </button>
      </aside>
    </div>
  );
}
