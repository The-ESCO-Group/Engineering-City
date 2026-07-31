import {
  ArrowRight,
  Building2,
  CircleAlert,
  LocateFixed,
  MapPinned,
  Search,
  X
} from "lucide-react";
import { useMemo, useState, type KeyboardEvent } from "react";
import type { BuildingId } from "../domain";
import {
  CITY_LOCATION_AVAILABILITY_LABELS,
  CITY_LOCATION_CATEGORY_LABELS,
  CITY_LOCATIONS,
  WORLD_ANCHOR_NAMES,
  filterCityLocations,
  type CityLocation,
  type CityLocationAvailability,
  type CityLocationCategory
} from "../world/locations";
import type { LocationId } from "../world/ids";
import "./CityDirectory.css";

interface CityDirectoryProps {
  selectedLocationId?: LocationId | null;
  onSelectLocation?: (location: CityLocation) => void;
  onFocusAnchor: (buildingId: BuildingId, location: CityLocation) => void;
}

const DEFAULT_LOCATION_ID = CITY_LOCATIONS.find(
  (location) => location.id === "project_core"
)?.id ?? CITY_LOCATIONS[0].id;

export function CityDirectory({
  selectedLocationId,
  onSelectLocation,
  onFocusAnchor
}: CityDirectoryProps) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<CityLocationCategory | "all">("all");
  const [availability, setAvailability] = useState<CityLocationAvailability | "all">("all");
  const [internalSelection, setInternalSelection] = useState<LocationId>(DEFAULT_LOCATION_ID);
  const activeSelection = selectedLocationId === undefined ? internalSelection : selectedLocationId;
  const selected =
    CITY_LOCATIONS.find((location) => location.id === activeSelection) ??
    CITY_LOCATIONS.find((location) => location.id === DEFAULT_LOCATION_ID) ??
    CITY_LOCATIONS[0];
  const visibleLocations = useMemo(
    () => filterCityLocations(query, category, availability),
    [availability, category, query]
  );

  function select(location: CityLocation) {
    setInternalSelection(location.id);
    onSelectLocation?.(location);
  }

  function handleListKeyDown(event: KeyboardEvent<HTMLUListElement>) {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const buttons = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>("button[data-city-location]")
    );
    if (buttons.length === 0) return;
    const currentIndex = buttons.findIndex((button) => button === document.activeElement);
    let nextIndex = currentIndex;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = buttons.length - 1;
    else if (event.key === "ArrowDown") nextIndex = Math.min(buttons.length - 1, currentIndex + 1);
    else nextIndex = Math.max(0, currentIndex < 0 ? 0 : currentIndex - 1);
    event.preventDefault();
    buttons[nextIndex]?.focus();
  }

  const filtersActive = query.length > 0 || category !== "all" || availability !== "all";

  return (
    <section className="city-directory" aria-labelledby="city-directory-title">
      <header className="city-directory__header">
        <div>
          <span className="city-directory__eyebrow">SEMANTIC WORLD INDEX</span>
          <h2 id="city-directory-title">City directory</h2>
        </div>
        <span className="city-directory__count" aria-label={`${CITY_LOCATIONS.length} destinations`}>
          {CITY_LOCATIONS.length}
        </span>
      </header>

      <div className="city-directory__filters" role="search">
        <label className="city-directory__search" htmlFor="city-directory-search">
          <Search aria-hidden="true" />
          <span className="city-directory__visually-hidden">Search city destinations</span>
          <input
            id="city-directory-search"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search destinations or purpose"
            autoComplete="off"
          />
          {query ? (
            <button type="button" onClick={() => setQuery("")} aria-label="Clear destination search">
              <X aria-hidden="true" />
            </button>
          ) : null}
        </label>
        <div className="city-directory__filter-row">
          <label>
            <span>Category</span>
            <select
              value={category}
              onChange={(event) =>
                setCategory(event.target.value as CityLocationCategory | "all")
              }
            >
              <option value="all">All categories</option>
              {Object.entries(CITY_LOCATION_CATEGORY_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Status</span>
            <select
              value={availability}
              onChange={(event) =>
                setAvailability(event.target.value as CityLocationAvailability | "all")
              }
            >
              <option value="all">All statuses</option>
              {Object.entries(CITY_LOCATION_AVAILABILITY_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <p className="city-directory__result-count" role="status" aria-live="polite">
        {visibleLocations.length} destination{visibleLocations.length === 1 ? "" : "s"} shown
      </p>

      <div className="city-directory__body">
        <div className="city-directory__list-panel">
          {visibleLocations.length > 0 ? (
            <ul
              className="city-directory__list"
              aria-label="City destinations"
              onKeyDown={handleListKeyDown}
            >
              {visibleLocations.map((location) => {
                const isSelected = location.id === selected.id;
                return (
                  <li key={location.id}>
                    <button
                      type="button"
                      data-city-location={location.id}
                      className={isSelected ? "is-selected" : ""}
                      aria-pressed={isSelected}
                      onClick={() => select(location)}
                    >
                      <span className="city-directory__list-icon" aria-hidden="true">
                        {location.availability === "backend_semantic" ? <Building2 /> : <MapPinned />}
                      </span>
                      <span className="city-directory__list-copy">
                        <strong>{location.displayName}</strong>
                        <small>{location.code}</small>
                      </span>
                      <span
                        className={`city-directory__status is-${location.availability}`}
                      >
                        {location.availability === "backend_semantic" ? "Backend" : "Mapped"}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : (
            <div className="city-directory__empty">
              <Search aria-hidden="true" />
              <strong>No matching destination</strong>
              <p>Change the search or filters to inspect another part of the city.</p>
              {filtersActive ? (
                <button
                  type="button"
                  onClick={() => {
                    setQuery("");
                    setCategory("all");
                    setAvailability("all");
                  }}
                >
                  Clear filters
                </button>
              ) : null}
            </div>
          )}
        </div>

        <article className="city-directory__detail" aria-labelledby="city-directory-detail-title">
          <div className="city-directory__detail-heading">
            <span>{selected.scope === "core" ? "Core destination" : "Expansion zone"}</span>
            <h3 id="city-directory-detail-title">{selected.displayName}</h3>
            <code>{selected.code}</code>
          </div>
          <p className="city-directory__purpose">{selected.purpose}</p>
          <dl>
            <div>
              <dt>Category</dt>
              <dd>{CITY_LOCATION_CATEGORY_LABELS[selected.category]}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{CITY_LOCATION_AVAILABILITY_LABELS[selected.availability]}</dd>
            </div>
            <div>
              <dt>Selection anchor</dt>
              <dd>{WORLD_ANCHOR_NAMES[selected.focusBuildingId]}</dd>
            </div>
          </dl>
          <div
            className={`city-directory__notice is-${selected.availability}`}
            role={selected.availability === "mapped_visual" ? "note" : undefined}
          >
            {selected.availability === "mapped_visual" ? (
              <CircleAlert aria-hidden="true" />
            ) : (
              <LocateFixed aria-hidden="true" />
            )}
            <p>
              {selected.availability === "mapped_visual"
                ? "This is a mapped city destination, not a backend-enabled workflow or agent capability."
                : selected.focusKind === "nearest"
                  ? "The backend recognizes this semantic destination. The camera uses its deterministic client-owned node while selection remains linked to the nearest stable district anchor."
                  : "This semantic destination maps directly to its current stable district anchor."}
            </p>
          </div>
          <button
            className="city-directory__focus-button"
            type="button"
            onClick={() => onFocusAnchor(selected.focusBuildingId, selected)}
          >
            <LocateFixed aria-hidden="true" />
            Focus {selected.displayName}
            <ArrowRight aria-hidden="true" />
          </button>
        </article>
      </div>
    </section>
  );
}

export default CityDirectory;
