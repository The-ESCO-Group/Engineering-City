import { engineeringApi, reportDownloadUrl } from "../../api/client";
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
  | "releaseReadiness"
  | "releaseCandidates"
  | "releaseArtifacts"
>;

export interface ReleaseSourceIntegrationOptions {
  artifactDownloads?: Readonly<Record<string, ReleaseDownloadLink>>;
  releaseZipDownload?: ReleaseDownloadLink | null;
}

export function releaseSourceFromEngineering(
  data: ReleaseEngineeringData,
  options: ReleaseSourceIntegrationOptions = {}
): ReleaseSource {
  const currentCandidate = data.releaseCandidates[0] ?? null;
  const persistedDownload = currentCandidate?.archive?.download_path
    ? {
        url: reportDownloadUrl(currentCandidate.archive.download_path),
        filename: currentCandidate.archive.filename
      } satisfies ReleaseDownloadLink
    : null;
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
    releaseReadiness: data.releaseReadiness,
    releaseCandidates: data.releaseCandidates,
    releaseArtifacts: data.releaseArtifacts,
    artifactDownloads: {
      ...reportDownloads,
      ...options.artifactDownloads
    },
    releaseZipDownload: options.releaseZipDownload ?? persistedDownload
  };
}

type ReleaseOperationsEngineeringData = Pick<EngineeringData, "projectId" | "refresh">;

export type ReleaseOperationsIntegrationOptions = Pick<ReleaseOperations, "openCityLocation">;

export function releaseOperationsFromEngineering(
  data: ReleaseOperationsEngineeringData,
  options: ReleaseOperationsIntegrationOptions = {}
): ReleaseOperations {
  const requireProject = () => {
    if (!data.projectId) throw new Error("Select an authorized project first.");
    return data.projectId;
  };
  const requireCandidate = (candidateId: string | null) => {
    if (!candidateId) throw new Error("Create a release candidate first.");
    return candidateId;
  };
  return {
    ...options,
    createReleaseCandidate: async () => {
      const candidate = await engineeringApi.createReleaseCandidate(requireProject());
      await data.refresh();
      return { message: `Release candidate ${candidate.candidate_number} created.` };
    },
    generateArtifacts: async ({ candidateId }) => {
      const candidate = await engineeringApi.generateReleaseArtifacts(requireCandidate(candidateId));
      await data.refresh();
      return { message: `${candidate.artifact_count} verified artifact${candidate.artifact_count === 1 ? "" : "s"} generated.` };
    },
    finalizeRelease: async ({ candidateId }) => {
      const archive = await engineeringApi.finalizeRelease(requireCandidate(candidateId));
      await data.refresh();
      return { message: `Immutable release ${archive.filename} finalized and verified.` };
    }
  };
}
