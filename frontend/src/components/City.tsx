import { Pause, Play, RotateCcw, ZoomIn, ZoomOut } from "lucide-react";
import { agents, buildings } from "../domain";
import { useUI } from "../store";

export function City() {
  const { selectedBuilding, selectBuilding, paused, togglePause, reducedMotion, setReducedMotion, speed, setSpeed } = useUI();
  return (
    <section className="city-panel" aria-label="Engineering city workflow visualization">
      <div className="city-toolbar">
        <div>
          <span className="eyebrow">LIVE WORKFLOW</span>
          <h2>Line 4 engineering campus</h2>
        </div>
        <div className="tool-row">
          <button className="icon-button" onClick={togglePause} aria-label={paused ? "Play timeline" : "Pause timeline"}>{paused ? <Play /> : <Pause />}</button>
          <button className="icon-button" aria-label="Zoom out"><ZoomOut /></button>
          <button className="icon-button" aria-label="Zoom in"><ZoomIn /></button>
          <button className="icon-button" aria-label="Jump to current event"><RotateCcw /></button>
        </div>
      </div>
      <div className={`city-map ${paused || reducedMotion ? "is-paused" : ""}`}>
        <div className="grid-lines" />
        <svg className="route-map" viewBox="0 0 100 84" aria-hidden="true">
          <path d="M14 23 L38 15 L65 20 L78 45 L60 67 L35 73 L12 60 L14 23" />
          <path d="M38 15 L60 67 M14 23 L78 45 M12 60 L83 77" />
        </svg>
        {buildings.map((building, index) => {
          const occupancy = agents.filter((agent) => agent.building === building.id).length;
          return (
            <button key={building.id} className={`building b${index} ${selectedBuilding === building.id ? "selected" : ""}`}
              style={{ left: `${building.x}%`, top: `${building.y}%` }} onClick={() => selectBuilding(building.id)}
              aria-label={`${building.name}, ${occupancy} agents`}>
              <span className="building-top" />
              <span className="building-face"><b>{building.short}</b><small>{occupancy ? `${occupancy} ACTIVE` : "STANDBY"}</small></span>
              {occupancy > 0 && <span className="occupancy">{occupancy}</span>}
            </button>
          );
        })}
        {agents.map((agent, index) => {
          const building = buildings.find((item) => item.id === agent.building)!;
          return (
            <button key={agent.id} className={`agent agent-${agent.state} a${index}`}
              style={{ left: `${building.x + 7 + (index % 2) * 3}%`, top: `${building.y + 9}%` }}
              title={`${agent.name}: ${agent.task}`} aria-label={`${agent.name}, ${agent.task}`}>
              <span>{agent.name.split(" ")[0].slice(0, 2).toUpperCase()}</span>
            </button>
          );
        })}
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

