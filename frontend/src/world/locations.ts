import type { BuildingId } from "../domain";
import type { SemanticLocation } from "../generated";
import { toLocationId, type LocationId } from "./ids";

export type CityLocationScope = "core" | "expansion";

export type CityLocationCategory =
  | "intake_planning"
  | "engineering"
  | "review_assurance"
  | "delivery_release"
  | "campus_services"
  | "future_capacity";

export type CityLocationAvailability = "backend_semantic" | "mapped_visual";
export type CityLocationFocusKind = "direct" | "nearest";

export interface CityLocation {
  readonly id: LocationId;
  readonly code: Uppercase<string>;
  readonly displayName: string;
  readonly purpose: string;
  readonly category: CityLocationCategory;
  readonly scope: CityLocationScope;
  readonly availability: CityLocationAvailability;
  readonly focusBuildingId: BuildingId;
  readonly focusKind: CityLocationFocusKind;
}

export const CITY_LOCATION_CATEGORY_LABELS: Readonly<Record<CityLocationCategory, string>> = {
  intake_planning: "Intake & planning",
  engineering: "Engineering production",
  review_assurance: "Review & assurance",
  delivery_release: "Delivery & release",
  campus_services: "Campus services",
  future_capacity: "Future capacity"
};

export const CITY_LOCATION_AVAILABILITY_LABELS: Readonly<
  Record<CityLocationAvailability, string>
> = {
  backend_semantic: "Backend semantic",
  mapped_visual: "Mapped visual only"
};

export const BACKEND_SEMANTIC_LOCATION_IDS = [
  "project_intake",
  "requirements_hall",
  "project_core",
  "tag_foundry",
  "automation_core",
  "ladder_logic_lab",
  "electrical_works",
  "hmi_district",
  "alarm_center",
  "qa_campus",
  "testing_grounds",
  "approval_plaza",
  "documentation_quarter",
  "release_logistics",
  "template_library",
  "data_center"
] as const satisfies readonly SemanticLocation[];

type MissingSemanticLocation = Exclude<
  SemanticLocation,
  (typeof BACKEND_SEMANTIC_LOCATION_IDS)[number]
>;
const allSemanticLocationsMapped: MissingSemanticLocation extends never ? true : false = true;
void allSemanticLocationsMapped;

export const WORLD_ANCHOR_NAMES: Readonly<Record<BuildingId, string>> = {
  intake: "Project Intake",
  automation: "Automation Core",
  hmi: "HMI District",
  electrical: "Electrical Works",
  qa: "QA Campus",
  documentation: "Documentation Quarter",
  approval: "Approval Plaza",
  release: "Release Logistics"
};

interface LocationDefinition extends Omit<CityLocation, "id" | "code"> {
  readonly id: string;
  readonly code?: Uppercase<string>;
}

function location(definition: LocationDefinition): CityLocation {
  const id = toLocationId(definition.id);
  if (id === null) throw new TypeError(`Invalid semantic city location: ${definition.id}`);
  const code = (definition.code ?? definition.id.toUpperCase()) as Uppercase<string>;
  if (!/^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$/.test(code)) {
    throw new TypeError(`Invalid city location code: ${code}`);
  }
  return Object.freeze({ ...definition, id, code });
}

/**
 * Product-owned semantic directory. These records contain no renderer
 * coordinates: a location resolves only to an existing stable world anchor.
 */
export const CITY_LOCATIONS: readonly CityLocation[] = Object.freeze([
  location({
    id: "project_intake",
    displayName: "Project Intake",
    purpose: "Receive project identity, descriptions, target information, and uploaded files.",
    category: "intake_planning",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "intake",
    focusKind: "direct"
  }),
  location({
    id: "requirements_hall",
    displayName: "Requirements Hall",
    purpose: "Decompose source requirements, record acceptance criteria, and surface clarifications.",
    category: "intake_planning",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "intake",
    focusKind: "nearest"
  }),
  location({
    id: "project_core",
    code: "PROJECT_CORE",
    displayName: "ESCO Project Core",
    purpose: "Represent the revisioned engineering source of truth shared across disciplines.",
    category: "engineering",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "automation",
    focusKind: "nearest"
  }),
  location({
    id: "template_library",
    displayName: "Template Library",
    purpose: "Reference approved engineering templates, naming rules, and reusable standards.",
    category: "engineering",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "automation",
    focusKind: "nearest"
  }),
  location({
    id: "tag_foundry",
    displayName: "Tag Foundry",
    purpose: "Develop evidence-linked tag names, interfaces, aliases, and data structures.",
    category: "engineering",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "automation",
    focusKind: "nearest"
  }),
  location({
    id: "automation_core",
    displayName: "Automation Core",
    purpose: "Coordinate controller architecture, programs, routines, and equipment modules.",
    category: "engineering",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "automation",
    focusKind: "direct"
  }),
  location({
    id: "ladder_logic_lab",
    displayName: "Ladder Logic Lab",
    purpose: "Review and develop structured PLC logic proposals with evidence traceability.",
    category: "engineering",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "automation",
    focusKind: "nearest"
  }),
  location({
    id: "electrical_works",
    displayName: "Electrical Works",
    purpose: "Reconcile devices, modules, addresses, signal types, and I/O schedules.",
    category: "engineering",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "electrical",
    focusKind: "direct"
  }),
  location({
    id: "hmi_district",
    displayName: "HMI District",
    purpose: "Develop template-based screen structures, bindings, navigation, and diagnostics.",
    category: "engineering",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "hmi",
    focusKind: "direct"
  }),
  location({
    id: "alarm_center",
    displayName: "Alarm Center",
    purpose: "Review alarm identifiers, triggers, priorities, messages, and operator response text.",
    category: "engineering",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "hmi",
    focusKind: "nearest"
  }),
  location({
    id: "qa_campus",
    displayName: "QA Campus",
    purpose: "Perform independent cross-discipline review and manage evidence-backed findings.",
    category: "review_assurance",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "qa",
    focusKind: "direct"
  }),
  location({
    id: "testing_grounds",
    displayName: "Testing Grounds",
    purpose: "Develop offline IAT, FAT, mode, alarm, interlock, and recovery test procedures.",
    category: "review_assurance",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "qa",
    focusKind: "nearest"
  }),
  location({
    id: "approval_plaza",
    displayName: "Approval Plaza",
    purpose: "Present named human decisions, risk acceptance, and release gates.",
    category: "review_assurance",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "approval",
    focusKind: "direct"
  }),
  location({
    id: "documentation_quarter",
    displayName: "Documentation Quarter",
    purpose: "Prepare reports, change records, manuals, and evidence indexes.",
    category: "delivery_release",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "documentation",
    focusKind: "direct"
  }),
  location({
    id: "release_logistics",
    displayName: "Release Logistics",
    purpose: "Assemble approved artifacts, manifests, checksums, and import-order guidance.",
    category: "delivery_release",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "release",
    focusKind: "direct"
  }),
  location({
    id: "training_academy",
    displayName: "Training Academy",
    purpose: "Provide a mapped campus destination for future walkthrough and onboarding experiences.",
    category: "campus_services",
    scope: "core",
    availability: "mapped_visual",
    focusBuildingId: "qa",
    focusKind: "nearest"
  }),
  location({
    id: "data_center",
    displayName: "Data Center",
    purpose: "Represent local backend, provider, storage, and project-service health.",
    category: "campus_services",
    scope: "core",
    availability: "backend_semantic",
    focusBuildingId: "automation",
    focusKind: "nearest"
  }),
  location({
    id: "central_park",
    displayName: "Central Park",
    purpose: "Provide a mapped visual landmark and breathing room in the engineering campus.",
    category: "campus_services",
    scope: "core",
    availability: "mapped_visual",
    focusBuildingId: "qa",
    focusKind: "nearest"
  }),
  location({
    id: "north_gateway",
    displayName: "North Gateway",
    purpose: "Visual expansion node reserved for a future northern campus arrival route.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "intake",
    focusKind: "nearest"
  }),
  location({
    id: "south_gateway",
    displayName: "South Gateway",
    purpose: "Visual expansion node reserved for a future southern service entrance.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "release",
    focusKind: "nearest"
  }),
  location({
    id: "innovation_district",
    displayName: "Innovation District",
    purpose: "Visual expansion node reserved for future approved engineering capabilities.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "automation",
    focusKind: "nearest"
  }),
  location({
    id: "research_park",
    displayName: "Research Park",
    purpose: "Visual expansion node reserved for future template and standards research functions.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "automation",
    focusKind: "nearest"
  }),
  location({
    id: "maintenance_depot",
    displayName: "Maintenance Depot",
    purpose: "Visual expansion node reserved for future support and maintenance workflows.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "qa",
    focusKind: "nearest"
  }),
  location({
    id: "supply_chain_hub",
    displayName: "Supply Chain Hub",
    purpose: "Visual expansion node reserved for future material and package coordination.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "release",
    focusKind: "nearest"
  }),
  location({
    id: "energy_plant",
    displayName: "Energy Plant",
    purpose: "Visual infrastructure node; it does not represent an enabled control workflow.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "electrical",
    focusKind: "nearest"
  }),
  location({
    id: "water_treatment",
    displayName: "Water Treatment",
    purpose: "Visual infrastructure node; it does not represent an enabled process-control workflow.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "electrical",
    focusKind: "nearest"
  }),
  location({
    id: "wind_solar_farm",
    displayName: "Wind & Solar Farm",
    purpose: "Visual renewable-energy node; it does not claim live utility integration.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "electrical",
    focusKind: "nearest"
  }),
  location({
    id: "port_terminal",
    displayName: "Port Terminal",
    purpose: "Visual logistics node reserved for future package-movement presentation.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "release",
    focusKind: "nearest"
  }),
  location({
    id: "industrial_zone",
    displayName: "Industrial Zone",
    purpose: "Visual industrial node reserved for future approved workflow capacity.",
    category: "future_capacity",
    scope: "expansion",
    availability: "mapped_visual",
    focusBuildingId: "electrical",
    focusKind: "nearest"
  })
]);

const LOCATION_BY_ID = new Map(CITY_LOCATIONS.map((item) => [item.id, item]));

export function getCityLocation(value: string): CityLocation | undefined {
  return LOCATION_BY_ID.get(value.trim().toLowerCase() as LocationId);
}

export function filterCityLocations(
  query: string,
  category: CityLocationCategory | "all" = "all",
  availability: CityLocationAvailability | "all" = "all"
): readonly CityLocation[] {
  const identityQuery = query
    .trim()
    .toLocaleLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  const exactLocation = CITY_LOCATIONS.find(
    (item) => item.id === identityQuery || item.code.toLocaleLowerCase() === identityQuery
  );
  if (
    exactLocation &&
    (category === "all" || exactLocation.category === category) &&
    (availability === "all" || exactLocation.availability === availability)
  ) {
    return [exactLocation];
  }
  const queryTerms = query
    .trim()
    .toLocaleLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .split(/\s+/)
    .filter(Boolean);
  return CITY_LOCATIONS.filter((item) => {
    if (category !== "all" && item.category !== category) return false;
    if (availability !== "all" && item.availability !== availability) return false;
    if (queryTerms.length === 0) return true;
    const searchable = [
      item.id,
      item.code,
      item.displayName,
      item.purpose,
      CITY_LOCATION_CATEGORY_LABELS[item.category],
      CITY_LOCATION_AVAILABILITY_LABELS[item.availability],
      WORLD_ANCHOR_NAMES[item.focusBuildingId]
    ]
      .join(" ")
      .toLocaleLowerCase()
      .replace(/[^a-z0-9]+/g, " ");
    return queryTerms.every((term) => searchable.includes(term));
  });
}
