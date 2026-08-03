import {
  AppWindow,
  BellRing,
  CircleAlert,
  ExternalLink,
  FileCheck2,
  FolderOpen,
  HardDrive,
  ShieldCheck
} from "lucide-react";
import { useEffect, useState } from "react";
import {
  loadDesktopCapabilities,
  loadSupportLogDirectory,
  openAuthorizedArtifact,
  openAuthorizedProjectDirectory,
  type DesktopCapabilities
} from "../desktop/bridge";
import "./DesktopCenter.css";

type RuntimeState = "checking" | "browser" | "desktop" | "error";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function DesktopCenter() {
  const [runtime, setRuntime] = useState<RuntimeState>("checking");
  const [capabilities, setCapabilities] = useState<DesktopCapabilities | null>(null);
  const [projectPath, setProjectPath] = useState("");
  const [artifactPath, setArtifactPath] = useState("");
  const [supportPath, setSupportPath] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [message, setMessage] = useState("");
  const [isError, setIsError] = useState(false);

  useEffect(() => {
    let active = true;
    void loadDesktopCapabilities()
      .then((value) => {
        if (!active) return;
        setCapabilities(value);
        setRuntime(value ? "desktop" : "browser");
      })
      .catch((error: unknown) => {
        if (!active) return;
        setRuntime("error");
        setIsError(true);
        setMessage(`Desktop capability check failed: ${errorMessage(error)}`);
      });
    return () => {
      active = false;
    };
  }, []);

  async function runAction(label: string, action: () => Promise<void>, success: string) {
    setBusyAction(label);
    setMessage("");
    setIsError(false);
    try {
      await action();
      setMessage(success);
    } catch (error) {
      setIsError(true);
      setMessage(errorMessage(error));
    } finally {
      setBusyAction("");
    }
  }

  const nativeAvailable = runtime === "desktop" && Boolean(capabilities);
  const unavailableReason = runtime === "checking"
    ? "Checking the local desktop runtime."
    : runtime === "browser"
      ? "Native file and notification actions are disabled in the browser. Install and run the desktop application to use them."
      : runtime === "error"
        ? "The desktop runtime did not return a valid capability boundary."
        : "";

  return (
    <section className="desktop-center" aria-labelledby="desktop-center-title">
      <header className="desktop-center__header">
        <div>
          <span className="desktop-center__eyebrow">SECURED LOCAL SHELL</span>
          <h2 id="desktop-center-title">Desktop integration</h2>
        </div>
        <span className={`desktop-center__runtime is-${nativeAvailable ? "desktop" : runtime}`}>
          <AppWindow aria-hidden="true" />
          {nativeAvailable ? "Desktop runtime" : runtime === "checking" ? "Checking" : "Browser runtime"}
        </span>
      </header>

      <div className={`desktop-center__state ${nativeAvailable ? "is-ready" : "is-browser"}`} role="status">
        {nativeAvailable ? <ShieldCheck aria-hidden="true" /> : <CircleAlert aria-hidden="true" />}
        <div>
          <strong>{nativeAvailable ? "Native boundary active" : "Native actions unavailable"}</strong>
          <p>{nativeAvailable
            ? "The installed shell will validate every path in Rust before asking Windows to open it."
            : unavailableReason}</p>
        </div>
      </div>

      {message ? (
        <p className={`desktop-center__message ${isError ? "is-error" : "is-success"}`} role={isError ? "alert" : "status"}>
          {message}
        </p>
      ) : null}

      <div className="desktop-center__facts" aria-label="Desktop safety boundary">
        <div><span>Backend boundary</span><strong>{capabilities?.local_backend_only ? "Loopback only" : "Desktop unavailable"}</strong></div>
        <div><span>Configured backend</span><strong>{capabilities?.backend_base_url ?? "Not exposed to browser"}</strong></div>
        <div><span>Equipment control</span><strong>{capabilities?.live_equipment_control ? "Enabled" : "Not supported"}</strong></div>
        <div><span>Project policy</span><strong>{capabilities ? "Authorized direct children" : "Validated by desktop only"}</strong></div>
      </div>

      <div className="desktop-center__section">
        <div className="desktop-center__section-heading"><FolderOpen aria-hidden="true" /><h3>Authorized project folder</h3></div>
        <p>The path must be a direct child of a configured ESCO projects root. Symlinks and lookalike paths are resolved before opening.</p>
        <label htmlFor="desktop-project-path">Absolute project directory</label>
        <div className="desktop-center__action-row">
          <input id="desktop-project-path" value={projectPath} onChange={(event) => setProjectPath(event.target.value)} placeholder="C:\\ESCO\\projects\\project-001" disabled={!nativeAvailable || Boolean(busyAction)} />
          <button type="button" onClick={() => void runAction("project", () => openAuthorizedProjectDirectory(projectPath.trim()), "Authorized project directory opened.")} disabled={!nativeAvailable || !projectPath.trim() || Boolean(busyAction)} title={!nativeAvailable ? unavailableReason : undefined}><ExternalLink aria-hidden="true" />Open</button>
        </div>
      </div>

      <div className="desktop-center__section">
        <div className="desktop-center__section-heading"><FileCheck2 aria-hidden="true" /><h3>Generated artifact</h3></div>
        <p>Only an existing file inside an authorized project can open. Allowed extensions: {capabilities?.allowed_artifact_extensions.map((item) => `.${item}`).join(", ") || "available in the desktop runtime"}.</p>
        <label htmlFor="desktop-artifact-path">Absolute artifact path</label>
        <div className="desktop-center__action-row">
          <input id="desktop-artifact-path" value={artifactPath} onChange={(event) => setArtifactPath(event.target.value)} placeholder="C:\\ESCO\\projects\\project-001\\reports\\summary.pdf" disabled={!nativeAvailable || Boolean(busyAction)} />
          <button type="button" onClick={() => void runAction("artifact", () => openAuthorizedArtifact(artifactPath.trim()), "Authorized artifact opened.")} disabled={!nativeAvailable || !artifactPath.trim() || Boolean(busyAction)} title={!nativeAvailable ? unavailableReason : undefined}><ExternalLink aria-hidden="true" />Open</button>
        </div>
      </div>

      <div className="desktop-center__section desktop-center__support">
        <div className="desktop-center__section-heading"><HardDrive aria-hidden="true" /><h3>Support &amp; notifications</h3></div>
        <p><BellRing aria-hidden="true" /> The installed application can notify the operator only when a human approval is required or a workflow fails.</p>
        <button type="button" onClick={() => void runAction("logs", async () => setSupportPath(await loadSupportLogDirectory()), "Desktop support-log location loaded.")} disabled={!nativeAvailable || Boolean(busyAction)} title={!nativeAvailable ? unavailableReason : undefined}><HardDrive aria-hidden="true" />Locate support logs</button>
        {supportPath ? <output aria-label="Desktop support log directory">{supportPath}</output> : null}
      </div>
    </section>
  );
}
