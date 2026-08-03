import {
  BookOpenCheck,
  BellRing,
  BookOpen,
  Cable,
  ClipboardCheck,
  FileInput,
  Files,
  FlaskConical,
  GitCompareArrows,
  GitPullRequestArrow,
  ListChecks,
  ListTree,
  Map,
  Monitor,
  Network,
  ShieldAlert,
  Tags
} from "lucide-react";
import { useEffect, useMemo, useState, type ComponentType } from "react";
import { buildWorkspaceModel } from "./adapters";
import { ApprovalInbox } from "./ApprovalInbox";
import { ChangeReviewWorkspace } from "./ChangeReviewWorkspace";
import { DeliveryCoreWorkspace } from "./DeliveryCoreWorkspace";
import { FilesWorkspace } from "./FilesWorkspace";
import { FindingsWorkspace } from "./FindingsWorkspace";
import { ProjectCoreExplorer } from "./ProjectCoreExplorer";
import { ProjectIntake } from "./ProjectIntake";
import { TaskWorkspace } from "./TaskWorkspace";
import type {
  WorkspaceApprovalDecision,
  WorkspaceOperations,
  WorkspaceSource,
  WorkspaceView
} from "./types";
import "./EngineeringWorkspace.css";

export interface EngineeringWorkspaceProps {
  source: WorkspaceSource;
  initialView?: WorkspaceView;
  initialRecordId?: string | null;
  initialEvidenceId?: string | null;
  operations?: WorkspaceOperations;
  onReturnToCity?: () => void;
}

interface NavigationItem {
  id: WorkspaceView;
  label: string;
  icon: ComponentType<{ "aria-hidden"?: boolean }>;
}

const NAVIGATION: Array<{ label: string; items: NavigationItem[] }> = [
  {
    label: "Project",
    items: [
      { id: "intake", label: "Project intake", icon: FileInput },
      { id: "files", label: "Files", icon: Files },
      { id: "tasks", label: "Tasks", icon: ListChecks }
    ]
  },
  {
    label: "Project Core",
    items: [
      { id: "requirements", label: "Requirements", icon: BookOpenCheck },
      { id: "devices", label: "Devices & I/O", icon: Cable },
      { id: "tags", label: "Tags", icon: Tags },
      { id: "plc", label: "PLC logic", icon: ListTree },
      { id: "hmi", label: "HMI", icon: Monitor },
      { id: "alarms", label: "Alarms", icon: BellRing },
      { id: "tests", label: "Tests", icon: FlaskConical },
      { id: "documentation", label: "Documentation", icon: BookOpen }
    ]
  },
  {
    label: "Review",
    items: [
      { id: "planning", label: "Planning", icon: Network },
      { id: "changesets", label: "Change sets", icon: GitPullRequestArrow },
      { id: "revisions", label: "Revisions", icon: GitCompareArrows },
      { id: "findings", label: "Findings & evidence", icon: ShieldAlert },
      { id: "approvals", label: "Approvals", icon: ClipboardCheck }
    ]
  }
];

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function decisionStatus(decision: WorkspaceApprovalDecision): string {
  if (decision === "approve") return "approved";
  if (decision === "accept_risk") return "risk_accepted";
  if (decision === "request_changes") return "changes_requested";
  if (decision === "defer") return "deferred";
  return "rejected";
}

export function EngineeringWorkspace({ source, initialView = "requirements", initialRecordId, initialEvidenceId, operations, onReturnToCity }: EngineeringWorkspaceProps) {
  const [view, setView] = useState<WorkspaceView>(initialView);
  useEffect(() => setView(initialView), [initialView]);
  const sourceModel = useMemo(() => buildWorkspaceModel(source), [source]);
  const [demoModel, setDemoModel] = useState(() => sourceModel);
  useEffect(() => {
    if (source.dataMode === "demo") setDemoModel(sourceModel);
  }, [source.dataMode, sourceModel]);
  const model = source.dataMode === "demo" ? demoModel : sourceModel;

  const effectiveOperations: WorkspaceOperations = { ...operations };
  if (model.mode === "demo") {
    effectiveOperations.allowPartialChangeSetReview = operations?.allowPartialChangeSetReview ?? true;
    effectiveOperations.submitIntake = operations?.submitIntake ?? (async () => ({ projectId: `DEMO-DRAFT-${Date.now()}` }));
    effectiveOperations.decideApproval = operations?.decideApproval ?? (async (approval, decision, comment) => {
      setDemoModel((current) => ({
        ...current,
        approvals: current.approvals.map((item) => item.id === approval.id ? {
          ...item,
          status: decisionStatus(decision),
          comments: comment ? [...item.comments, comment] : item.comments,
          priorDecisions: [...item.priorDecisions, `${humanize(decision)} · synthetic demo session`],
          allowedDecisions: []
        } : item)
      }));
    });
    effectiveOperations.reviewChangeSet = operations?.reviewChangeSet ?? (async (changeset, decision, comment) => {
      setDemoModel((current) => ({
        ...current,
        changesets: current.changesets.map((item) => item.id === changeset.id ? {
          ...item,
          status: decision === "approve" ? "approved" : decision === "reject" ? "rejected" : "changes_requested",
          concerns: comment ? [...item.concerns, comment] : item.concerns
        } : item)
      }));
    });
  }

  const activeLabel = NAVIGATION.flatMap((group) => group.items).find((item) => item.id === view)?.label ?? humanize(view);
  const pendingApprovals = model.approvals.filter((approval) => approval.status === "pending").length;
  const openFindings = model.findings.filter((finding) => finding.status === "open").length;

  return (
    <div className="eng-workspace">
      <header className="eng-workspace-topbar">
        <div className="eng-workspace-brand"><span>E</span><div><b>ESCO</b><small>ENGINEERING WORKSPACE</small></div></div>
        <div className="eng-project-context"><span>{model.project?.reference || "NO PROJECT"}</span><b>{model.project?.name || "Engineering data unavailable"}</b><small>{model.project ? `${humanize(model.project.status)} · ${model.project.revision === null ? "Revision not published" : `Revision ${model.project.revision}`}` : model.unavailableReason}</small></div>
        <div className="eng-workspace-summary"><span><b>{model.tasks.filter((task) => task.state === "running").length}</b> active tasks</span><span className={openFindings ? "attention" : ""}><b>{openFindings}</b> open findings</span><span className={pendingApprovals ? "attention" : ""}><b>{pendingApprovals}</b> approvals</span></div>
        <button type="button" className="eng-city-return" disabled={!onReturnToCity} onClick={onReturnToCity} title={!onReturnToCity ? "City navigation is not connected in this host." : undefined}><Map aria-hidden="true" /> Return to city</button>
      </header>
      {model.demoLabel && <div className="eng-demo-banner" role="status"><b>{model.demoLabel}</b><span>No customer data · no deployable artifacts · decisions affect this synthetic session only</span></div>}
      {model.mode === "unavailable" && <div className="eng-offline-banner"><ShieldAlert aria-hidden="true" /><b>Authorized project data is unavailable.</b><span>{model.unavailableReason}</span></div>}
      <div className="eng-workspace-frame">
        <aside className="eng-workspace-nav">
          <div><span className="eng-eyebrow">PROJECT WORKSPACE</span><h1>{activeLabel}</h1></div>
          {NAVIGATION.map((group) => (
            <nav key={group.label} aria-label={group.label}>
              <span>{group.label}</span>
              {group.items.map((item) => {
                const Icon = item.icon;
                const count = item.id === "tasks" ? model.tasks.length : item.id === "findings" ? openFindings : item.id === "approvals" ? pendingApprovals : null;
                return <button type="button" key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)} aria-current={view === item.id ? "page" : undefined}><Icon aria-hidden={true} /><span>{item.label}</span>{count !== null && <b>{count}</b>}</button>;
              })}
            </nav>
          ))}
          <p><ShieldAlert aria-hidden="true" />No direct PLC control, online editing, controller mode, forces, or live equipment actions exist in this workspace.</p>
        </aside>
        <div className="eng-workspace-main" id={`workspace-${view}`} tabIndex={-1} role="region" aria-label="Engineering workspace content">
          {view === "intake" && <ProjectIntake mode={model.mode} capability={model.capabilities.intakeSubmission} providerNotice={model.mode === "demo" ? "Synthetic offline provider. No internet or customer system is contacted." : model.mode === "live" ? "Authorized local backend only. The workspace does not silently fall back to cloud services." : model.unavailableReason} onSubmit={effectiveOperations.submitIntake} />}
          {view === "files" && <FilesWorkspace files={model.files} onOpenIntake={() => setView("intake")} />}
          {view === "tasks" && <TaskWorkspace tasks={model.tasks} onOpenCityLocation={effectiveOperations.openCityLocation} />}
          {(view === "requirements" || view === "devices" || view === "tags" || view === "plc") && <ProjectCoreExplorer view={view} initialRecordId={initialRecordId ?? undefined} capability={model.capabilities.projectCore} requirements={model.requirements} devices={model.devices} tags={model.tags} plcPrograms={model.plcPrograms} onExport={effectiveOperations.exportCoreView} />}
          {(view === "hmi" || view === "alarms" || view === "tests" || view === "documentation") && <DeliveryCoreWorkspace view={view} capability={model.capabilities.deliveryCore} hmiScreens={model.hmiScreens} alarms={model.alarms} tests={model.testProcedures} documents={model.documents} tasks={model.tasks} findings={model.findings} onOpenReleaseCenter={effectiveOperations.openReleaseCenter} />}
          {(view === "planning" || view === "changesets" || view === "revisions") && <ChangeReviewWorkspace view={view} plans={model.plans} changesets={model.changesets} revisions={model.revisions} changesetCapability={model.capabilities.changesetOperations} revisionCapability={model.capabilities.revisionComparison} operations={effectiveOperations} />}
          {view === "findings" && <FindingsWorkspace findings={model.findings} initialFindingId={initialRecordId ?? undefined} initialEvidenceId={initialEvidenceId ?? undefined} capability={model.capabilities.findingEvidence} loadFindingDetail={effectiveOperations.loadFindingDetail} onOpenChangeset={() => setView("changesets")} onOpenApprovals={() => setView("approvals")} />}
          {view === "approvals" && <ApprovalInbox approvals={model.approvals} capability={model.capabilities.approvalDecisions} onDecision={effectiveOperations.decideApproval} onOpenChangeset={() => setView("changesets")} />}
        </div>
      </div>
    </div>
  );
}
