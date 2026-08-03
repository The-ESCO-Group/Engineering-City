import {
  BookOpenCheck,
  Braces,
  ClipboardCheck,
  Code2,
  Copy,
  FileCode2,
  FileSpreadsheet,
  FileText,
  Gavel,
  Link2,
  SearchX,
  ShieldCheck
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import type { EvidenceSourceKind, WorkspaceEvidence } from "./types";

export interface EvidenceViewerProps {
  evidence: WorkspaceEvidence[];
  selectedId?: string | null;
  initialSelectedId?: string | null;
  onSelect?: (evidenceId: string) => void;
  deepLinkFor?: (evidenceId: string) => string;
  title?: string;
}

const KIND_LABELS: Record<EvidenceSourceKind, string> = {
  l5x: "L5X object",
  xlsx: "Workbook range",
  csv: "CSV row / column",
  docx: "Document location",
  pdf: "PDF page",
  xml: "XML path",
  requirement: "Requirement",
  template_rule: "Template rule",
  human_decision: "Human decision",
  generated_object: "Generated object",
  validation_output: "Validation output",
  unknown: "Evidence"
};

function EvidenceIcon({ kind }: { kind: EvidenceSourceKind }) {
  const props = { "aria-hidden": true } as const;
  if (kind === "l5x") return <Code2 {...props} />;
  if (kind === "xlsx" || kind === "csv") return <FileSpreadsheet {...props} />;
  if (kind === "xml") return <FileCode2 {...props} />;
  if (kind === "requirement") return <BookOpenCheck {...props} />;
  if (kind === "template_rule") return <Braces {...props} />;
  if (kind === "human_decision") return <Gavel {...props} />;
  if (kind === "generated_object") return <ClipboardCheck {...props} />;
  if (kind === "validation_output") return <ShieldCheck {...props} />;
  return <FileText {...props} />;
}

export function EvidenceViewer({ evidence, selectedId, initialSelectedId, onSelect, deepLinkFor, title = "Evidence citations" }: EvidenceViewerProps) {
  const headingId = useId();
  const initialMatch = Boolean(initialSelectedId && evidence.some((item) => item.id === initialSelectedId));
  const [localSelection, setLocalSelection] = useState<string | null>(() => initialMatch ? initialSelectedId! : initialSelectedId ? null : evidence[0]?.id ?? null);
  const [copyState, setCopyState] = useState("");
  const appliedRouteId = useRef<string | null>(initialMatch ? initialSelectedId! : null);
  const lastRouteId = useRef<string | null | undefined>(initialSelectedId);
  const activeId = selectedId === undefined ? localSelection : selectedId;
  const requestedId = selectedId === undefined ? initialSelectedId : selectedId;
  const requestedMissing = Boolean(requestedId && !evidence.some((item) => item.id === requestedId));
  const selected = useMemo(
    () => requestedMissing ? null : evidence.find((item) => item.id === activeId) ?? evidence[0] ?? null,
    [activeId, evidence, requestedMissing]
  );

  useEffect(() => {
    if (lastRouteId.current !== initialSelectedId) {
      lastRouteId.current = initialSelectedId;
      appliedRouteId.current = null;
    }
    if (selectedId !== undefined || !initialSelectedId || appliedRouteId.current === initialSelectedId) return;
    if (!evidence.some((item) => item.id === initialSelectedId)) return;
    setLocalSelection(initialSelectedId);
    appliedRouteId.current = initialSelectedId;
    onSelect?.(initialSelectedId);
  }, [evidence, initialSelectedId, onSelect, selectedId]);

  const select = (id: string) => {
    if (selectedId === undefined) setLocalSelection(id);
    onSelect?.(id);
  };

  const copyCitation = async () => {
    if (!selected) return;
    const citation = `${selected.sourceLabel} · ${selected.exactLocation}`;
    try {
      await navigator.clipboard.writeText(citation);
      setCopyState("Citation copied");
    } catch {
      setCopyState("Clipboard access is unavailable");
    }
  };

  if (evidence.length === 0) {
    return (
      <section className="eng-evidence-viewer empty" aria-labelledby={headingId}>
        <h3 id={headingId}>{title}</h3>
        <SearchX aria-hidden="true" />
        <b>No evidence citation is available</b>
        <p>The interface does not infer a source or location when the backend has not published one.</p>
      </section>
    );
  }

  if (requestedMissing) {
    return (
      <section className="eng-evidence-viewer empty" aria-labelledby={headingId} role="alert">
        <h3 id={headingId}>{title}</h3>
        <SearchX aria-hidden="true" />
        <b>Evidence link not found</b>
        <p>No citation with ID <code>{requestedId}</code> exists for this record. No substitute citation is shown.</p>
      </section>
    );
  }

  return (
    <section className="eng-evidence-viewer" aria-labelledby={headingId}>
      <header>
        <div><span className="eng-eyebrow">TRACEABLE SOURCE</span><h3 id={headingId}>{title}</h3></div>
        <span>{evidence.length} citation{evidence.length === 1 ? "" : "s"}</span>
      </header>
      <div className="eng-evidence-layout">
        <div className="eng-evidence-list" role="list" aria-label="Evidence citations">
          {evidence.map((item) => (
            <div role="listitem" key={item.id}>
              <button
                type="button"
                className={selected?.id === item.id ? "selected" : ""}
                onClick={() => select(item.id)}
                aria-pressed={selected?.id === item.id}
              >
                <EvidenceIcon kind={item.kind} />
                <span><b>{item.sourceLabel}</b><small>{KIND_LABELS[item.kind]} · {item.role}</small></span>
              </button>
            </div>
          ))}
        </div>
        {selected && (
          <article className="eng-evidence-detail" id={`evidence-${selected.id}`}>
            <div className="eng-source-line">
              <span><EvidenceIcon kind={selected.kind} />{KIND_LABELS[selected.kind]}</span>
              <span className={`eng-evidence-role ${selected.evidenceKind}`}>{selected.evidenceKind.replaceAll("_", " ")}</span>
            </div>
            <h4>{selected.sourceLabel}</h4>
            <div className="eng-exact-location"><Link2 aria-hidden="true" /><code>{selected.exactLocation}</code></div>
            <EvidenceBlock label="Cited excerpt"><pre>{selected.excerpt}</pre></EvidenceBlock>
            {selected.expectation && <EvidenceBlock label="Expected evidence"><p>{selected.expectation}</p></EvidenceBlock>}
            {selected.note && <EvidenceBlock label="Citation note"><p>{selected.note}</p></EvidenceBlock>}
            <div className="eng-evidence-actions">
              <button type="button" onClick={() => void copyCitation()}><Copy aria-hidden="true" /> Copy citation</button>
              {deepLinkFor && <a href={deepLinkFor(selected.id)}><Link2 aria-hidden="true" /> Evidence deep link</a>}
              <span role="status">{copyState}</span>
            </div>
          </article>
        )}
      </div>
    </section>
  );
}

function EvidenceBlock({ label, children }: { label: string; children: ReactNode }) {
  return <div className="eng-evidence-block"><span>{label}</span>{children}</div>;
}
