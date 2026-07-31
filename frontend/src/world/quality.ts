export type WorldQualityMode = "auto" | "high" | "balanced" | "performance";
export type ResolvedWorldQualityMode = Exclude<WorldQualityMode, "auto">;
export type ShadowPolicy = "dynamic" | "static" | "off";
export type PostprocessingPolicy = "full" | "static" | "off";

export interface WorldQualityProfile {
  readonly mode: WorldQualityMode;
  readonly pixelRatioCap: number;
  readonly shadows: ShadowPolicy;
  readonly shadowMapSize: 0 | 1024 | 2048;
  readonly targetFps: number;
  readonly fallbackFps: number;
  readonly ambientLifeDensity: number;
  readonly labelDensity: number;
  readonly postprocessing: PostprocessingPolicy;
}

export interface WorldHardwareHints {
  readonly deviceMemoryGb?: number | null;
  readonly logicalProcessors?: number | null;
  readonly maxTextureSize?: number | null;
  readonly gpuTier?: "low" | "medium" | "high" | null;
  readonly mobile?: boolean;
  readonly powerPreference?: "low-power" | "default" | "high-performance";
}

export interface WorldQualitySelection {
  readonly requested: WorldQualityMode;
  readonly resolved: ResolvedWorldQualityMode;
  readonly profile: WorldQualityProfile;
  readonly reasons: readonly string[];
}

function profile(value: WorldQualityProfile): WorldQualityProfile {
  return Object.freeze(value);
}

/**
 * Rendering budgets exposed without consulting browser globals. `auto` is the
 * conservative baseline shown before hardware hints are available; selection
 * resolves it to one of the three concrete profiles.
 */
export const WORLD_QUALITY_PROFILES: Readonly<Record<WorldQualityMode, WorldQualityProfile>> =
  Object.freeze({
    auto: profile({
      mode: "auto",
      pixelRatioCap: 1.5,
      shadows: "static",
      shadowMapSize: 1024,
      targetFps: 45,
      fallbackFps: 30,
      ambientLifeDensity: 0.65,
      labelDensity: 0.72,
      postprocessing: "static"
    }),
    high: profile({
      mode: "high",
      pixelRatioCap: 2,
      shadows: "dynamic",
      shadowMapSize: 2048,
      targetFps: 60,
      fallbackFps: 30,
      ambientLifeDensity: 1,
      labelDensity: 1,
      postprocessing: "full"
    }),
    balanced: profile({
      mode: "balanced",
      pixelRatioCap: 1.5,
      shadows: "static",
      shadowMapSize: 1024,
      targetFps: 45,
      fallbackFps: 30,
      ambientLifeDensity: 0.65,
      labelDensity: 0.72,
      postprocessing: "static"
    }),
    performance: profile({
      mode: "performance",
      pixelRatioCap: 1,
      shadows: "off",
      shadowMapSize: 0,
      targetFps: 30,
      fallbackFps: 24,
      ambientLifeDensity: 0.25,
      labelDensity: 0.42,
      postprocessing: "off"
    })
  });

function positiveFinite(value: number | null | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : undefined;
}

function automaticMode(
  hints: WorldHardwareHints,
  reducedMotion: boolean
): { mode: ResolvedWorldQualityMode; reason: string } {
  const memory = positiveFinite(hints.deviceMemoryGb);
  const processors = positiveFinite(hints.logicalProcessors);
  const textureSize = positiveFinite(hints.maxTextureSize);

  const constrained =
    hints.gpuTier === "low" ||
    hints.powerPreference === "low-power" ||
    hints.mobile === true ||
    (memory !== undefined && memory < 4) ||
    (processors !== undefined && processors < 4) ||
    (textureSize !== undefined && textureSize < 4096);

  if (constrained) {
    return { mode: "performance", reason: "hardware:constrained" };
  }

  const capable =
    hints.gpuTier === "high" &&
    (memory === undefined || memory >= 8) &&
    (processors === undefined || processors >= 8) &&
    (textureSize === undefined || textureSize >= 8192);

  if (capable && !reducedMotion) {
    return { mode: "high", reason: "hardware:capable" };
  }

  return { mode: "balanced", reason: "hardware:balanced" };
}

function reducedMotionProfile(base: WorldQualityProfile): WorldQualityProfile {
  return profile({
    ...base,
    shadows: base.shadows === "dynamic" ? "static" : base.shadows,
    ambientLifeDensity: 0,
    postprocessing: base.postprocessing === "full" ? "static" : base.postprocessing
  });
}

export function selectWorldQuality(
  requested: WorldQualityMode = "auto",
  hints: WorldHardwareHints = {},
  reducedMotion = false
): WorldQualitySelection {
  const automatic = requested === "auto" ? automaticMode(hints, reducedMotion) : null;
  const resolved: ResolvedWorldQualityMode = automatic?.mode || requested as ResolvedWorldQualityMode;
  const base = WORLD_QUALITY_PROFILES[resolved];
  const reasons = [automatic?.reason || `preference:${requested}`];

  if (reducedMotion) reasons.push("accessibility:reduced-motion");

  return Object.freeze({
    requested,
    resolved,
    profile: reducedMotion ? reducedMotionProfile(base) : base,
    reasons: Object.freeze(reasons)
  });
}

export function selectWorldQualityProfile(
  requested: WorldQualityMode = "auto",
  hints: WorldHardwareHints = {},
  reducedMotion = false
): WorldQualityProfile {
  return selectWorldQuality(requested, hints, reducedMotion).profile;
}
