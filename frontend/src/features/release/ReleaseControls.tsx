import {
  Archive,
  BadgeCheck,
  Boxes,
  Download,
  FlaskConical,
  LoaderCircle,
  PackagePlus,
  Send
} from "lucide-react";
import { useState, type ComponentType } from "react";
import type {
  ReleaseActionAvailability,
  ReleaseActionContext,
  ReleaseActionId,
  ReleaseDownloadLink,
  ReleaseOperations
} from "./types";

export interface ReleaseControlsProps {
  actions: ReleaseActionAvailability[];
  context: ReleaseActionContext | null;
  operations?: ReleaseOperations;
  releaseZipDownload: ReleaseDownloadLink | null;
}

const ACTION_ICONS: Record<ReleaseActionId, ComponentType<{ "aria-hidden"?: boolean; className?: string }>> = {
  run_validation: FlaskConical,
  create_release_candidate: PackagePlus,
  generate_artifacts: Boxes,
  request_import_verification: BadgeCheck,
  request_final_approval: Send,
  download_release_zip: Download
};

function operationFor(id: ReleaseActionId, operations?: ReleaseOperations) {
  if (id === "run_validation") return operations?.runValidation;
  if (id === "create_release_candidate") return operations?.createReleaseCandidate;
  if (id === "generate_artifacts") return operations?.generateArtifacts;
  if (id === "request_import_verification") return operations?.requestImportVerification;
  if (id === "request_final_approval") return operations?.requestFinalApproval;
  return undefined;
}

export function ReleaseControls({ actions, context, operations, releaseZipDownload }: ReleaseControlsProps) {
  const [busy, setBusy] = useState<ReleaseActionId | null>(null);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const invoke = async (action: ReleaseActionAvailability) => {
    const operation = operationFor(action.id, operations);
    if (!action.enabled || !context || !operation) return;
    setBusy(action.id);
    setMessage(null);
    try {
      const result = await operation(context);
      setMessage({ kind: "success", text: result?.message || `${action.label} request accepted by the integration. Awaiting event-stream confirmation.` });
    } catch (error) {
      setMessage({ kind: "error", text: error instanceof Error ? error.message : `${action.label} failed.` });
    } finally {
      setBusy(null);
    }
  };
  return (
    <section className="release-panel release-controls" aria-labelledby="release-controls-title">
      <header className="release-section-heading compact">
        <div className="release-heading-icon"><Archive aria-hidden="true" /></div>
        <div><span>AUTHORIZED OPERATIONS</span><h2 id="release-controls-title">Release controls</h2><p>Disabled controls state the missing permission, contract operation, or prerequisite.</p></div>
      </header>
      <div className="release-control-grid">
        {actions.map((action) => {
          const Icon = ACTION_ICONS[action.id];
          const isBusy = busy === action.id;
          if (action.id === "download_release_zip" && action.enabled && releaseZipDownload) {
            return <div className="release-control" key={action.id}><a className="primary" href={releaseZipDownload.url} download={releaseZipDownload.filename || "release.zip"}><Icon aria-hidden={true} />{action.label}</a><p>{action.reason}</p></div>;
          }
          return (
            <div className="release-control" key={action.id}>
              <button type="button" className={action.id === "download_release_zip" ? "primary" : ""} disabled={!action.enabled || isBusy || !context} onClick={() => void invoke(action)} title={!action.enabled ? action.reason : undefined}>
                {isBusy ? <LoaderCircle aria-hidden="true" className="spin" /> : <Icon aria-hidden={true} />}{action.label}
              </button>
              <p className={action.enabled ? "enabled" : "disabled"}>{action.reason}</p>
            </div>
          );
        })}
      </div>
      {message && <p className={`release-action-message ${message.kind}`} role="status">{message.text}</p>}
      <p className="release-safety-boundary">This workspace creates and reviews offline artifacts only. It contains no controller connection, download, mode, force, or live-control action.</p>
    </section>
  );
}
