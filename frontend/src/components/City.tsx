import { useState } from "react";
import { Pause, Play, RotateCcw, ZoomIn, ZoomOut } from "lucide-react";
import { useUI } from "../store";
import { CityHomeChrome, type CityHomeLayer, type CityViewMode } from "./CityHomeChrome";
import { CityMapHud } from "./CityMapHud";
import { ThreeCity } from "./ThreeCity";

export function City() {
  const [viewResetKey, setViewResetKey] = useState(0);
  const [activeLayer, setActiveLayer] = useState<CityHomeLayer>("overview");
  const [viewMode, setViewMode] = useState<CityViewMode>("overview");
  const { selectedBuilding, selectBuilding, paused, togglePause, reducedMotion, setReducedMotion, speed, setSpeed, zoom, setZoom } = useUI();
  const isWalking = viewMode === "first-person";
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
        <div>
          <span className="eyebrow">{isWalking ? "WALK MODE · DIGITAL CITY" : "ENGINEERING OPERATIONS · DIGITAL CITY"}</span>
          <h2>Line 4 operations city</h2>
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
        />
        {!isWalking && (
          <CityMapHud
            selectedBuilding={selectedBuilding}
            selectBuilding={selectBuilding}
            projectLabel="PRJ-0241 · Line 4 Cartoner Upgrade"
          />
        )}
        <CityHomeChrome
          activeLayer={activeLayer}
          onLayerChange={setActiveLayer}
          onZoomIn={() => setZoom(Math.min(1.28, zoom + 0.1))}
          onZoomOut={() => setZoom(Math.max(0.82, zoom - 0.1))}
          onLocate={resetCityView}
          viewMode={viewMode}
          onViewModeChange={changeViewMode}
        />
        {!isWalking && <div className="map-legend"><span><i className="dot active" /> Active</span><span><i className="dot waiting" /> Waiting</span><span><i className="dot fault" /> Warning</span></div>}
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
