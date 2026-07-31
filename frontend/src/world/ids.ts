declare const semanticIdBrand: unique symbol;

export type SemanticId<Kind extends string> = string & {
  readonly [semanticIdBrand]: Kind;
};

export type ProjectId = SemanticId<"project">;
export type EventId = SemanticId<"event">;
export type AgentId = SemanticId<"agent">;
export type TaskId = SemanticId<"task">;
export type LocationId = SemanticId<"location">;

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
