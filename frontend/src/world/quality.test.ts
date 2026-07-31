// @vitest-environment node

import { describe, expect, it } from "vitest";
import {
  WORLD_QUALITY_PROFILES,
  selectWorldQuality,
  selectWorldQualityProfile
} from "./quality";

describe("world quality profiles", () => {
  it("is safe to import without browser globals", () => {
    expect("window" in globalThis).toBe(false);
    expect(selectWorldQuality().resolved).toBe("balanced");
  });

  it("publishes monotonic rendering budgets for every supported mode", () => {
    expect(Object.keys(WORLD_QUALITY_PROFILES)).toEqual([
      "auto",
      "high",
      "balanced",
      "performance"
    ]);
    expect(WORLD_QUALITY_PROFILES.high.pixelRatioCap).toBeGreaterThan(
      WORLD_QUALITY_PROFILES.balanced.pixelRatioCap
    );
    expect(WORLD_QUALITY_PROFILES.balanced.pixelRatioCap).toBeGreaterThan(
      WORLD_QUALITY_PROFILES.performance.pixelRatioCap
    );
    expect(WORLD_QUALITY_PROFILES.high.ambientLifeDensity).toBeGreaterThan(
      WORLD_QUALITY_PROFILES.balanced.ambientLifeDensity
    );
    expect(WORLD_QUALITY_PROFILES.balanced.labelDensity).toBeGreaterThan(
      WORLD_QUALITY_PROFILES.performance.labelDensity
    );
    expect(WORLD_QUALITY_PROFILES.performance.shadows).toBe("off");
    expect(WORLD_QUALITY_PROFILES.performance.postprocessing).toBe("off");
  });

  it("selects high quality only for clearly capable hardware", () => {
    const selection = selectWorldQuality("auto", {
      deviceMemoryGb: 16,
      logicalProcessors: 12,
      maxTextureSize: 16384,
      gpuTier: "high",
      powerPreference: "high-performance"
    });

    expect(selection).toMatchObject({
      requested: "auto",
      resolved: "high",
      reasons: ["hardware:capable"]
    });
    expect(selection.profile).toBe(WORLD_QUALITY_PROFILES.high);
  });

  it.each([
    { gpuTier: "low" as const },
    { deviceMemoryGb: 2 },
    { logicalProcessors: 2 },
    { maxTextureSize: 2048 },
    { mobile: true },
    { powerPreference: "low-power" as const }
  ])("selects performance for constrained hardware hints: %o", (hints) => {
    const selection = selectWorldQuality("auto", hints);

    expect(selection.resolved).toBe("performance");
    expect(selection.reasons).toContain("hardware:constrained");
  });

  it("uses balanced quality when hints are missing, partial, or invalid", () => {
    expect(selectWorldQuality("auto").resolved).toBe("balanced");
    expect(
      selectWorldQuality("auto", {
        deviceMemoryGb: Number.NaN,
        logicalProcessors: -1,
        maxTextureSize: 0,
        gpuTier: "medium"
      }).resolved
    ).toBe("balanced");
  });

  it("respects an explicit preference independently of hardware hints", () => {
    const profile = selectWorldQualityProfile("high", {
      deviceMemoryGb: 2,
      logicalProcessors: 2,
      gpuTier: "low"
    });

    expect(profile).toBe(WORLD_QUALITY_PROFILES.high);
  });

  it("applies a deterministic reduced-motion overlay", () => {
    const hints = {
      deviceMemoryGb: 16,
      logicalProcessors: 12,
      maxTextureSize: 16384,
      gpuTier: "high" as const
    };
    const first = selectWorldQuality("auto", hints, true);
    const second = selectWorldQuality("auto", hints, true);

    expect(first).toEqual(second);
    expect(first.resolved).toBe("balanced");
    expect(first.profile.ambientLifeDensity).toBe(0);
    expect(first.profile.shadows).toBe("static");
    expect(first.profile.postprocessing).toBe("static");
    expect(first.reasons).toContain("accessibility:reduced-motion");
  });

  it("keeps published profiles and selection reasons immutable", () => {
    const selection = selectWorldQuality("performance");

    expect(Object.isFrozen(WORLD_QUALITY_PROFILES)).toBe(true);
    expect(Object.isFrozen(selection.profile)).toBe(true);
    expect(Object.isFrozen(selection.reasons)).toBe(true);
  });
});
