import type { CityHomeLayer } from "./CityHomeChrome";
import "./CityLayerLegend.css";

interface CityLayerLegendProps {
  activeLayer: CityHomeLayer;
}

const LAYER_LEGENDS: Record<
  CityHomeLayer,
  { label: string; description: string; signals: readonly [string, string] }
> = {
  overview: {
    label: "Overview",
    description: "All operational systems",
    signals: ["Live city activity", "Ambient network state"]
  },
  infrastructure: {
    label: "Infrastructure",
    description: "Physical engineering assets",
    signals: ["Anchor footprints", "Structural beacons"]
  },
  utilities: {
    label: "Utilities",
    description: "Power, control, and water",
    signals: ["Distribution links", "Utility node flow"]
  },
  environment: {
    label: "Environment",
    description: "Water and green systems",
    signals: ["Ecology zones", "Water corridors"]
  },
  security: {
    label: "Security",
    description: "Review and release controls",
    signals: ["Controlled perimeter", "Priority checkpoints"]
  },
  mobility: {
    label: "Mobility",
    description: "Movement across the city",
    signals: ["Route corridors", "Vehicle and foot flow"]
  }
};

export function CityLayerLegend({ activeLayer }: CityLayerLegendProps) {
  const legend = LAYER_LEGENDS[activeLayer];

  return (
    <aside
      className={`city-layer-legend is-${activeLayer}`}
      aria-label={`${legend.label} map layer legend`}
      aria-live="polite"
    >
      <header>
        <span>Active layer</span>
        <strong>{legend.label}</strong>
      </header>
      <p>{legend.description}</p>
      <ul>
        {legend.signals.map((signal, index) => (
          <li key={signal}>
            <i className={index === 0 ? "is-primary" : "is-secondary"} aria-hidden="true" />
            {signal}
          </li>
        ))}
      </ul>
    </aside>
  );
}
