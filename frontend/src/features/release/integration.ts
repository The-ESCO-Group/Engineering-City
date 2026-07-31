import { reportDownloadUrl } from "../../api/client";
import type { EngineeringData } from "../../api/live";
import type { ReleaseDownloadLink, ReleaseOperations, ReleaseSource } from "./types";

type ReleaseEngineeringData = Pick<
  EngineeringData,
  | "dataMode"
  | "fallbackReason"
  | "project"
  | "reports"
  | "events"
  | "currentUser"
  | "capabilities"
>;

export interface ReleaseSourceIntegrationOptions {
  artifactDownloads?: Readonly<Record<string, ReleaseDownloadLink>>;
  releaseZipDownload?: ReleaseDownloadLink | null;
}

export function releaseSourceFromEngineering(
  data: ReleaseEngineeringData,
  options: ReleaseSourceIntegrationOptions = {}
): ReleaseSource {
  const reportDownloads = Object.fromEntries(
    data.reports
      .filter((report) => report.status === "generated" && Boolean(report.download_path))
      .map((report) => [report.id, {
        url: reportDownloadUrl(report.download_path),
        filename: report.title
      } satisfies ReleaseDownloadLink])
  );
  return {
    dataMode: data.dataMode,
    fallbackReason: data.fallbackReason,
    project: data.project,
    reports: data.reports,
    events: data.events,
    currentUser: data.currentUser,
    capabilities: data.capabilities,
    artifactDownloads: {
      ...reportDownloads,
      ...options.artifactDownloads
    },
    releaseZipDownload: options.releaseZipDownload ?? null
  };
}

export type ReleaseOperationsIntegrationOptions = ReleaseOperations;

export function releaseOperationsFromEngineering(
  options: ReleaseOperationsIntegrationOptions = {}
): ReleaseOperations {
  return { ...options };
}
