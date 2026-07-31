import { CITY_LOCATIONS } from "./locations";
import type { LocationId } from "./ids";

export interface SceneLocationNode {
  readonly position: readonly [number, number];
  readonly targetY?: number;
}

/**
 * Renderer-owned lookup from backend semantic IDs to deterministic world
 * positions. Backend events and model output never carry or alter coordinates.
 */
export const SCENE_LOCATION_NODES: Readonly<Record<string, SceneLocationNode>> =
  Object.freeze({
    project_intake: { position: [-95, -9] },
    requirements_hall: { position: [-76, -22] },
    project_core: { position: [-17, -37], targetY: 12 },
    template_library: { position: [-82, -72] },
    tag_foundry: { position: [10, -73] },
    automation_core: { position: [-53, -44], targetY: 7 },
    ladder_logic_lab: { position: [-27, -53], targetY: 8 },
    electrical_works: { position: [85, -36], targetY: 6 },
    hmi_district: { position: [39, -57], targetY: 6 },
    alarm_center: { position: [67, 4] },
    qa_campus: { position: [46, 21] },
    testing_grounds: { position: [-32, 124] },
    approval_plaza: { position: [-80, 36] },
    documentation_quarter: { position: [-24, 56] },
    release_logistics: { position: [78, 40] },
    training_academy: { position: [-24, 94] },
    data_center: { position: [30, 119] },
    central_park: { position: [14, 65] },
    north_gateway: { position: [-191, -98] },
    south_gateway: { position: [149, -8] },
    innovation_district: { position: [-67, -145] },
    research_park: { position: [-124, -98] },
    maintenance_depot: { position: [131, 52] },
    supply_chain_hub: { position: [62, 86] },
    energy_plant: { position: [29, -165] },
    water_treatment: { position: [124, -168] },
    wind_solar_farm: { position: [-91, 122] },
    port_terminal: { position: [96, 126] },
    industrial_zone: { position: [134, -73] }
  });

export function getSceneLocationNode(id: LocationId): SceneLocationNode {
  const node = SCENE_LOCATION_NODES[id];
  if (!node) throw new RangeError(`No renderer node registered for semantic location: ${id}`);
  return node;
}

for (const location of CITY_LOCATIONS) {
  getSceneLocationNode(location.id);
}
