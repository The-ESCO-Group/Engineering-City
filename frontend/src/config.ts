export const PRODUCT_VERSION = "0.1.0";
export const CONTRACT_VERSION = "1.1.0";

type RuntimeEnvironment = Record<string, string | undefined>;

function runtimeEnvironment(): RuntimeEnvironment {
  return ((import.meta as ImportMeta & { env?: RuntimeEnvironment }).env || {});
}

function environmentFlag(name: string, fallback: boolean): boolean {
  const value = runtimeEnvironment()[name];
  if (value === undefined) return fallback;
  return value.toLowerCase() === "true" || value === "1";
}

export interface ProductFeatureFlags {
  liveBackend: boolean;
  projectCore: boolean;
  changesets: boolean;
  releaseCenter: boolean;
  targetGenerators: boolean;
}

export function productFeatureFlags(): ProductFeatureFlags {
  const demoQuery = new URLSearchParams(window.location.search).get("demo")?.toLowerCase();
  const demoRequested = demoQuery === "1" || demoQuery === "true";
  return {
    liveBackend: !demoRequested && !environmentFlag("VITE_DEMO_MODE", false),
    projectCore: environmentFlag("VITE_FEATURE_PROJECT_CORE", false),
    changesets: environmentFlag("VITE_FEATURE_CHANGESETS", false),
    releaseCenter: environmentFlag("VITE_FEATURE_RELEASE_CENTER", false),
    targetGenerators: environmentFlag("VITE_FEATURE_TARGET_GENERATORS", false)
  };
}

export function configuredApiBase(): string {
  const configured = runtimeEnvironment().VITE_ESCO_API_BASE?.trim();
  return (configured || "/api").replace(/\/$/, "");
}

export function configuredProjectId(): string | null {
  const fromEnvironment = runtimeEnvironment().VITE_ESCO_PROJECT_ID?.trim();
  if (fromEnvironment) return fromEnvironment;
  const fromUrl = new URLSearchParams(window.location.search).get("project")?.trim();
  return fromUrl || window.localStorage.getItem("esco:selected-project");
}
