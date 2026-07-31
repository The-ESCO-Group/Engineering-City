import type { LocationId } from "./ids";
import { getSceneLocationNode } from "./sceneLocations";

export type WorldPoint = readonly [number, number];

const ROUTE_CORRIDORS = [-146, -110, -76, -42, -8, 28, 64, 100, 128] as const;

function stableHash(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function nearestCorridor(value: number) {
  return ROUTE_CORRIDORS.reduce((nearest, candidate) =>
    Math.abs(candidate - value) < Math.abs(nearest - value) ? candidate : nearest
  );
}

function withoutRepeatedPoints(points: readonly WorldPoint[]) {
  return points.filter((point, index) => {
    const previous = points[index - 1];
    return !previous || previous[0] !== point[0] || previous[1] !== point[1];
  });
}

/**
 * Resolves two semantic locations onto a deterministic client-owned route.
 * The backend never publishes these coordinates and moving the city remains a
 * renderer-only change.
 */
export function planSemanticRoute(
  fromLocationId: LocationId,
  toLocationId: LocationId,
  routeKey = ""
): readonly WorldPoint[] {
  const from = getSceneLocationNode(fromLocationId).position;
  const to = getSceneLocationNode(toLocationId).position;
  if (fromLocationId === toLocationId) return [from];

  const hash = stableHash(`${fromLocationId}:${toLocationId}:${routeKey}`);
  const lane = ((hash % 5) - 2) * 0.72;
  const xFirst = (hash & 1) === 0;
  const center = (from[1] + to[1]) / 2;
  const corridorZ = nearestCorridor(center) + lane;
  const fromLaneX = from[0] + lane;
  const toLaneX = to[0] + lane;

  const points: readonly WorldPoint[] = xFirst
    ? [from, [fromLaneX, corridorZ], [toLaneX, corridorZ], to]
    : [from, [from[0], corridorZ], [to[0], corridorZ], to];
  return withoutRepeatedPoints(points);
}

/** Gives co-located workflow markers a stable, compact radial offset. */
export function markerStackOffset(index: number, count: number): WorldPoint {
  if (count <= 1) return [0, 0];
  const safeIndex = Math.max(0, Math.min(count - 1, Math.floor(index)));
  const ring = Math.floor(safeIndex / 8);
  const indexOnRing = safeIndex % 8;
  const itemsOnRing = Math.min(8, count - ring * 8);
  const angle = (indexOnRing / Math.max(1, itemsOnRing)) * Math.PI * 2 - Math.PI / 2;
  const radius = 1.3 + ring * 0.92;
  return [Math.cos(angle) * radius, Math.sin(angle) * radius];
}

export function routeLength(points: readonly WorldPoint[]) {
  let total = 0;
  for (let index = 1; index < points.length; index += 1) {
    total += Math.hypot(
      points[index][0] - points[index - 1][0],
      points[index][1] - points[index - 1][1]
    );
  }
  return total;
}

export function pointAlongRoute(points: readonly WorldPoint[], progress: number): WorldPoint {
  if (points.length === 0) return [0, 0];
  if (points.length === 1) return points[0];
  const total = routeLength(points);
  if (total <= 0) return points.at(-1)!;
  let remaining = Math.max(0, Math.min(1, progress)) * total;
  for (let index = 1; index < points.length; index += 1) {
    const from = points[index - 1];
    const to = points[index];
    const segment = Math.hypot(to[0] - from[0], to[1] - from[1]);
    if (remaining <= segment || index === points.length - 1) {
      const ratio = segment <= 0 ? 1 : Math.min(1, remaining / segment);
      return [
        from[0] + (to[0] - from[0]) * ratio,
        from[1] + (to[1] - from[1]) * ratio
      ];
    }
    remaining -= segment;
  }
  return points.at(-1)!;
}
