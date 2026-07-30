import {
  Building2,
  CarFront,
  Footprints,
  LayoutGrid,
  Leaf,
  LocateFixed,
  Map,
  Minus,
  Plus,
  ShieldCheck,
  Zap,
  type LucideIcon
} from "lucide-react";
import "./CityHomeChrome.css";

const LAYERS = [
  { id: "overview", label: "Overview", icon: LayoutGrid },
  { id: "infrastructure", label: "Infrastructure", icon: Building2 },
  { id: "utilities", label: "Utilities", icon: Zap },
  { id: "environment", label: "Environment", icon: Leaf },
  { id: "security", label: "Security", icon: ShieldCheck },
  { id: "mobility", label: "Mobility", icon: CarFront }
] as const satisfies ReadonlyArray<{
  id: string;
  label: string;
  icon: LucideIcon;
}>;

export type CityHomeLayer = (typeof LAYERS)[number]["id"];
export type CityViewMode = "overview" | "first-person";

export interface CityHomeChromeProps {
  activeLayer?: CityHomeLayer;
  className?: string;
  onLayerChange?: (layer: CityHomeLayer) => void;
  onLocate?: () => void;
  onZoomIn?: () => void;
  onZoomOut?: () => void;
  viewMode?: CityViewMode;
  onViewModeChange?: (viewMode: CityViewMode) => void;
}

interface MapControlProps {
  label: string;
  onClick?: () => void;
  icon: LucideIcon;
}

function MapControl({ label, onClick, icon: Icon }: MapControlProps) {
  return (
    <button
      className="city-home-chrome__map-control"
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
    >
      <Icon aria-hidden="true" strokeWidth={1.8} />
    </button>
  );
}

function CityMinimap({
  onLocate,
  onZoomIn,
  onZoomOut
}: Pick<CityHomeChromeProps, "onLocate" | "onZoomIn" | "onZoomOut">) {
  return (
    <aside className="city-home-chrome__minimap" aria-label="Engineering city minimap">
      <div className="city-home-chrome__minimap-panel">
        <div className="city-home-chrome__minimap-header" aria-hidden="true">
          <span>Campus vector</span>
          <span className="city-home-chrome__live"><i /> Live</span>
        </div>
        <svg
          className="city-home-chrome__minimap-svg"
          viewBox="0 0 220 136"
          aria-hidden="true"
          focusable="false"
        >
          <path className="city-home-chrome__map-shore" d="M-8 116C30 98 45 89 72 88c33-2 48 14 78 8 30-5 41-28 78-37" />
          <path className="city-home-chrome__map-shore" d="M-5 127c42-18 58-25 84-24 34 2 48 13 78 6 27-6 43-25 69-34" />

          <path className="city-home-chrome__street city-home-chrome__street--minor" d="M7 30l34 18 26-3 30 18 35-7 22 15 56-7" />
          <path className="city-home-chrome__street city-home-chrome__street--minor" d="M12 76l31-12 32 12 31-8 35 23 29-8 44 21" />
          <path className="city-home-chrome__street city-home-chrome__street--minor" d="M28 16l12 32 3 40-14 38M67 10v35l8 31-3 48M106 7l-9 56 9 38-2 31M145 6l9 65-13 20 7 40M184 12l-9 42-5 29 21 42" />
          <path className="city-home-chrome__street city-home-chrome__street--minor" d="M5 54l34-6 28-3 39-18 39 17 30 10 39-11M8 101l35-13 29 0 34 13 42 8 39-8 29 11" />

          <path className="city-home-chrome__street city-home-chrome__street--major" d="M-8 91C24 78 42 71 70 73c28 2 43 19 68 18 29-1 42-20 88-25" />
          <path className="city-home-chrome__street city-home-chrome__street--major" d="M18 136c11-28 24-41 48-50 22-8 43-10 59-30 13-17 20-31 31-57" />
          <path className="city-home-chrome__street city-home-chrome__street--major" d="M-4 38c31 6 52 13 70 28 21 18 30 29 58 35 31 7 59 4 101 21" />

          <path className="city-home-chrome__route" d="M18 114c19-22 36-29 58-31 25-2 34 15 56 12 22-2 34-17 62-33" />

          <g className="city-home-chrome__districts">
            <circle cx="19" cy="37" r="2.2" />
            <circle cx="67" cy="45" r="2.2" />
            <circle cx="106" cy="27" r="2.2" />
            <circle cx="145" cy="44" r="2.2" />
            <circle cx="176" cy="54" r="2.2" />
            <circle cx="43" cy="88" r="2.2" />
            <circle cx="104" cy="101" r="2.2" />
            <circle cx="188" cy="101" r="2.2" />
          </g>

          <g className="city-home-chrome__location" transform="translate(125 94)">
            <circle className="city-home-chrome__location-pulse" r="10" />
            <circle className="city-home-chrome__location-ring" r="5.5" />
            <circle className="city-home-chrome__location-core" r="2.3" />
          </g>
        </svg>
        <span className="city-home-chrome__sr-only">
          Stylized overview of the engineering campus road network and current location.
        </span>
      </div>

      <div className="city-home-chrome__map-controls" role="group" aria-label="Minimap controls">
        <MapControl label="Zoom city map in" icon={Plus} onClick={onZoomIn} />
        <MapControl label="Zoom city map out" icon={Minus} onClick={onZoomOut} />
        <MapControl label="Locate current city view" icon={LocateFixed} onClick={onLocate} />
      </div>
    </aside>
  );
}

export function CityHomeChrome({
  activeLayer = "overview",
  className,
  onLayerChange,
  onLocate,
  onZoomIn,
  onZoomOut,
  viewMode = "overview",
  onViewModeChange
}: CityHomeChromeProps) {
  const isWalking = viewMode === "first-person";
  const rootClassName = ["city-home-chrome", isWalking && "is-walking", className].filter(Boolean).join(" ");

  return (
    <div className={rootClassName}>
      <div className="city-home-chrome__view-switch" role="group" aria-label="City view mode">
        <button
          type="button"
          className={viewMode === "overview" ? "is-active" : ""}
          aria-pressed={viewMode === "overview"}
          onClick={() => onViewModeChange?.("overview")}
        >
          <Map aria-hidden="true" strokeWidth={1.8} />
          <span>Map view</span>
        </button>
        <button
          type="button"
          className={isWalking ? "is-active" : ""}
          aria-pressed={isWalking}
          onClick={() => onViewModeChange?.("first-person")}
        >
          <Footprints aria-hidden="true" strokeWidth={1.8} />
          <span>Walk city</span>
        </button>
      </div>

      {isWalking && (
        <aside className="city-home-chrome__walk-command" aria-label="Walk mode controls">
          <button type="button" onClick={() => onViewModeChange?.("overview")}>
            <Map aria-hidden="true" strokeWidth={1.8} />
            <span>Exit walk</span>
            <small>Map view</small>
          </button>
        </aside>
      )}

      <CityMinimap onLocate={onLocate} onZoomIn={onZoomIn} onZoomOut={onZoomOut} />

      <nav className="city-home-chrome__layer-dock" aria-label="Engineering city map layers">
        {LAYERS.map(({ id, label, icon: Icon }) => {
          const selected = id === activeLayer;
          return (
            <button
              className={`city-home-chrome__layer${selected ? " is-active" : ""}`}
              type="button"
              key={id}
              aria-label={`${label} layer`}
              aria-pressed={selected}
              title={label}
              onClick={() => onLayerChange?.(id)}
            >
              <Icon aria-hidden="true" strokeWidth={1.65} />
              <span>{label}</span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}
