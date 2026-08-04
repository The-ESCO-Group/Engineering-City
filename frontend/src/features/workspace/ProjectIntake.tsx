import {
  AlertTriangle,
  Cpu,
  FilePlus2,
  HardDrive,
  Info,
  Rocket,
  Save,
  Settings2,
  ShieldCheck,
  Trash2,
  UploadCloud
} from "lucide-react";
import { useEffect, useMemo, useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";
import {
  DOCUMENT_ROLES,
  RESTRICTED_DOMAINS,
  clearIntakeDraft,
  createEmptyIntakeDraft,
  loadIntakeDraft,
  prepareIntakeSubmissionDraft,
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
  { value: "new_installation", label: "New installation" },
  { value: "upgrade", label: "Controls upgrade" },
  { value: "retrofit", label: "Retrofit" },
  { value: "migration", label: "PLC migration" },
  { value: "expansion", label: "Expansion" },
  { value: "other", label: "Other / determine from sources" }
] as const;

const CONTROLLER_FAMILIES = [
  { value: "compactlogix", label: "CompactLogix" },
  { value: "controllogix", label: "ControlLogix" },
  { value: "guardlogix", label: "GuardLogix" },
  { value: "compact_guardlogix", label: "Compact GuardLogix" },
  { value: "micro800", label: "Micro800" },
  { value: "slc500", label: "SLC 500" },
  { value: "plc5", label: "PLC-5" },
  { value: "other", label: "Other" }
] as const;

const HMI_PRODUCTS = [
  { value: "none", label: "None / out of scope" },
  { value: "factorytalk_view_me", label: "FactoryTalk View ME" },
  { value: "factorytalk_view_se", label: "FactoryTalk View SE" },
  { value: "factorytalk_optix", label: "FactoryTalk Optix" },
  { value: "panelview_800", label: "PanelView 800" },
  { value: "other", label: "Other" }
] as const;

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

export function ProjectIntake({ mode, capability, providerNotice, onSubmit }: ProjectIntakeProps) {
  const [draft, setDraft] = useState<IntakeDraft>(() => {
    if (typeof window === "undefined") return createEmptyIntakeDraft();
    return loadIntakeDraft(window.localStorage);
  });
  const [files, setFiles] = useState<Map<string, File>>(() => new Map());
  const [submitState, setSubmitState] = useState<"idle" | "submitting" | "success" | "error">("idle");
  const [submitMessage, setSubmitMessage] = useState("");
  const issues = useMemo(() => validateIntakeDraft(draft), [draft]);
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
  const hasError = (field: string) => errors.some((issue) => issue.field === field);

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
    const submissionDraft = prepareIntakeSubmissionDraft(draft);
    if (validateIntakeDraft(submissionDraft).some((issue) => issue.severity === "error") || !onSubmit) return;
    const selectedFiles = submissionDraft.files.flatMap((staged) => {
      const file = files.get(staged.id);
      return file ? [{ file, documentRole: staged.documentRole }] : [];
    });
    setSubmitState("submitting");
    setSubmitMessage("");
    try {
      const result = await onSubmit({ draft: submissionDraft, files: selectedFiles });
      clearIntakeDraft(window.localStorage);
      setSubmitState("success");
      setSubmitMessage(
        mode === "demo"
          ? `Synthetic project ${result.projectId} was created and demo agents were started.`
          : `Project ${result.projectId} was created and engineering agents were started.`
      );
    } catch (error) {
      setSubmitState("error");
      setSubmitMessage(error instanceof Error ? error.message : "The local project launch failed.");
    }
  };

  const clearDraft = () => {
    clearIntakeDraft(window.localStorage);
    setDraft(createEmptyIntakeDraft());
    setFiles(new Map());
    setSubmitState("idle");
    setSubmitMessage("");
  };

  return (
    <form className="eng-intake" onSubmit={submit} aria-labelledby="intake-title">
      <header className="eng-workspace-header eng-quick-intake-header">
        <div>
          <span className="eng-eyebrow">QUICK PROJECT LAUNCH</span>
          <h2 id="intake-title">Start a project</h2>
          <p>Name the project, identify the controller, and add source files. The engineering agents start automatically.</p>
        </div>
        <div className="eng-draft-state"><Save aria-hidden="true" /> Draft autosaved</div>
      </header>

      <div className="eng-intake-stage eng-quick-intake">
        <div className="eng-quick-intake-grid">
          <section className="eng-quick-card" aria-labelledby="quick-project-heading">
            <div className="eng-quick-heading">
              <span>1</span>
              <div><h3 id="quick-project-heading">Project</h3><p>Only the name is required.</p></div>
            </div>
            <div className="eng-form-grid">
              <label className="wide">Project name
                <input
                  autoFocus
                  value={draft.identity.name}
                  onChange={(event) => updateIdentity("name", event.target.value)}
                  aria-invalid={hasError("identity.name")}
                  placeholder="e.g. Line 3 controls upgrade"
                />
              </label>
              <label><span>Customer / internal label <small>Optional</small></span>
                <input value={draft.identity.customerLabel} onChange={(event) => updateIdentity("customerLabel", event.target.value)} />
              </label>
              <label><span>Plant / site <small>Optional</small></span>
                <input value={draft.identity.plantSite} onChange={(event) => updateIdentity("plantSite", event.target.value)} placeholder="Can be resolved later" />
              </label>
              <label className="wide"><span>Project objective <small>Optional when the source files contain the scope</small></span>
                <textarea rows={3} value={draft.identity.description} onChange={(event) => updateIdentity("description", event.target.value)} placeholder="What should the engineering agents review, change, or deliver?" />
              </label>
            </div>
          </section>

          <section className="eng-quick-card" aria-labelledby="quick-target-heading">
            <div className="eng-quick-heading">
              <span>2</span>
              <div><h3 id="quick-target-heading">Target controller</h3><p>The minimum profile needed to start analysis.</p></div>
            </div>
            <div className="eng-form-grid">
              <label>Controller family
                <select value={draft.target.controllerFamily} onChange={(event) => updateTarget("controllerFamily", event.target.value)} aria-invalid={hasError("target.controllerFamily")}>
                  <option value="">Select family…</option>
                  {CONTROLLER_FAMILIES.map((family) => <option value={family.value} key={family.value}>{family.label}</option>)}
                </select>
              </label>
              <label>Catalog number
                <input value={draft.target.catalogNumber} onChange={(event) => updateTarget("catalogNumber", event.target.value)} aria-invalid={hasError("target.catalogNumber")} placeholder="e.g. 5069-L306ER" />
              </label>
              <label>Firmware
                <input value={draft.target.firmware} onChange={(event) => updateTarget("firmware", event.target.value)} aria-invalid={hasError("target.firmware")} placeholder="e.g. 35" />
              </label>
              <label>Studio 5000 version
                <input value={draft.target.studio5000Version} onChange={(event) => updateTarget("studio5000Version", event.target.value)} aria-invalid={hasError("target.studio5000Version")} placeholder="e.g. 35" />
              </label>
            </div>
            {capability.level !== "available" && <CapabilityNotice text={capability.reason} />}
          </section>
        </div>

        <section className="eng-quick-card eng-quick-files" aria-labelledby="quick-files-heading">
          <div className="eng-quick-heading">
            <span>3</span>
            <div><h3 id="quick-files-heading">Project source files</h3><p>Add the requirements and project package. File roles can be corrected after selection.</p></div>
          </div>
          <label className="eng-dropzone">
            <UploadCloud aria-hidden="true" />
            <b>Select or drop engineering files</b>
            <span>L5X, XLSX, CSV, DOCX, PDF, XML, ZIP, or TXT</span>
            <input type="file" multiple accept=".l5x,.xlsx,.csv,.docx,.pdf,.xml,.zip,.txt" onChange={addFiles} />
          </label>
          <div className="eng-file-stage" aria-live="polite">
            {draft.files.length === 0 && <EmptyInline icon={<FilePlus2 />} text="Add at least one requirements or project file." />}
            {draft.files.map((file) => (
              <article key={file.id} className={file.validation === "ready" ? "ready" : "attention"}>
                <HardDrive aria-hidden="true" />
                <div><b>{file.name}</b><span>{formatBytes(file.sizeBytes)} · {file.validationMessage}</span></div>
                <label>Role
                  <select value={file.documentRole} onChange={(event) => updateFileRole(file.id, event.target.value)}>
                    {DOCUMENT_ROLES.map((role) => <option value={role.value} key={role.value}>{role.label}</option>)}
                  </select>
                </label>
                <button type="button" onClick={() => removeFile(file.id)} aria-label={`Remove ${file.name}`}><Trash2 aria-hidden="true" /></button>
              </article>
            ))}
          </div>
        </section>

        <details className="eng-intake-advanced">
          <summary><Settings2 aria-hidden="true" /><span><b>Advanced project settings</b>Defaults are already applied; open only when this project needs overrides.</span></summary>
          <div className="eng-intake-advanced-body">
            <section>
              <h4>Project profile</h4>
              <div className="eng-form-grid three">
                <label>Project type
                  <select value={draft.identity.projectType} onChange={(event) => updateIdentity("projectType", event.target.value)}>
                    {PROJECT_TYPES.map((type) => <option value={type.value} key={type.value}>{type.label}</option>)}
                  </select>
                </label>
                <label>Unit system
                  <select value={draft.identity.units} onChange={(event) => updateIdentity("units", event.target.value)}>
                    <option value="metric">Metric</option><option value="imperial">Imperial</option><option value="mixed">Mixed</option>
                  </select>
                </label>
                <label>Locale<input value={draft.identity.locale} onChange={(event) => updateIdentity("locale", event.target.value)} /></label>
              </div>
            </section>
            <section>
              <h4>Delivery and HMI</h4>
              <div className="eng-form-grid three">
                <label>Safety classification
                  <select value={draft.target.safetyClassification} onChange={(event) => updateTarget("safetyClassification", event.target.value)}>
                    <option value="unknown">Unknown — require review</option><option value="standard">Standard</option><option value="safety_rated">Safety rated</option><option value="mixed_standard_and_safety">Mixed standard and safety</option>
                  </select>
                </label>
                <label>HMI product
                  <select value={draft.target.hmiProduct} onChange={(event) => updateTarget("hmiProduct", event.target.value)}>
                    {HMI_PRODUCTS.map((product) => <option value={product.value} key={product.value}>{product.label}</option>)}
                  </select>
                </label>
                <label>Release mode
                  <select value={draft.target.releaseMode} onChange={(event) => updateTarget("releaseMode", event.target.value)}>
                    <option value="import_package_only">Import-ready package only</option><option value="rockwell_workstation_assembly">Verify on licensed Rockwell workstation</option><option value="review_only">Review and documentation only</option>
                  </select>
                </label>
                {draft.target.hmiProduct !== "none" && (
                  <>
                    <label>HMI version<input value={draft.target.hmiVersion} onChange={(event) => updateTarget("hmiVersion", event.target.value)} /></label>
                    <label>Terminal / model<input value={draft.target.terminalModel} onChange={(event) => updateTarget("terminalModel", event.target.value)} /></label>
                    <label>Resolution<input value={draft.target.resolution} onChange={(event) => updateTarget("resolution", event.target.value)} placeholder="e.g. 1280 × 800" /></label>
                  </>
                )}
              </div>
            </section>
            <fieldset className="eng-check-grid">
              <legend>Restricted engineering domains</legend>
              <p>Select applicable domains to add the required human gates.</p>
              {RESTRICTED_DOMAINS.map((domain) => (
                <label key={domain.value}>
                  <input type="checkbox" checked={draft.target.restrictedDomains.includes(domain.value)} onChange={() => toggleRestrictedDomain(domain.value)} />
                  <span>{domain.label}</span>
                </label>
              ))}
            </fieldset>
            <section>
              <h4>Standards and templates</h4>
              <div className="eng-form-grid three">
                <label>Naming standard<input value={draft.standards.namingStandard} onChange={(event) => updateStandards("namingStandard", event.target.value)} /></label>
                <label>Alarm philosophy<input value={draft.standards.alarmPhilosophy} onChange={(event) => updateStandards("alarmPhilosophy", event.target.value)} /></label>
                <label>Controller template<input value={draft.standards.controllerTemplate} onChange={(event) => updateStandards("controllerTemplate", event.target.value)} /></label>
                <label>AOI / UDT library<input value={draft.standards.aoiUdtLibrary} onChange={(event) => updateStandards("aoiUdtLibrary", event.target.value)} /></label>
                <label>HMI library<input value={draft.standards.hmiLibrary} onChange={(event) => updateStandards("hmiLibrary", event.target.value)} /></label>
                <label>Test template set<input value={draft.standards.testTemplateSet} onChange={(event) => updateStandards("testTemplateSet", event.target.value)} /></label>
              </div>
            </section>
          </div>
        </details>

        <div className="eng-provider-notice"><Info aria-hidden="true" /><div><b>Local workflow</b><p>{providerNotice}</p></div></div>
        <label className="eng-confirmation eng-launch-confirmation">
          <input
            type="checkbox"
            checked={draft.safetyAcknowledged}
            onChange={(event) => setDraft((current) => ({
              ...current,
              safetyAcknowledged: event.target.checked,
              analysisConfirmed: event.target.checked
            }))}
          />
          <ShieldCheck aria-hidden="true" />
          <span><b>Engineering responsibility acknowledged</b>Start analysis with the available inputs. AI output is advisory; safety, restricted-domain, equipment-damage, and final-release decisions require an authorized human reviewer.</span>
        </label>
      </div>

      <footer className="eng-intake-footer eng-launch-footer">
        <button type="button" className="quiet danger" onClick={clearDraft}>Clear draft</button>
        <div className={errors.length === 0 ? "eng-launch-readiness ready" : "eng-launch-readiness"} aria-live="polite">
          {errors.length === 0
            ? <><Rocket aria-hidden="true" /><span><b>Ready to launch</b>Agents will begin when the project is created.</span></>
            : <><AlertTriangle aria-hidden="true" /><span><b>{errors.length} required item{errors.length === 1 ? "" : "s"}</b>{errors.map((issue) => issue.message.replace(/ is required\.$/, "")).join(" · ")}</span></>}
        </div>
        <button
          type="submit"
          className="primary eng-launch-button"
          disabled={errors.length > 0 || !onSubmit || submitState === "submitting"}
          title={!onSubmit ? capability.reason : errors.length > 0 ? "Complete the required launch fields first." : undefined}
        >
          {submitState === "submitting" ? "Starting agents…" : mode === "demo" ? "Start demo project" : "Create project & start agents"}
          <Cpu aria-hidden="true" />
        </button>
      </footer>
      {!onSubmit && <p className="eng-disabled-reason">Project launch is disabled: {capability.reason}</p>}
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
