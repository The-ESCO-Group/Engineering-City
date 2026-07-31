import { describe, expect, it } from "vitest";
import { buildings } from "../domain";
import {
  BACKEND_SEMANTIC_LOCATION_IDS,
  CITY_LOCATIONS,
  filterCityLocations,
  getCityLocation
} from "./locations";

describe("city semantic directory", () => {
  it("contains all 18 core destinations plus the expansion zones", () => {
    expect(CITY_LOCATIONS.filter((location) => location.scope === "core")).toHaveLength(18);
    expect(CITY_LOCATIONS.filter((location) => location.scope === "expansion")).toHaveLength(11);
    expect(new Set(CITY_LOCATIONS.map((location) => location.id)).size).toBe(
      CITY_LOCATIONS.length
    );
  });

  it("uses backend-compatible IDs and stable uppercase codes", () => {
    for (const location of CITY_LOCATIONS) {
      expect(location.id).toMatch(/^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/);
      expect(location.code).toBe(location.code.toUpperCase());
      expect(location.code).toMatch(/^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$/);
    }
    expect(getCityLocation("QA_CAMPUS")?.id).toBe("qa_campus");
  });

  it("maps every destination to an existing stable world anchor", () => {
    const anchors = new Set(buildings.map((building) => building.id));
    expect(CITY_LOCATIONS.every((location) => anchors.has(location.focusBuildingId))).toBe(true);
  });

  it("searches names, purposes, semantic IDs, categories, and anchor labels", () => {
    expect(filterCityLocations("I/O schedule").map((item) => item.id)).toContain(
      "electrical_works"
    );
    expect(filterCityLocations("wind solar").map((item) => item.id)).toContain(
      "wind_solar_farm"
    );
    expect(filterCityLocations("QA_CAMPUS").map((item) => item.id)).toEqual(["qa_campus"]);
    expect(filterCityLocations("Automation Engineering").length).toBeGreaterThan(1);
  });

  it("keeps visual-only destinations distinguishable from backend semantic locations", () => {
    const mappedCore = filterCityLocations("", "all", "mapped_visual").filter(
      (location) => location.scope === "core"
    );
    expect(mappedCore.map((location) => location.id)).toEqual([
      "training_academy",
      "central_park"
    ]);
    expect(
      CITY_LOCATIONS.filter((location) => location.scope === "expansion").every(
        (location) => location.availability === "mapped_visual"
      )
    ).toBe(true);
  });

  it("matches the generated backend semantic-location contract exactly", () => {
    const registered = CITY_LOCATIONS
      .filter((location) => location.availability === "backend_semantic")
      .map((location) => location.id)
      .sort();
    expect(registered).toEqual([...BACKEND_SEMANTIC_LOCATION_IDS].sort());
  });
});
