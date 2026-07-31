import {
  AlertTriangle,
  Check,
  Download,
  FileCheck2,
  FileText,
  Filter,
  Hash,
  Info,
  X
} from "lucide-react";
import { useMemo, useState } from "react";
import type { ReleaseReportCatalogItem, ReleaseReportFile } from "./types";

export interface ReportLibraryProps {
  reports: ReleaseReportCatalogItem[];
  availableFormats: string[];
}

const REQUIRED_FORMATS = ["html", "pdf", "docx", "xlsx", "csv", "json"];

function formatBytes(bytes: number): string {
  if (!bytes) return "Size not reported";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function ReportFileCard({ file }: { file: ReleaseReportFile }) {
  return (
    <article className={`release-report-file status-${file.status}`}>
      <header><FileText aria-hidden="true" /><div><b>{file.format.toUpperCase()}</b><span>{file.mediaType || "Media type not reported"}</span></div><em>{file.status}</em></header>
      <dl>
        <div><dt>Size</dt><dd>{formatBytes(file.sizeBytes)}</dd></div>
        <div><dt>Generator</dt><dd>{file.provider} {file.providerVersion !== "Not reported" ? file.providerVersion : ""}</dd></div>
        <div><dt>Created</dt><dd>{new Date(file.createdAt).toLocaleString()}</dd></div>
        <div><dt>SHA-256</dt><dd><Hash aria-hidden="true" />{file.sha256 ? `${file.sha256.slice(0, 12)}…` : "Not reported"}</dd></div>
      </dl>
      {file.error && <p className="release-report-error"><AlertTriangle aria-hidden="true" />{file.error}</p>}
      {file.download ? <a href={file.download.url} download={file.download.filename || file.title}><Download aria-hidden="true" />Download generated {file.format.toUpperCase()}</a> : <button type="button" disabled title="A generated report download path was not supplied by the authorized API host."><Download aria-hidden="true" />Download unavailable</button>}
      {!file.download && <small>No fake file is created in the browser.</small>}
    </article>
  );
}

export function ReportLibrary({ reports, availableFormats }: ReportLibraryProps) {
  const firstGenerated = reports.find((report) => report.files.length > 0)?.id ?? reports[0]?.id ?? "";
  const [selectedId, setSelectedId] = useState(firstGenerated);
  const [format, setFormat] = useState("all");
  const filtered = useMemo(() => reports.filter((report) => format === "all" || report.files.some((file) => file.format === format)), [format, reports]);
  const selected = reports.find((report) => report.id === selectedId) ?? filtered[0] ?? reports[0];
  const generatedCount = reports.filter((report) => report.files.some((file) => file.status === "generated")).length;
  const formats = new Set(availableFormats);
  return (
    <section className="release-panel release-reports" aria-labelledby="release-reports-title">
      <header className="release-section-heading">
        <div className="release-heading-icon purple"><FileCheck2 aria-hidden="true" /></div>
        <div><span>DOCUMENTATION QUARTER</span><h2 id="release-reports-title">Report library</h2><p>{generatedCount} of {reports.length} required report types have generated API records.</p></div>
        <label className="release-report-filter"><Filter aria-hidden="true" /><span className="release-sr-only">Filter by format</span><select value={format} onChange={(event) => setFormat(event.target.value)}><option value="all">All formats</option>{REQUIRED_FORMATS.map((item) => <option key={item} value={item}>{item.toUpperCase()}</option>)}</select></label>
      </header>

      <div className="release-format-coverage" aria-label="Required report format coverage">
        {REQUIRED_FORMATS.map((item) => <span key={item} className={formats.has(item) ? "available" : "missing"}>{formats.has(item) ? <Check aria-hidden="true" /> : <X aria-hidden="true" />}{item.toUpperCase()}<small>{formats.has(item) ? "API record" : "not generated"}</small></span>)}
      </div>

      <div className="release-report-layout">
        <nav aria-label="Required report catalog">
          {filtered.map((report) => {
            const generated = report.files.filter((file) => file.status === "generated").length;
            return <button type="button" key={report.id} className={selected?.id === report.id ? "selected" : ""} onClick={() => setSelectedId(report.id)}><span>{generated ? <Check aria-hidden="true" /> : <Info aria-hidden="true" />}</span><div><b>{report.title}</b><small>{generated ? `${generated} generated format${generated === 1 ? "" : "s"}` : "Not generated"}</small></div></button>;
          })}
          {filtered.length === 0 && <p>No report records match this format.</p>}
        </nav>

        <div className="release-report-detail">
          {selected && <>
            <header><span>REQUIRED REPORT</span><h3>{selected.title}</h3><p>{selected.description}</p></header>
            {selected.files.length ? <div className="release-report-files">{selected.files.map((file) => <ReportFileCard key={file.id} file={file} />)}</div> : <div className="release-report-empty"><FileText aria-hidden="true" /><h4>Not generated</h4><p>No matching report record was returned by the API. The catalog entry remains visible so absence cannot look like completion.</p></div>}
            {selected.files[0] && <section className="release-provenance"><h4>Visible provenance and disclaimer coverage</h4><p>These values come from the project record and report API summary. “Not exposed” does not assert that the downloaded document lacks the field.</p><dl>{selected.files[0].provenance.map((item) => <div key={item.label} className={item.available ? "available" : "unavailable"}><dt>{item.available ? <Check aria-hidden="true" /> : <Info aria-hidden="true" />}{item.label}</dt><dd>{item.value}</dd></div>)}</dl></section>}
          </>}
        </div>
      </div>
    </section>
  );
}
