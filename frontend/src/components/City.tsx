import { useCallback, useState } from "react";
import { Footprints, Map, Pause, Play, RotateCcw, ZoomIn, ZoomOut } from "lucide-react";
import { useEngineeringData } from "../api/live";
import { useUI } from "../store";
import { CityHomeChrome, type CityHomeLayer, type CityViewMode } from "./CityHomeChrome";
import { CityMapHud } from "./CityMapHud";
import { ThreeCity, type CityLabelPositions } from "./ThreeCity";

export function City() {
  const engineering = useEngineeringData();
  const [viewResetKey, setViewResetKey] = useState(0);
  const [activeLayer, setActiveLayer] = useState<CityHomeLayer>("overview");
  const [viewMode, setViewMode] = useState<CityViewMode>("overview");
  const [labelPositions, setLabelPositions] = useState<CityLabelPositions>({});
  const updateLabelPositions = useCallback((positions: CityLabelPositions) => {
    setLabelPositions(positions);
  }, []);
  const { selectedBuilding, selectBuilding, paused, togglePause, reducedMotion, setReducedMotion, speed, setSpeed, zoom, setZoom } = useUI();
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
    selectBuilding(null);
    setViewResetKey((key) => key + 1);
  };
  const changeViewMode = (nextViewMode: CityViewMode) => {
    if (nextViewMode === viewMode) return;
    setViewMode(nextViewMode);
    selectBuilding(null);
    setViewResetKey((key) => key + 1);
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
          selectBuilding={selectBuilding}
          paused={paused}
          reducedMotion={reducedMotion}
          speed={speed}
          zoom={zoom}
          resetKey={viewResetKey}
          viewMode={viewMode}
          showWorkflowAgents={engineering.dataMode === "demo"}
          onLabelPositionsChange={updateLabelPositions}
        />
        {!isWalking && (
          <CityMapHud
            selectedBuilding={selectedBuilding}
            selectBuilding={selectBuilding}
            projectLabel={`${projectReference} · ${projectName}`}
            dataMode={engineering.dataMode}
            summary={citySummary}
            labelPositions={labelPositions}
          />
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
