import { Archive, Bot, Database, Radio } from "lucide-react";
import { useMemo } from "react";
import { buildOperationsModel } from "./adapters";
import { AgentsSurface } from "./AgentsSurface";
import { AuditTrailSurface } from "./AuditTrailSurface";
import type { OperationsSource, OperationsView } from "./types";
import "./OperationsCenter.css";

export interface OperationsCenterProps {
  source: OperationsSource;
  view: OperationsView;
  onOpenCityLocation?: (semanticLocation: string) => void;
}

function modeLabel(mode: OperationsSource["dataMode"]): string {
  if (mode === "demo") return "DEMO DATA";
  if (mode === "live") return "LIVE LOCAL DATA";
  return "DATA UNAVAILABLE";
}

export function OperationsCenter({ source, view, onOpenCityLocation }: OperationsCenterProps) {
  const model = useMemo(() => buildOperationsModel(source), [source]);
  const title = view === "agents" ? "Agents" : "Audit Trail";
  const Icon = view === "agents" ? Bot : Archive;

  return (
    <div className="ops-center">
      <header className="ops-center__topbar">
        <div className="ops-center__identity"><span><Icon aria-hidden="true" /></span><div><small>ESCO OPERATIONS</small><h1>{title}</h1></div></div>
        <div className="ops-center__project"><small>AUTHORIZED PROJECT CONTEXT</small><strong>{model.project?.client_reference || "NO PROJECT"} · {model.project?.name || "Project data unavailable"}</strong><span>{model.project ? `${model.project.status.replaceAll("_", " ")} · ${model.project.event_seq} published project sequence` : model.unavailableReason}</span></div>
        <span className={`ops-center__mode is-${model.mode}`}><Radio aria-hidden="true" />{modeLabel(model.mode)}</span>
      </header>

      {model.mode === "demo" && <div className="ops-demo-banner" role="status"><Database aria-hidden="true" /><strong>DEMO DATA · SYNTHETIC OFFLINE</strong><span>Agent snapshots and event envelopes are synthetic, isolated from customer projects, and provide no operational control.</span></div>}
      {model.mode === "unavailable" && <div className="ops-unavailable" role="alert"><Archive aria-hidden="true" /><div><strong>Authorized local records are unavailable.</strong><p>{model.unavailableReason}</p></div></div>}

      <div className="ops-center__content">
        {view === "agents" ? (
          <AgentsSurface agents={model.agents} onOpenCityLocation={onOpenCityLocation} />
        ) : (
          <AuditTrailSurface events={model.audit} firstSequence={model.firstSequence} latestSequence={model.latestSequence} sequenceGapCount={model.sequenceGapCount} />
        )}
      </div>
    </div>
  );
}
