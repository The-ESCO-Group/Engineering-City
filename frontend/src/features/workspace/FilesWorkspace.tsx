import { Archive, FileCheck2, FileQuestion, Filter, Search, SearchX, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import type { WorkspaceFile } from "./types";

export interface FilesWorkspaceProps {
  files: WorkspaceFile[];
  onOpenIntake?: () => void;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function FilesWorkspace({ files, onOpenIntake }: FilesWorkspaceProps) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const statuses = useMemo(() => [...new Set(files.map((file) => file.status))].sort(), [files]);
  const visible = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return files.filter((file) => {
      const matches = `${file.name} ${file.kind} ${file.role}`.toLowerCase().includes(normalized);
      return matches && (status === "all" || file.status === status);
    });
  }, [files, query, status]);

  return (
    <section className="eng-files-workspace" aria-labelledby="files-workspace-title">
      <header className="eng-workspace-header">
        <div><span className="eng-eyebrow">SOURCE CONTROL</span><h2 id="files-workspace-title">Project files</h2><p>Uploaded sources, parser state, archive provenance, and immutable content hashes.</p></div>
        <button type="button" className="eng-secondary-action" disabled={!onOpenIntake} onClick={onOpenIntake} title={!onOpenIntake ? "Intake navigation is not connected." : undefined}>Add files through intake</button>
      </header>
      <div className="eng-toolbar">
        <label className="eng-search"><Search aria-hidden="true" /><span className="sr-only">Search project files</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search filename, kind, or role…" /></label>
        <label className="eng-select"><Filter aria-hidden="true" /><span className="sr-only">Filter file status</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">All parser states</option>{statuses.map((item) => <option key={item} value={item}>{humanize(item)}</option>)}</select></label>
        <span className="eng-result-count">{visible.length} of {files.length}</span>
      </div>
      {visible.length === 0 ? (
        <div className="eng-large-empty"><SearchX aria-hidden="true" /><h3>{files.length ? "No files match" : "No project files"}</h3><p>{files.length ? "Change the search or parser-state filter." : "Add source files through guided intake; no files are inferred."}</p></div>
      ) : (
        <div className="eng-table-scroll">
          <table className="eng-data-table eng-file-table">
            <thead><tr><th>Source file</th><th>Role</th><th>Parser state</th><th>Size</th><th>Content hash</th><th>Archive source</th></tr></thead>
            <tbody>{visible.map((file) => (
              <tr key={file.id} id={`file-${file.id}`}>
                <td><div className="eng-file-name">{file.status === "parsed" ? <FileCheck2 aria-hidden="true" /> : <FileQuestion aria-hidden="true" />}<span><b>{file.name}</b><small>{humanize(file.kind)} · updated {new Date(file.updatedAt).toLocaleString()}</small></span></div></td>
                <td>{humanize(file.role)}</td>
                <td><span className={`eng-status ${file.status}`}><ShieldCheck aria-hidden="true" />{humanize(file.status)}</span>{file.detail && <small className="eng-cell-note">{file.detail}</small>}</td>
                <td>{formatBytes(file.sizeBytes)}</td>
                <td><code title={file.sha256}>{file.sha256 ? `${file.sha256.slice(0, 14)}…` : "Not published"}</code></td>
                <td>{file.archiveMemberPath ? <span><Archive aria-hidden="true" />{file.archiveMemberPath}</span> : "Direct upload"}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

