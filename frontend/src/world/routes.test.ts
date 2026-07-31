import { describe, expect, it } from "vitest";
import { toLocationId } from "./ids";
import {
  markerStackOffset,
  planSemanticRoute,
  pointAlongRoute,
  routeLength
} from "./routes";
import { getSceneLocationNode } from "./sceneLocations";

const location = (value: string) => {
  const id = toLocationId(value);
  if (!id) throw new Error(`Invalid test location: ${value}`);
  return id;
};

describe("semantic city routes", () => {
  it("is deterministic and starts and ends at client-owned location nodes", () => {
    const from = location("project_intake");
    const to = location("qa_campus");
    const first = planSemanticRoute(from, to, "agent-7");
    const second = planSemanticRoute(from, to, "agent-7");

    expect(first).toEqual(second);
    expect(first[0]).toEqual(getSceneLocationNode(from).position);
    expect(first.at(-1)).toEqual(getSceneLocationNode(to).position);
    expect(routeLength(first)).toBeGreaterThan(0);
  });

  it("interpolates along the full polyline and clamps progress", () => {
    const route = planSemanticRoute(
      location("electrical_works"),
      location("approval_plaza"),
      "electrical-agent"
    );
    expect(pointAlongRoute(route, -1)).toEqual(route[0]);
    expect(pointAlongRoute(route, 1.4)).toEqual(route.at(-1));
    expect(pointAlongRoute(route, 0.5)).not.toEqual(route[0]);
  });

  it("assigns stable non-overlapping offsets to co-located markers", () => {
    const offsets = Array.from({ length: 12 }, (_, index) => markerStackOffset(index, 12));
    expect(new Set(offsets.map((point) => point.join(":"))).size).toBe(12);
    expect(markerStackOffset(0, 1)).toEqual([0, 0]);
    expect(markerStackOffset(2, 4)).toEqual(markerStackOffset(2, 4));
  });
});
