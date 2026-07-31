import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
  FilePlus2,
  HardDrive,
  Info,
  Save,
  ShieldCheck,
  Trash2,
  UploadCloud
} from "lucide-react";
import { useEffect, useMemo, useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";
import {
  DOCUMENT_ROLES,
  INTAKE_STEPS,
  RESTRICTED_DOMAINS,
  clearIntakeDraft,
  createEmptyIntakeDraft,
  issuesForStep,
  loadIntakeDraft,
  saveIntakeDraft,
  stageIntakeFile,
  validateIntakeDraft
} from "./intake";
import type {
  IntakeDraft,
  IntakeIdentity,
  IntakeStandards,
  IntakeSubmission,
  IntakeTargetProfile,
  WorkspaceCapability,
  WorkspaceMode
} from "./types";

export interface ProjectIntakeProps {
  mode: WorkspaceMode;
  capability: WorkspaceCapability;
  providerNotice: string;
  onSubmit?: (submission: IntakeSubmission) => Promise<{ projectId: string }>;
}

const PROJECT_TYPES = [
  "Controls upgrade",
  "New automation system",
  "PLC migration",
  "HMI / alarm modernization",
  "Engineering review",
  "FAT / commissioning package"
];

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function fieldError(draft: IntakeDraft, field: string): string | null {
  return validateIntakeDraft(draft).find((issue) => issue.field === field && issue.severity === "error")?.message ?? null;
}

export function ProjectIntake({ mode, capability, providerNotice, onSubmit }: ProjectIntakeProps) {
  const [draft, setDraft] = useState<IntakeDraft>(() => {
    if (typeof window === "undefined") return createEmptyIntakeDraft();
    return loadIntakeDraft(window.localStorage);
  });
  const [step, setStep] = useState(() => Math.min(draft.lastCompletedStep + 1, 5));
  const [files, setFiles] = useState<Map<string, File>>(() => new Map());
  const [submitState, setSubmitState] = useState<"idle" | "submitting" | "success" | "error">("idle");
  const [submitMessage, setSubmitMessage] = useState("");
  const issues = useMemo(() => validateIntakeDraft(draft), [draft]);
  const currentIssues = issuesForStep(issues, step);
  const errors = issues.filter((issue) => issue.severity === "error");

  useEffect(() => {
    const timer = window.setTimeout(() => saveIntakeDraft(window.localStorage, draft), 150);
    return () => window.clearTimeout(timer);
  }, [draft]);

  const updateIdentity = <Key extends keyof IntakeIdentity>(key: Key, value: IntakeIdentity[Key]) => {
    setDraft((current) => ({ ...current, identity: { ...current.identity, [key]: value } }));
  };
  const updateTarget = <Key extends keyof IntakeTargetProfile>(key: Key, value: IntakeTargetProfile[Key]) => {
    setDraft((current) => ({ ...current, target: { ...current.target, [key]: value } }));
  };
  const updateStandards = <Key extends keyof IntakeStandards>(key: Key, value: IntakeStandards[Key]) => {
    setDraft((current) => ({ ...current, standards: { ...current.standards, [key]: value } }));
  };

  const goToStep = (nextStep: number) => {
    const bounded = Math.max(1, Math.min(5, nextStep));
    setDraft((current) => ({
      ...current,
      lastCompletedStep: bounded > step ? Math.max(current.lastCompletedStep, step) : current.lastCompletedStep
    }));
    setStep(bounded);
  };

  const addFiles = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files ?? []);
    if (selected.length === 0) return;
    setFiles((current) => {
      const next = new Map(current);
      selected.forEach((file) => {
        const staged = stageIntakeFile(file);
        next.set(staged.id, file);
      });
      return next;
    });
    setDraft((current) => {
      const next = new Map(current.files.map((file) => [file.id, file]));
      selected.forEach((file) => {
        const staged = stageIntakeFile(file);
        next.set(staged.id, staged);
      });
      return { ...current, files: [...next.values()] };
    });
    event.target.value = "";
  };

  const removeFile = (id: string) => {
    setFiles((current) => {
      const next = new Map(current);
      next.delete(id);
      return next;
    });
    setDraft((current) => ({ ...current, files: current.files.filter((file) => file.id !== id) }));
  };

  const updateFileRole = (id: string, documentRole: string) => {
    setDraft((current) => ({
      ...current,
      files: current.files.map((file) => file.id === id ? { ...file, documentRole } : file)
    }));
  };

  const toggleRestrictedDomain = (domain: string) => {
    setDraft((current) => {
      const selected = current.target.restrictedDomains.includes(domain);
      return {
        ...current,
        target: {
          ...current.target,
          restrictedDomains: selected
            ? current.target.restrictedDomains.filter((item) => item !== domain)
            : [...current.target.restrictedDomains, domain]
        }
      };
    });
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (errors.length > 0 || !onSubmit) return;
    const selectedFiles = draft.files.flatMap((staged) => {
      const file = files.get(staged.id);
      return file ? [{ file, documentRole: staged.documentRole }] : [];
    });
    setSubmitState("submitting");
    setSubmitMessage("");
    try {
      const result = await onSubmit({ draft, files: selectedFiles });
      setSubmitState("success");
      setSubmitMessage(
        mode === "demo"
          ? `Synthetic intake ${result.projectId} was created in this demo session only.`
          : `Project ${result.projectId} was created by the local backend.`
      );
    } catch (error) {
      setSubmitState("error");
      setSubmitMessage(error instanceof Error ? error.message : "The local intake request failed.");
    }
  };

  const clearDraft = () => {
    clearIntakeDraft(window.localStorage);
    setDraft(createEmptyIntakeDraft());
    setFiles(new Map());
    setStep(1);
    setSubmitState("idle");
    setSubmitMessage("");
  };

  return (
    <form className="eng-intake" onSubmit={submit} aria-labelledby="intake-title">
      <header className="eng-workspace-header">
        <div>
          <span className="eng-eyebrow">PROJECT INTAKE</span>
          <h2 id="intake-title">Guided engineering intake</h2>
          <p>Drafts are saved locally on this workstation. File contents must be reselected after a browser restart.</p>
        </div>
        <div className="eng-draft-state"><Save aria-hidden="true" /> Draft autosaved</div>
      </header>

      <ol className="eng-stepper" aria-label="Intake progress">
        {INTAKE_STEPS.map((label, index) => {
          const number = index + 1;
          return (
            <li key={label} className={number === step ? "active" : number < step ? "complete" : ""}>
              <button type="button" onClick={() => goToStep(number)} aria-current={number === step ? "step" : undefined}>
                <span>{number < step ? <Check aria-hidden="true" /> : number}</span>
                <b>{label}</b>
              </button>
            </li>
          );
        })}
      </ol>

      <div className="eng-intake-stage">
        {step === 1 && (
          <section aria-labelledby="identity-heading">
            <div className="eng-section-heading">
              <span>01</span><div><h3 id="identity-heading">Project identity</h3><p>Use the original customer or internal wording. Do not summarize away constraints.</p></div>
            </div>
            <div className="eng-form-grid">
              <label className="wide">Project name
                <input value={draft.identity.name} onChange={(event) => updateIdentity("name", event.target.value)} aria-invalid={Boolean(fieldError(draft, "identity.name"))} />
              </label>
              <label>Customer / internal label
                <input value={draft.identity.customerLabel} onChange={(event) => updateIdentity("customerLabel", event.target.value)} />
              </label>
              <label>Plant / site
                <input value={draft.identity.plantSite} onChange={(event) => updateIdentity("plantSite", event.target.value)} />
              </label>
              <label>Project type
                <select value={draft.identity.projectType} onChange={(event) => updateIdentity("projectType", event.target.value)}>
                  <option value="">Select project type…</option>
                  {PROJECT_TYPES.map((type) => <option key={type}>{type}</option>)}
                </select>
              </label>
              <label className="wide">Original project description
                <textarea rows={7} value={draft.identity.description} onChange={(event) => updateIdentity("description", event.target.value)} placeholder="Paste the original scope, objectives, constraints, and known acceptance conditions…" />
              </label>
            </div>
          </section>
        )}

        {step === 2 && (
          <section aria-labelledby="target-heading">
            <div className="eng-section-heading">
              <span>02</span><div><h3 id="target-heading">Target Rockwell profile</h3><p>Exact target versions determine what can be generated and what must remain unsupported.</p></div>
            </div>
            <div className="eng-form-grid three">
              <label>Controller family<input value={draft.target.controllerFamily} onChange={(event) => updateTarget("controllerFamily", event.target.value)} placeholder="CompactLogix / ControlLogix" /></label>
              <label>Catalog number<input value={draft.target.catalogNumber} onChange={(event) => updateTarget("catalogNumber", event.target.value)} /></label>
              <label>Firmware<input value={draft.target.firmware} onChange={(event) => updateTarget("firmware", event.target.value)} /></label>
              <label>Studio 5000 version<input value={draft.target.studio5000Version} onChange={(event) => updateTarget("studio5000Version", event.target.value)} /></label>
              <label>HMI product<input value={draft.target.hmiProduct} onChange={(event) => updateTarget("hmiProduct", event.target.value)} placeholder="Leave blank when out of scope" /></label>
              <label>HMI version<input value={draft.target.hmiVersion} onChange={(event) => updateTarget("hmiVersion", event.target.value)} /></label>
              <label>Terminal / model<input value={draft.target.terminalModel} onChange={(event) => updateTarget("terminalModel", event.target.value)} /></label>
              <label>Resolution<input value={draft.target.resolution} onChange={(event) => updateTarget("resolution", event.target.value)} placeholder="e.g. 1280 × 800" /></label>
              <label>Release mode
                <select value={draft.target.releaseMode} onChange={(event) => updateTarget("releaseMode", event.target.value)}>
                  <option value="review_candidate">Review candidate</option>
                  <option value="import_ready_candidate">Import-ready candidate</option>
                  <option value="documentation_only">Documentation only</option>
                </select>
              </label>
            </div>
            <fieldset className="eng-check-grid">
              <legend>Restricted engineering domains in scope</legend>
              <p>Select every applicable domain. Selection adds human gates; it never grants automated authority.</p>
              {RESTRICTED_DOMAINS.map((domain) => (
                <label key={domain.value}>
                  <input type="checkbox" checked={draft.target.restrictedDomains.includes(domain.value)} onChange={() => toggleRestrictedDomain(domain.value)} />
                  <span>{domain.label}</span>
                </label>
              ))}
            </fieldset>
            {capability.level !== "available" && <CapabilityNotice text={capability.reason} />}
          </section>
        )}

        {step === 3 && (
          <section aria-labelledby="standards-heading">
            <div className="eng-section-heading">
              <span>03</span><div><h3 id="standards-heading">Standards & templates</h3><p>Name approved identifiers and versions. A blank library is treated as missing input, not permission to invent one.</p></div>
            </div>
            <div className="eng-form-grid">
              <label>ESCO naming standard<input value={draft.standards.namingStandard} onChange={(event) => updateStandards("namingStandard", event.target.value)} /></label>
              <label>Alarm philosophy<input value={draft.standards.alarmPhilosophy} onChange={(event) => updateStandards("alarmPhilosophy", event.target.value)} /></label>
              <label>Controller template<input value={draft.standards.controllerTemplate} onChange={(event) => updateStandards("controllerTemplate", event.target.value)} /></label>
              <label>AOI / UDT library<input value={draft.standards.aoiUdtLibrary} onChange={(event) => updateStandards("aoiUdtLibrary", event.target.value)} /></label>
              <label>HMI / global object library<input value={draft.standards.hmiLibrary} onChange={(event) => updateStandards("hmiLibrary", event.target.value)} /></label>
              <label>Test template set<input value={draft.standards.testTemplateSet} onChange={(event) => updateStandards("testTemplateSet", event.target.value)} /></label>
            </div>
          </section>
        )}

        {step === 4 && (
          <section aria-labelledby="files-heading">
            <div className="eng-section-heading">
              <span>04</span><div><h3 id="files-heading">Engineering files</h3><p>Staging only checks the local format. The backend remains authoritative for parser support and file safety.</p></div>
            </div>
            <label className="eng-dropzone">
              <UploadCloud aria-hidden="true" />
              <b>Select local engineering files</b>
              <span>L5X, XLSX, CSV, DOCX, PDF, XML, ZIP, or TXT</span>
              <input type="file" multiple accept=".l5x,.xlsx,.csv,.docx,.pdf,.xml,.zip,.txt" onChange={addFiles} />
            </label>
            <div className="eng-file-stage" aria-live="polite">
              {draft.files.length === 0 && <EmptyInline icon={<FilePlus2 />} text="No files staged. The original description remains saved in the draft." />}
              {draft.files.map((file) => (
                <article key={file.id} className={file.validation === "ready" ? "ready" : "attention"}>
                  <HardDrive aria-hidden="true" />
                  <div><b>{file.name}</b><span>{formatBytes(file.sizeBytes)} · {file.validationMessage}</span></div>
                  <label>Document role
                    <select value={file.documentRole} onChange={(event) => updateFileRole(file.id, event.target.value)}>
                      {DOCUMENT_ROLES.map((role) => <option value={role.value} key={role.value}>{role.label}</option>)}
                    </select>
                  </label>
                  <button type="button" onClick={() => removeFile(file.id)} aria-label={`Remove ${file.name}`}><Trash2 aria-hidden="true" /></button>
                </article>
              ))}
            </div>
          </section>
        )}

        {step === 5 && (
          <section aria-labelledby="review-heading">
            <div className="eng-section-heading">
              <span>05</span><div><h3 id="review-heading">Review & confirmation</h3><p>Resolve required inputs before the local workflow can begin.</p></div>
            </div>
            <div className="eng-review-grid">
              <article><span>Project</span><b>{draft.identity.name || "Missing"}</b><p>{draft.identity.customerLabel || "No label"} · {draft.identity.plantSite || "No site"}</p></article>
              <article><span>Target</span><b>{draft.target.controllerFamily || "Missing"}</b><p>{draft.target.catalogNumber || "No catalog"} · Firmware {draft.target.firmware || "missing"} · Studio {draft.target.studio5000Version || "missing"}</p></article>
              <article><span>Sources</span><b>{draft.files.length} staged file{draft.files.length === 1 ? "" : "s"}</b><p>{draft.files.filter((file) => file.validation === "ready").length} ready for authoritative backend validation</p></article>
              <article><span>Restricted scope</span><b>{draft.target.restrictedDomains.length || "None declared"}</b><p>{draft.target.restrictedDomains.join(", ").replaceAll("_", " ") || "Verify this selection before analysis."}</p></article>
            </div>
            <div className="eng-provider-notice"><Info aria-hidden="true" /><div><b>Provider boundary</b><p>{providerNotice}</p></div></div>
            <label className="eng-confirmation">
              <input type="checkbox" checked={draft.safetyAcknowledged} onChange={(event) => setDraft((current) => ({ ...current, safetyAcknowledged: event.target.checked }))} />
              <ShieldCheck aria-hidden="true" />
              <span><b>Engineering responsibility acknowledged</b>AI output is advisory. Safety-related, equipment-damage-related, restricted-domain, and final release decisions require an authorized human reviewer.</span>
            </label>
            <label className="eng-confirmation">
              <input type="checkbox" checked={draft.analysisConfirmed} onChange={(event) => setDraft((current) => ({ ...current, analysisConfirmed: event.target.checked }))} />
              <Check aria-hidden="true" />
              <span><b>Inputs reviewed</b>I confirm this intake reflects the available source information and unresolved omissions are visible above.</span>
            </label>
            {issues.length > 0 && (
              <div className="eng-issue-summary" role="alert">
                <h4><AlertTriangle aria-hidden="true" /> {errors.length} required item{errors.length === 1 ? "" : "s"} remain</h4>
                <ul>{issues.map((issue) => <li key={`${issue.field}-${issue.message}`} className={issue.severity}>{issue.message}</li>)}</ul>
              </div>
            )}
          </section>
        )}

        {currentIssues.length > 0 && step < 5 && (
          <div className="eng-stage-issues" aria-live="polite">
            {currentIssues.map((issue) => <p key={`${issue.field}-${issue.message}`} className={issue.severity}><AlertTriangle aria-hidden="true" />{issue.message}</p>)}
          </div>
        )}
      </div>

      <footer className="eng-intake-footer">
        <button type="button" className="quiet danger" onClick={clearDraft}>Clear local draft</button>
        <div>
          <button type="button" className="quiet" disabled={step === 1} onClick={() => goToStep(step - 1)}><ChevronLeft aria-hidden="true" /> Back</button>
          {step < 5 ? (
            <button type="button" className="primary" onClick={() => goToStep(step + 1)}>Continue <ChevronRight aria-hidden="true" /></button>
          ) : (
            <button
              type="submit"
              className="primary"
              disabled={errors.length > 0 || !onSubmit || submitState === "submitting"}
              title={!onSubmit ? capability.reason : errors.length > 0 ? "Resolve the required intake items first." : undefined}
            >
              {submitState === "submitting" ? "Submitting to local workflow…" : mode === "demo" ? "Create synthetic demo intake" : "Create project & stage files"}
            </button>
          )}
        </div>
      </footer>
      {!onSubmit && step === 5 && <p className="eng-disabled-reason">Submission is disabled: {capability.reason}</p>}
      {submitMessage && <p className={`eng-submit-message ${submitState}`} role="status">{submitMessage}</p>}
    </form>
  );
}

function CapabilityNotice({ text }: { text: string }) {
  return <div className="eng-capability-notice"><Info aria-hidden="true" /><p>{text}</p></div>;
}

function EmptyInline({ icon, text }: { icon: ReactNode; text: string }) {
  return <div className="eng-inline-empty">{icon}<span>{text}</span></div>;
}
