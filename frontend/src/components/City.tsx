import { Pause, Play, RotateCcw, ZoomIn, ZoomOut } from "lucide-react";
import { useUI } from "../store";
import { PixiCity } from "./PixiCity";

export function City() {
  const { selectedBuilding, selectBuilding, paused, togglePause, reducedMotion, setReducedMotion, speed, setSpeed, zoom, setZoom } = useUI();
  return (
    <section className="city-panel" aria-label="Engineering city workflow visualization">
      <div className="city-toolbar">
        <div>
          <span className="eyebrow">LIVE WORKFLOW</span>
          <h2>Line 4 engineering campus</h2>
        </div>
        <div className="tool-row">
          <button className="icon-button" onClick={togglePause} aria-label={paused ? "Play timeline" : "Pause timeline"}>{paused ? <Play /> : <Pause />}</button>
          <button className="icon-button" onClick={() => setZoom(Math.max(0.82, zoom - 0.1))} disabled={zoom <= 0.82} aria-label="Zoom out"><ZoomOut /></button>
          <button className="icon-button" onClick={() => setZoom(Math.min(1.28, zoom + 0.1))} disabled={zoom >= 1.28} aria-label="Zoom in"><ZoomIn /></button>
          <button className="icon-button" onClick={() => { setZoom(1); selectBuilding(null); }} aria-label="Reset city view"><RotateCcw /></button>
        </div>
      </div>
      <div className={`city-map ${paused || reducedMotion ? "is-paused" : ""}`}>
        <PixiCity
          selectedBuilding={selectedBuilding}
          selectBuilding={selectBuilding}
          paused={paused}
          reducedMotion={reducedMotion}
          speed={speed}
          zoom={zoom}
        />
        <div className="map-legend"><span><i className="dot active" /> Active</span><span><i className="dot waiting" /> Waiting</span><span><i className="dot fault" /> Warning</span></div>
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
