import {
  AlertTriangle,
  ChevronRight,
  Download,
  Eye,
  FileArchive,
  FileCode2,
  FileText,
  Folder,
  Hash,
  PackageOpen,
  ShieldCheck
} from "lucide-react";
import { useMemo, useState } from "react";
import type { ReleaseArtifact, ReleaseArtifactFolder } from "./types";

export interface ArtifactTreeProps {
  folders: ReleaseArtifactFolder[];
  candidateNumber: number | null;
}

function formatBytes(bytes: number): string {
  if (!bytes) return "Size not reported";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function compactHash(hash: string): string {
  return hash ? `${hash.slice(0, 10)}…${hash.slice(-8)}` : "Not reported";
}

function ArtifactIcon({ artifact }: { artifact: ReleaseArtifact }) {
  if (artifact.kind.includes("l5x") || artifact.kind.includes("xml")) return <FileCode2 aria-hidden="true" />;
  if (artifact.kind === "report" || artifact.kind.includes("summary") || artifact.kind.includes("limitations")) return <FileText aria-hidden="true" />;
  return <FileArchive aria-hidden="true" />;
}

export function ArtifactTree({ folders, candidateNumber }: ArtifactTreeProps) {
  const artifacts = useMemo(() => folders.flatMap((folder) => folder.artifacts), [folders]);
  const [selectedId, setSelectedId] = useState<string | null>(() => artifacts[0]?.id ?? null);
  const selected = artifacts.find((artifact) => artifact.id === selectedId) ?? artifacts[0] ?? null;
  return (
    <section className="release-panel release-artifacts" aria-labelledby="release-artifacts-title">
      <header className="release-section-heading">
        <div className="release-heading-icon orange"><PackageOpen aria-hidden="true" /></div>
        <div>
          <span>PACKAGE CONTENTS</span>
          <h2 id="release-artifacts-title">Artifact tree</h2>
          <p>{candidateNumber === null ? "No release candidate has been reported." : `Release candidate ${candidateNumber} · ${artifacts.length} event-backed artifact${artifacts.length === 1 ? "" : "s"}`}</p>
        </div>
      </header>

      <div className="release-artifact-layout">
        <nav className="release-folder-tree" aria-label="Release artifact folders">
          {folders.map((folder) => (
            <details key={folder.id} open={folder.artifacts.length > 0}>
              <summary><Folder aria-hidden="true" /><b>{folder.sequence} {folder.label}</b><span>{folder.artifacts.length}</span><ChevronRight aria-hidden="true" /></summary>
              <p>{folder.description}</p>
              {folder.artifacts.length === 0 ? (
                <div className="release-folder-empty">No artifact event received</div>
              ) : folder.artifacts.map((artifact) => (
                <button type="button" key={artifact.id} className={selected?.id === artifact.id ? "selected" : ""} onClick={() => setSelectedId(artifact.id)}>
                  <ArtifactIcon artifact={artifact} />
                  <span><b>{artifact.filename}</b><small>{artifact.kind.replaceAll("_", " ")} · {formatBytes(artifact.sizeBytes)}</small></span>
                  {artifact.warningCount > 0 && <AlertTriangle aria-label={`${artifact.warningCount} warnings`} />}
                </button>
              ))}
            </details>
          ))}
        </nav>

        <div className="release-artifact-detail">
          {selected ? (
            <>
              <header>
                <div><span>{selected.folderId.replace("-", " · ").toUpperCase()}</span><h3>{selected.filename}</h3><code>{selected.relativePath}</code></div>
                <span className={`release-validation-level level-${selected.validationLevel}`}>{selected.validationLevel.replaceAll("_", " ")}</span>
              </header>
              <dl className="release-artifact-metadata">
                <div><dt>Type</dt><dd>{selected.kind.replaceAll("_", " ")}</dd></div>
                <div><dt>Size</dt><dd>{formatBytes(selected.sizeBytes)}</dd></div>
                <div><dt>Generator</dt><dd>{selected.generator}</dd></div>
                <div><dt>Source revision</dt><dd>{selected.sourceRevision ?? "Not reported"}</dd></div>
                <div><dt>Created</dt><dd>{selected.createdAt ? new Date(selected.createdAt).toLocaleString() : "Not reported"}</dd></div>
                <div><dt>Import order</dt><dd>{selected.importOrder || "Not assigned"}</dd></div>
                <div className="wide"><dt>Target</dt><dd>{selected.target}</dd></div>
                <div className="wide"><dt>SHA-256</dt><dd><Hash aria-hidden="true" /><code title={selected.sha256 || undefined}>{compactHash(selected.sha256)}</code></dd></div>
              </dl>
              <div className="release-artifact-actions">
                {selected.download ? <a href={selected.download.url} download={selected.download.filename || selected.filename}><Download aria-hidden="true" />Download generated file</a> : <button type="button" disabled title="The API host did not supply an authorized file URL."><Download aria-hidden="true" />Download unavailable</button>}
                {selected.download?.previewUrl ? <a className="secondary" href={selected.download.previewUrl} target="_blank" rel="noreferrer"><Eye aria-hidden="true" />Safe preview</a> : <button type="button" className="secondary" disabled title="No safe preview URL was supplied."><Eye aria-hidden="true" />Preview unavailable</button>}
              </div>
              {!selected.download && <p className="release-disabled-explanation">This row came from a real export artifact event, but the current API contract did not provide a downloadable artifact URL. No fake download is offered.</p>}
              <section className="release-diagnostics">
                <h4><ShieldCheck aria-hidden="true" />Diagnostics</h4>
                {selected.diagnostics.length ? <ul>{selected.diagnostics.map((diagnostic) => <li key={diagnostic}>{diagnostic}</li>)}</ul> : <p>No artifact diagnostics were reported.</p>}
              </section>
            </>
          ) : (
            <div className="release-artifact-blank"><PackageOpen aria-hidden="true" /><h3>No artifact events</h3><p>The required folder structure is visible, but files appear only after export.artifact_created events arrive.</p></div>
          )}
        </div>
      </div>
      <p className="release-verification-disclaimer"><ShieldCheck aria-hidden="true" />A “generated” artifact is not a Rockwell-import-verified artifact. Validation levels above are reproduced exactly from the event contract.</p>
    </section>
  );
}
