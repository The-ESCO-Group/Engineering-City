import { demoEvents, demoProject } from "../../api/demo";
import type { ReleaseSource } from "./types";

export function createDemoReleaseSource(): ReleaseSource {
  return {
    dataMode: "demo",
    fallbackReason: "Synthetic offline demo mode is active.",
    project: demoProject,
    reports: [],
    events: demoEvents,
    currentUser: null,
    capabilities: [],
    artifactDownloads: {},
    releaseZipDownload: null
  };
}
