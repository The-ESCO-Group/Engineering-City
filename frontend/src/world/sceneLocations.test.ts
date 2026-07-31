import { describe, expect, it } from "vitest";
import { CITY_LOCATIONS } from "./locations";
import { SCENE_LOCATION_NODES, getSceneLocationNode } from "./sceneLocations";

describe("scene location registry", () => {
  it("maps every semantic destination to one finite client-owned node", () => {
    expect(Object.keys(SCENE_LOCATION_NODES)).toHaveLength(CITY_LOCATIONS.length);
    for (const location of CITY_LOCATIONS) {
      const node = getSceneLocationNode(location.id);
      expect(node.position.every(Number.isFinite)).toBe(true);
      expect(node.targetY === undefined || Number.isFinite(node.targetY)).toBe(true);
    }
  });

  it("keeps destination camera nodes spatially distinct", () => {
    const positions = CITY_LOCATIONS.map((location) =>
      getSceneLocationNode(location.id).position.join(",")
    );
    expect(new Set(positions).size).toBe(positions.length);
  });
});
