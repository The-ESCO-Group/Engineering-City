import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  CONTRACT_VERSION,
  PRODUCT_VERSION,
  configuredApiBase,
  configuredProjectId,
  productFeatureFlags
} from "./config";

describe("product configuration", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/");
    window.localStorage.clear();
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("exposes the pinned product and backend contract versions", () => {
    expect(PRODUCT_VERSION).toBe("0.1.0");
    expect(CONTRACT_VERSION).toBe("1.1.0");
  });

  it("uses the safe feature-flag defaults", () => {
    expect(productFeatureFlags()).toEqual({
      liveBackend: true,
      projectCore: false,
      changesets: false,
      releaseCenter: false,
      targetGenerators: false
    });
  });

  it("requires an explicit URL switch before using the offline demo adapter", () => {
    window.history.replaceState({}, "", "/?demo=1");
    expect(productFeatureFlags().liveBackend).toBe(false);

    window.history.replaceState({}, "", "/?demo=false");
    expect(productFeatureFlags().liveBackend).toBe(true);
  });

  it("uses the same-origin API proxy by default", () => {
    expect(configuredApiBase()).toBe("/api");
  });

  it("selects a URL project before the saved preference", () => {
    window.localStorage.setItem("esco:selected-project", "saved-project");
    window.history.replaceState({}, "", "/?project=%20url-project%20");
    expect(configuredProjectId()).toBe("url-project");

    window.history.replaceState({}, "", "/");
    expect(configuredProjectId()).toBe("saved-project");

    window.localStorage.clear();
    expect(configuredProjectId()).toBeNull();
  });
});
