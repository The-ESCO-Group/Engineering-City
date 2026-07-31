import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Footprints,
  Gauge,
  ListTree,
  Map,
  Pause,
  Play,
  RotateCcw,
  X,
  ZoomIn,
  ZoomOut
} from "lucide-react";
import { useEngineeringData } from "../api/live";
import { useUI } from "../store";
import type { CityLocation } from "../world/locations";
import {
  selectWorldQuality,
  type WorldHardwareHints,
  type WorldQualityMode
} from "../world/quality";
import { CityDirectory } from "./CityDirectory";
import { CityHomeChrome, type CityHomeLayer, type CityViewMode } from "./CityHomeChrome";
import { CityLayerLegend } from "./CityLayerLegend";
import { CityMapHud } from "./CityMapHud";
import { ThreeCity, type CityLabelPositions } from "./ThreeCity";
import { WorldQualityControl } from "./WorldQualityControl";

const WORLD_QUALITY_STORAGE_KEY = "esco.world-quality";

function initialWorldQualityMode(): WorldQualityMode {
  if (typeof window === "undefined") return "auto";
  let stored: string | null = null;
  try {
    stored = window.localStorage.getItem(WORLD_QUALITY_STORAGE_KEY);
  } catch {
    return "auto";
  }
  return stored === "high" || stored === "balanced" || stored === "performance"
    ? stored
    : "auto";
}

function detectWorldHardware(): WorldHardwareHints {
  if (typeof navigator === "undefined" || typeof window === "undefined") return {};
  const hardwareNavigator = navigator as Navigator & { deviceMemory?: number };
  const logicalProcessors = navigator.hardwareConcurrency || null;
  const deviceMemoryGb = hardwareNavigator.deviceMemory ?? null;
  const mobile = window.matchMedia?.("(pointer: coarse)").matches ?? false;
  const constrained = mobile ||
    (logicalProcessors !== null && logicalProcessors < 4) ||
    (deviceMemoryGb !== null && deviceMemoryGb < 4);
  const capable = !mobile &&
    (logicalProcessors === null || logicalProcessors >= 8) &&
    (deviceMemoryGb === null || deviceMemoryGb >= 8);

  return {
    logicalProcessors,
    deviceMemoryGb,
    mobile,
    gpuTier: constrained ? "low" : capable ? "high" : "medium",
    powerPreference: constrained ? "low-power" : "high-performance"
  };
}

export function City() {
  const engineering = useEngineeringData();
  const [viewResetKey, setViewResetKey] = useState(0);
  const [activeLayer, setActiveLayer] = useState<CityHomeLayer>("overview");
  const [viewMode, setViewMode] = useState<CityViewMode>("overview");
  const [labelPositions, setLabelPositions] = useState<CityLabelPositions>({});
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const [qualityOpen, setQualityOpen] = useState(false);
  const [qualityMode, setQualityMode] = useState<WorldQualityMode>(initialWorldQualityMode);
  const [focusedLocation, setFocusedLocation] = useState<CityLocation | null>(null);
  const [focusRequestKey, setFocusRequestKey] = useState(0);
  const [selectionRequestKey, setSelectionRequestKey] = useState(0);
  const directoryTriggerRef = useRef<HTMLButtonElement>(null);
  const directoryDialogRef = useRef<HTMLDivElement>(null);
  const qualityTriggerRef = useRef<HTMLButtonElement>(null);
  const updateLabelPositions = useCallback((positions: CityLabelPositions) => {
    setLabelPositions(positions);
  }, []);
  const { selectedBuilding, selectBuilding, paused, togglePause, reducedMotion, setReducedMotion, speed, setSpeed, zoom, setZoom } = useUI();
  const hardwareHints = useMemo(detectWorldHardware, []);
  const qualitySelection = useMemo(
    () => selectWorldQuality(qualityMode, hardwareHints, reducedMotion),
    [hardwareHints, qualityMode, reducedMotion]
  );
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const syncPreference = (event: MediaQueryListEvent) => setReducedMotion(event.matches);
    media.addEventListener("change", syncPreference);
    return () => media.removeEventListener("change", syncPreference);
  }, [setReducedMotion]);
  const isWalking = viewMode === "first-person";
  const liveActivityAgents = engineering.dataMode === "unavailable"
    ? []
    : engineering.agents.map((agent) => ({
        id: agent.id,
        name: agent.display_name,
        state: agent.status === "working" || agent.status === "walking"
          ? "active" as const
          : agent.status === "waiting" || agent.status === "blocked"
            ? "waiting" as const
            : "complete" as const,
        task: agent.current_activity || agent.location || "Ready"
      }));
  const activeReviewCount = liveActivityAgents?.filter((agent) => agent.state === "active").length ?? 3;
  const citySummary = {
    activeReviews: activeReviewCount,
    openFindings: engineering.dataMode === "demo"
      ? engineering.project?.counts.findings_open || 0
      : engineering.findings.filter((finding) => finding.status === "open").length,
    pendingApprovals: engineering.dataMode === "demo"
      ? engineering.project?.counts.approvals_pending || 0
      : engineering.approvals.filter((approval) => approval.status === "pending").length,
    releaseReadiness: engineering.dataMode === "demo"
      ? 68
      : engineering.project?.status === "completed" ? 100 : null
  };
  const projectReference = engineering.project?.client_reference || "NO PROJECT";
  const projectName = engineering.project?.name || "Local project data unavailable";
  const resetCityView = () => {
    setZoom(1);
    setFocusedLocation(null);
    selectBuilding(null);
    setViewResetKey((key) => key + 1);
  };
  const changeViewMode = (nextViewMode: CityViewMode) => {
    if (nextViewMode === viewMode) return;
    setViewMode(nextViewMode);
    setDirectoryOpen(false);
    setQualityOpen(false);
    setFocusedLocation(null);
    selectBuilding(null);
    setViewResetKey((key) => key + 1);
  };
  const changeQualityMode = (nextMode: WorldQualityMode) => {
    setQualityMode(nextMode);
    try {
      window.localStorage.setItem(WORLD_QUALITY_STORAGE_KEY, nextMode);
    } catch {
      // The preference remains effective for this session when storage is unavailable.
    }
  };
  const selectWorldBuilding = (buildingId: Parameters<typeof selectBuilding>[0]) => {
    setFocusedLocation(null);
    setSelectionRequestKey((key) => key + 1);
    selectBuilding(buildingId);
  };
  const closeDirectory = (restoreFocus = true) => {
    setDirectoryOpen(false);
    if (restoreFocus) window.requestAnimationFrame(() => directoryTriggerRef.current?.focus());
  };
  const closeQuality = () => {
    setQualityOpen(false);
    window.requestAnimationFrame(() => qualityTriggerRef.current?.focus());
  };
  const focusSemanticLocation = (
    buildingId: Parameters<typeof selectBuilding>[0],
    location: CityLocation
  ) => {
    setFocusedLocation(location);
    setFocusRequestKey((key) => key + 1);
    selectBuilding(buildingId);
    closeDirectory(false);
  };
  return (
    <section
      className={`city-panel${isWalking ? " is-first-person" : ""}`}
      aria-label="Engineering city workflow visualization"
    >
      <div className="city-toolbar">
        <div className="city-command-identity">
          <span className="eyebrow">{projectReference} · ENGINEERING WORLD</span>
          <h2>{projectName}</h2>
        </div>
        {isWalking ? (
          <button className="city-walk-exit" type="button" onClick={() => changeViewMode("overview")}>
            <Map aria-hidden="true" />
            <span>Exit walk</span>
            <small>Map view</small>
          </button>
        ) : (
          <div className="city-view-switch" role="group" aria-label="City view mode">
            <button
              type="button"
              className="is-active"
              aria-pressed="true"
              title="Flat-world map: drag to orbit 360°, scroll or pinch to zoom"
              onClick={() => changeViewMode("overview")}
            >
              <Map aria-hidden="true" />
              <span>Map</span>
            </button>
            <button
              type="button"
              aria-pressed="false"
              onClick={() => changeViewMode("first-person")}
            >
              <Footprints aria-hidden="true" />
              <span>Walk</span>
            </button>
          </div>
        )}
        <div className="city-command-demo" aria-label="Synthetic demonstration status">
          <span>{engineering.dataMode === "live" ? "Local" : engineering.dataMode === "demo" ? "Demo" : "Offline"}</span>
          <i aria-hidden="true" />
          <b>{paused ? "Workflow paused" : `${activeReviewCount} reviews active`}</b>
        </div>
        <div className="tool-row">
          {!isWalking && (
            <button
              ref={directoryTriggerRef}
              className="icon-button city-directory-trigger"
              type="button"
              onClick={() => {
                setDirectoryOpen((open) => !open);
                setQualityOpen(false);
              }}
              aria-label="Open city directory"
              aria-controls="city-directory-dialog"
              aria-expanded={directoryOpen}
              title="Search semantic city destinations"
            >
              <ListTree />
            </button>
          )}
          {!isWalking && (
            <button
              ref={qualityTriggerRef}
              className="icon-button city-quality-trigger"
              type="button"
              onClick={() => {
                setQualityOpen((open) => !open);
                setDirectoryOpen(false);
              }}
              aria-label="World quality settings"
              aria-controls="world-quality-panel"
              aria-expanded={qualityOpen}
              title="World rendering quality"
            >
              <Gauge />
            </button>
          )}
          <button className="icon-button" onClick={togglePause} aria-label={paused ? "Play timeline" : "Pause timeline"}>{paused ? <Play /> : <Pause />}</button>
          <button
            className="icon-button"
            onClick={() => setZoom(Math.max(0.82, zoom - 0.1))}
            disabled={isWalking || zoom <= 0.82}
            aria-label="Zoom map out"
            title={isWalking ? "Zoom is available in Map view" : "Zoom map out"}
          >
            <ZoomOut />
          </button>
          <button
            className="icon-button"
            onClick={() => setZoom(Math.min(1.28, zoom + 0.1))}
            disabled={isWalking || zoom >= 1.28}
            aria-label="Zoom map in"
            title={isWalking ? "Zoom is available in Map view" : "Zoom map in"}
          >
            <ZoomIn />
          </button>
          <button className="icon-button" onClick={resetCityView} aria-label={isWalking ? "Reset walk position" : "Reset city view"}><RotateCcw /></button>
        </div>
      </div>
      <div className={`city-map ${paused || reducedMotion ? "is-paused" : ""}`}>
        <ThreeCity
          selectedBuilding={selectedBuilding}
          selectBuilding={selectWorldBuilding}
          paused={paused}
          reducedMotion={reducedMotion}
          speed={speed}
          zoom={zoom}
          resetKey={viewResetKey}
          viewMode={viewMode}
          activeLayer={activeLayer}
          qualityProfile={qualitySelection.profile}
          selectionRequestKey={selectionRequestKey}
          focusLocationId={focusedLocation?.id ?? null}
          focusRequestKey={focusRequestKey}
          showWorkflowAgents={engineering.dataMode === "demo"}
          onLabelPositionsChange={updateLabelPositions}
        />
        {!isWalking && <CityLayerLegend activeLayer={activeLayer} />}
        {!isWalking && focusedLocation && (
          <div className="city-focused-location" role="status">
            <span>Focused destination</span>
            <strong>{focusedLocation.displayName}</strong>
            <small>{focusedLocation.code}</small>
          </div>
        )}
        {!isWalking && (
          <CityMapHud
            selectedBuilding={selectedBuilding}
            selectBuilding={selectWorldBuilding}
            projectLabel={`${projectReference} · ${projectName}`}
            dataMode={engineering.dataMode}
            summary={citySummary}
            labelDensity={qualitySelection.profile.labelDensity}
            labelPositions={labelPositions}
          />
        )}
        {!isWalking && qualityOpen && (
          <div
            id="world-quality-panel"
            className="world-quality-panel"
            onKeyDown={(event) => {
              if (event.key === "Escape") closeQuality();
            }}
          >
            <WorldQualityControl
              requested={qualitySelection.requested}
              resolved={qualitySelection.resolved}
              profile={qualitySelection.profile}
              onChange={changeQualityMode}
              autoFocus
              className="is-city-hud"
            />
          </div>
        )}
        {!isWalking && directoryOpen && (
          <div
            className="city-directory-overlay"
            role="dialog"
            aria-modal="true"
            aria-label="City directory"
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                closeDirectory();
                return;
              }
              if (event.key !== "Tab") return;
              const focusable = Array.from(
                directoryDialogRef.current?.querySelectorAll<HTMLElement>(
                  "button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])"
                ) ?? []
              );
              const first = focusable[0];
              const last = focusable.at(-1);
              if (!first || !last) return;
              if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
              } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
              }
            }}
          >
            <div
              ref={directoryDialogRef}
              className="city-directory-dialog"
              id="city-directory-dialog"
            >
              <button
                className="city-directory-dialog__close"
                type="button"
                onClick={() => closeDirectory()}
                aria-label="Close city directory"
                autoFocus
              >
                <X />
              </button>
              <CityDirectory
                onFocusAnchor={focusSemanticLocation}
              />
            </div>
          </div>
        )}
        <CityHomeChrome
          activeLayer={activeLayer}
          onLayerChange={setActiveLayer}
          onZoomIn={() => setZoom(Math.min(1.28, zoom + 0.1))}
          onZoomOut={() => setZoom(Math.max(0.82, zoom - 0.1))}
          onLocate={resetCityView}
          viewMode={viewMode}
          paused={paused}
          dataMode={engineering.dataMode}
          activityAgents={liveActivityAgents}
        />
      </div>
      <div className="timeline">
        <button className="play-label" onClick={togglePause}>{paused ? <Play /> : <Pause />} {paused ? "Resume" : "Live"}</button>
        <div className="timeline-track"><span style={{ width: "68%" }} /><i style={{ left: "68%" }} /></div>
        <span>Event 42 / 61</span>
        <label>Speed <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}><option value={0.5}>0.5×</option><option value={1}>1×</option><option value={2}>2×</option></select></label>
        <label className="check"><input type="checkbox" checked={reducedMotion} onChange={(e) => setReducedMotion(e.target.checked)} /> Reduced motion</label>
      </div>
    </section>
  );
}
