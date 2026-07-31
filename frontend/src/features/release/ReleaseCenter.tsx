import {
  Archive,
  ArrowLeft,
  Boxes,
  CheckCircle2,
  FileStack,
  PackageCheck,
  ShieldAlert,
  ShieldCheck,
  Warehouse
} from "lucide-react";
import { useMemo, useState } from "react";
import { buildReleaseModel } from "./adapter";
import { ArtifactTree } from "./ArtifactTree";
import { ReleaseControls } from "./ReleaseControls";
import { ReleaseReadiness } from "./ReleaseReadiness";
import { ReleaseRoute } from "./ReleaseRoute";
import { ReportLibrary } from "./ReportLibrary";
import type { ReleaseActionId, ReleaseOperations, ReleaseSource } from "./types";
import "./ReleaseCenter.css";

export type ReleaseCenterView = "overview" | "artifacts" | "reports";

export interface ReleaseCenterProps {
  source: ReleaseSource;
  operations?: ReleaseOperations;
  initialView?: ReleaseCenterView;
  onReturnToCity?: () => void;
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function ReleaseCenter({ source, operations, initialView = "overview", onReturnToCity }: ReleaseCenterProps) {
  const [view, setView] = useState<ReleaseCenterView>(initialView);
  const operationSupport = useMemo<Partial<Record<ReleaseActionId, boolean>>>(() => ({
    run_validation: Boolean(operations?.runValidation),
    create_release_candidate: Boolean(operations?.createReleaseCandidate),
    generate_artifacts: Boolean(operations?.generateArtifacts),
    request_import_verification: Boolean(operations?.requestImportVerification),
    request_final_approval: Boolean(operations?.requestFinalApproval),
    download_release_zip: Boolean(source.releaseZipDownload)
  }), [operations?.createReleaseCandidate, operations?.generateArtifacts, operations?.requestFinalApproval, operations?.requestImportVerification, operations?.runValidation, source.releaseZipDownload]);
  const model = useMemo(() => buildReleaseModel(source, operationSupport), [operationSupport, source]);
  const passedGates = model.gates.filter((gate) => gate.state === "passed" || gate.state === "ready").length;
  const context = model.project ? {
    projectId: model.project.id,
    candidateId: model.candidate?.id ?? null,
    revision: model.project.revision
  } : null;
  const released = Boolean(model.releaseOccurredAt);
  return (
    <div className="release-center">
      <header className="release-topbar">
        <div className="release-brand"><span><Warehouse aria-hidden="true" /></span><div><b>RELEASE LOGISTICS</b><small>ESCO ENGINEERING CITY</small></div></div>
        <div className="release-project-context"><span>{model.project?.reference || "NO PROJECT"}</span><b>{model.project?.name || "Release data unavailable"}</b><small>{model.project ? `${humanize(model.project.status)} · ${model.project.revision === null ? "Revision not reported" : `Revision ${model.project.revision}`}` : model.unavailableReason}</small></div>
        <div className="release-top-stats"><span><b>{passedGates}/{model.gates.length}</b>gates</span><span><b>{model.artifactCoverage.actual}</b>artifacts</span><span><b>{model.reports.filter((report) => report.files.length).length}</b>reports</span></div>
        <button type="button" className="release-return" disabled={!onReturnToCity} onClick={onReturnToCity} title={!onReturnToCity ? "City navigation is not connected in this host." : undefined}><ArrowLeft aria-hidden="true" />Return to city</button>
      </header>
      {model.demoLabel && <div className="release-demo-banner" role="status"><ShieldAlert aria-hidden="true" /><b>{model.demoLabel}</b><span>No customer data · no deployable files · no generation API calls</span></div>}
      {model.mode === "unavailable" && <div className="release-unavailable-banner" role="alert"><ShieldAlert aria-hidden="true" /><b>Authorized release state is unavailable.</b><span>{model.unavailableReason}</span></div>}

      <div className="release-shell">
        <aside className="release-sidebar">
          <div className="release-candidate-card">
            <span>ACTIVE PACKAGE</span>
            {model.candidate ? <><PackageCheck aria-hidden="true" /><h2>Candidate {model.candidate.number}</h2><p>{humanize(model.candidate.status)}</p><dl><div><dt>Validation</dt><dd>{humanize(model.candidate.validationLevel)}</dd></div><div><dt>Revision</dt><dd>{model.candidate.revision}</dd></div><div><dt>Expected</dt><dd>{model.candidate.expectedArtifactCount ?? "Not reported"}</dd></div><div><dt>Limitations</dt><dd>{model.candidate.limitationCount}</dd></div></dl></> : <><Archive aria-hidden="true" /><h2>No candidate</h2><p>Awaiting a release.candidate_created event.</p></>}
          </div>
          <nav aria-label="Release Center sections">
            <button type="button" className={view === "overview" ? "active" : ""} onClick={() => setView("overview")} aria-current={view === "overview" ? "page" : undefined}><ShieldCheck aria-hidden="true" /><span>Readiness</span><b>{passedGates}</b></button>
            <button type="button" className={view === "artifacts" ? "active" : ""} onClick={() => setView("artifacts")} aria-current={view === "artifacts" ? "page" : undefined}><Boxes aria-hidden="true" /><span>Artifacts</span><b>{model.artifactCoverage.actual}</b></button>
            <button type="button" className={view === "reports" ? "active" : ""} onClick={() => setView("reports")} aria-current={view === "reports" ? "page" : undefined}><FileStack aria-hidden="true" /><span>Reports</span><b>{model.reports.filter((report) => report.files.length).length}</b></button>
          </nav>
          <div className="release-role"><ShieldCheck aria-hidden="true" /><span>AUTHORIZED AS</span><p>{model.userRoleSummary}</p></div>
          <p className="release-boundary"><ShieldAlert aria-hidden="true" />This workspace has no direct PLC upload, online editing, controller mode, force, or live equipment control.</p>
        </aside>

        <main className="release-main" id={`release-${view}`} tabIndex={-1}>
          <header className="release-page-heading">
            <div><span>{view === "overview" ? "RELEASE COMMAND" : view === "artifacts" ? "PACKAGE INSPECTION" : "REPORT DELIVERY"}</span><h1>{view === "overview" ? "Release readiness" : view === "artifacts" ? "Artifact package" : "Engineering reports"}</h1><p>{view === "overview" ? "Confirm every gate, validation result, human decision, and verification boundary before handoff." : view === "artifacts" ? "Inspect event-backed package contents, hashes, provenance, and diagnostics." : "Download only files generated and exposed by the authorized local API."}</p></div>
            <div className={`release-status-card ${released ? "ready" : "pending"}`}>{released ? <CheckCircle2 aria-hidden="true" /> : <ShieldAlert aria-hidden="true" />}<span><small>HANDOFF STATE</small><b>{released ? "READY EVENT RECORDED" : model.candidate ? humanize(model.candidate.status) : "NOT ASSEMBLED"}</b></span></div>
          </header>

          {view === "overview" && <>
            <div className="release-overview-grid">
              <ReleaseRoute candidate={model.candidate} released={released} onOpenCityLocation={operations?.openCityLocation} />
              <section className="release-panel release-overview-summary" aria-label="Package summary">
                <span>PACKAGE FACTS</span>
                <dl>
                  <div><dt>Artifacts observed</dt><dd>{model.artifactCoverage.actual}<small>{model.artifactCoverage.expected === null ? "expected count not reported" : `${model.artifactCoverage.expected} expected`}</small></dd></div>
                  <div><dt>Hashes observed</dt><dd>{model.artifactCoverage.hashed}<small>event-reported SHA-256</small></dd></div>
                  <div><dt>Import verification</dt><dd>{humanize(model.verification.status)}<small>{model.verification.candidateOnly ? "candidate only" : "workstation verified"}</small></dd></div>
                  <div><dt>Target</dt><dd className="target">{model.project?.targetProfile || "Not available"}</dd></div>
                </dl>
              </section>
            </div>
            <ReleaseReadiness gates={model.gates} validation={model.validation} verification={model.verification} />
            <ReleaseControls actions={model.actions} context={context} operations={operations} releaseZipDownload={model.releaseZipDownload} />
          </>}
          {view === "artifacts" && <ArtifactTree folders={model.folders} candidateNumber={model.candidate?.number ?? null} />}
          {view === "reports" && <ReportLibrary reports={model.reports} availableFormats={model.reportFormats} />}
        </main>
      </div>
    </div>
  );
}
