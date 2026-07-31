import type { SemanticLocation } from "../generated";

declare const semanticIdBrand: unique symbol;

export type SemanticId<Kind extends string> = string & {
  readonly [semanticIdBrand]: Kind;
};

export type ProjectId = SemanticId<"project">;
export type EventId = SemanticId<"event">;
export type AgentId = SemanticId<"agent">;
export type TaskId = SemanticId<"task">;
export type LocationId = SemanticId<"location">;
export type PackageId = SemanticId<"package">;
export type ReleaseCandidateId = SemanticId<"release_candidate">;

const MAX_ENTITY_ID_LENGTH = 128;
const MAX_LOCATION_ID_LENGTH = 80;
const LOCATION_ID_PATTERN = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;

function semanticId<Kind extends string>(value: unknown): SemanticId<Kind> | null {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > MAX_ENTITY_ID_LENGTH ||
    value !== value.trim()
  ) {
    return null;
  }
  return value as SemanticId<Kind>;
}

export const toProjectId = (value: unknown): ProjectId | null => semanticId<"project">(value);
export const toEventId = (value: unknown): EventId | null => semanticId<"event">(value);
export const toAgentId = (value: unknown): AgentId | null => semanticId<"agent">(value);
export const toTaskId = (value: unknown): TaskId | null => semanticId<"task">(value);
export const toPackageId = (value: unknown): PackageId | null => semanticId<"package">(value);
export const toReleaseCandidateId = (value: unknown): ReleaseCandidateId | null =>
  semanticId<"release_candidate">(value);

export const SEMANTIC_LOCATION_IDS = [
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

const semanticLocationIds = new Set<string>(SEMANTIC_LOCATION_IDS);

/**
 * Accepts backend semantic location slugs, never renderer positions. Keeping
 * this validation here prevents coordinates or free-form labels from entering
 * the world model by accident.
 */
export function toLocationId(value: unknown): LocationId | null {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > MAX_LOCATION_ID_LENGTH ||
    value !== value.trim() ||
    !LOCATION_ID_PATTERN.test(value)
  ) {
    return null;
  }
  return value as LocationId;
}

/** Accepts only locations published by the generated backend contract. */
export function toSemanticLocationId(value: unknown): LocationId | null {
  const locationId = toLocationId(value);
  return locationId !== null && semanticLocationIds.has(locationId) ? locationId : null;
}

export function packageIdForProject(projectId: ProjectId): PackageId {
  return `project:${projectId}` as PackageId;
}
