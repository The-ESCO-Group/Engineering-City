import type {
  IntakeDraft,
  IntakeValidationIssue,
  StagedIntakeFile
} from "./types";

export const INTAKE_DRAFT_STORAGE_KEY = "esco:intake-draft:v1";

export const INTAKE_STEPS = [
  "Project identity",
  "Target Rockwell profile",
  "Standards & templates",
  "Engineering files",
  "Safety & review"
] as const;

export const DOCUMENT_ROLES = [
  { value: "functional_description", label: "Functional description" },
  { value: "io_list", label: "Device / I/O list" },
  { value: "interlock_schedule", label: "Interlock matrix" },
  { value: "control_logic", label: "Controller logic / L5X" },
  { value: "hmi_export", label: "HMI XML" },
  { value: "alarm_list", label: "Alarm file" },
  { value: "testing_notes", label: "Testing notes" },
  { value: "other", label: "Drawing / supporting document" },
  { value: "unclassified", label: "Unclassified" }
] as const;

export const RESTRICTED_DOMAINS = [
  { value: "motion", label: "Motion" },
  { value: "safety_logic", label: "Safety logic" },
  { value: "burner_management", label: "Burner management" },
  { value: "process_safety", label: "Process safety" },
  { value: "high_energy", label: "High energy" },
  { value: "electrical_power_distribution", label: "Electrical power distribution" },
  { value: "robotics", label: "Robotics" }
] as const;

export const QUICK_INTAKE_DEFAULTS = {
  plantSite: "Not specified",
  description: "Autonomous engineering review of uploaded project source files.",
  projectType: "other",
  namingStandard: "ESCO default naming standard",
  alarmPhilosophy: "ESCO default alarm philosophy",
  controllerTemplate: "ESCO default controller template",
  testTemplateSet: "ESCO default engineering test templates"
} as const;

const SUPPORTED_EXTENSIONS = new Set([
  ".l5x",
  ".xlsx",
  ".csv",
  ".docx",
  ".pdf",
  ".xml",
  ".zip",
  ".txt"
]);

interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function nowIso(): string {
  return new Date().toISOString();
}

export function createEmptyIntakeDraft(): IntakeDraft {
  return {
    version: 1,
    updatedAt: nowIso(),
    lastCompletedStep: 0,
    identity: {
      name: "",
      customerLabel: "",
      plantSite: "",
      description: "",
      projectType: QUICK_INTAKE_DEFAULTS.projectType,
      units: "metric",
      locale: "en-US"
    },
    target: {
      controllerFamily: "",
      catalogNumber: "",
      firmware: "",
      studio5000Version: "",
      safetyClassification: "unknown",
      hmiProduct: "none",
      hmiVersion: "",
      terminalModel: "",
      resolution: "",
      releaseMode: "import_package_only",
      restrictedDomains: []
    },
    standards: {
      namingStandard: QUICK_INTAKE_DEFAULTS.namingStandard,
      alarmPhilosophy: QUICK_INTAKE_DEFAULTS.alarmPhilosophy,
      controllerTemplate: QUICK_INTAKE_DEFAULTS.controllerTemplate,
      aoiUdtLibrary: "",
      hmiLibrary: "",
      testTemplateSet: QUICK_INTAKE_DEFAULTS.testTemplateSet
    },
    files: [],
    safetyAcknowledged: false,
    analysisConfirmed: false
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringField(record: Record<string, unknown>, key: string): string {
  return typeof record[key] === "string" ? record[key] : "";
}

function stringList(record: Record<string, unknown>, key: string): string[] {
  return Array.isArray(record[key])
    ? record[key].filter((item): item is string => typeof item === "string")
    : [];
}

function restoredFiles(value: unknown): StagedIntakeFile[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item)) return [];
    const name = stringField(item, "name");
    if (!name) return [];
    return [{
      id: stringField(item, "id") || `restored-${name}`,
      name,
      sizeBytes: typeof item.sizeBytes === "number" ? item.sizeBytes : 0,
      extension: stringField(item, "extension"),
      documentRole: stringField(item, "documentRole") || "unclassified",
      validation: "needs_reselection" as const,
      validationMessage: "Reselect this file before submission; browsers do not persist file contents."
    }];
  });
}

export function restoreIntakeDraft(raw: string | null): IntakeDraft {
  if (!raw) return createEmptyIntakeDraft();
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed) || parsed.version !== 1) return createEmptyIntakeDraft();
    const identity = isRecord(parsed.identity) ? parsed.identity : {};
    const target = isRecord(parsed.target) ? parsed.target : {};
    const standards = isRecord(parsed.standards) ? parsed.standards : {};
    return {
      version: 1,
      updatedAt: stringField(parsed, "updatedAt") || nowIso(),
      lastCompletedStep: typeof parsed.lastCompletedStep === "number"
        ? Math.max(0, Math.min(4, Math.floor(parsed.lastCompletedStep)))
        : 0,
      identity: {
        name: stringField(identity, "name"),
        customerLabel: stringField(identity, "customerLabel"),
        plantSite: stringField(identity, "plantSite"),
        description: stringField(identity, "description"),
        projectType: stringField(identity, "projectType") || QUICK_INTAKE_DEFAULTS.projectType,
        units: stringField(identity, "units") || "metric",
        locale: stringField(identity, "locale") || "en-US"
      },
      target: {
        controllerFamily: stringField(target, "controllerFamily"),
        catalogNumber: stringField(target, "catalogNumber"),
        firmware: stringField(target, "firmware"),
        studio5000Version: stringField(target, "studio5000Version"),
        safetyClassification: stringField(target, "safetyClassification") || "unknown",
        hmiProduct: stringField(target, "hmiProduct") || "none",
        hmiVersion: stringField(target, "hmiVersion"),
        terminalModel: stringField(target, "terminalModel"),
        resolution: stringField(target, "resolution"),
        releaseMode: stringField(target, "releaseMode") || "import_package_only",
        restrictedDomains: stringList(target, "restrictedDomains")
      },
      standards: {
        namingStandard: stringField(standards, "namingStandard") || QUICK_INTAKE_DEFAULTS.namingStandard,
        alarmPhilosophy: stringField(standards, "alarmPhilosophy") || QUICK_INTAKE_DEFAULTS.alarmPhilosophy,
        controllerTemplate: stringField(standards, "controllerTemplate") || QUICK_INTAKE_DEFAULTS.controllerTemplate,
        aoiUdtLibrary: stringField(standards, "aoiUdtLibrary"),
        hmiLibrary: stringField(standards, "hmiLibrary"),
        testTemplateSet: stringField(standards, "testTemplateSet") || QUICK_INTAKE_DEFAULTS.testTemplateSet
      },
      files: restoredFiles(parsed.files),
      safetyAcknowledged: parsed.safetyAcknowledged === true,
      analysisConfirmed: parsed.safetyAcknowledged === true
    };
  } catch {
    return createEmptyIntakeDraft();
  }
}

export function loadIntakeDraft(storage: StorageLike): IntakeDraft {
  return restoreIntakeDraft(storage.getItem(INTAKE_DRAFT_STORAGE_KEY));
}

export function saveIntakeDraft(storage: StorageLike, draft: IntakeDraft): IntakeDraft {
  const updated = { ...draft, updatedAt: nowIso() };
  storage.setItem(INTAKE_DRAFT_STORAGE_KEY, JSON.stringify(updated));
  return updated;
}

export function clearIntakeDraft(storage: StorageLike): void {
  storage.removeItem(INTAKE_DRAFT_STORAGE_KEY);
}

function requireField(
  issues: IntakeValidationIssue[],
  step: number,
  field: string,
  value: string,
  label: string
): void {
  if (!value.trim()) issues.push({ step, field, message: `${label} is required.`, severity: "error" });
}

export function validateIntakeDraft(draft: IntakeDraft): IntakeValidationIssue[] {
  const issues: IntakeValidationIssue[] = [];
  requireField(issues, 1, "identity.name", draft.identity.name, "Project name");
  requireField(issues, 1, "identity.projectType", draft.identity.projectType, "Project type");
  requireField(issues, 1, "identity.units", draft.identity.units, "Unit system");
  requireField(issues, 1, "identity.locale", draft.identity.locale, "Locale");

  requireField(issues, 2, "target.controllerFamily", draft.target.controllerFamily, "Controller family");
  requireField(issues, 2, "target.catalogNumber", draft.target.catalogNumber, "Controller catalog number");
  requireField(issues, 2, "target.firmware", draft.target.firmware, "Firmware version");
  requireField(issues, 2, "target.studio5000Version", draft.target.studio5000Version, "Studio 5000 version");
  requireField(issues, 2, "target.safetyClassification", draft.target.safetyClassification, "Safety classification");
  requireField(issues, 2, "target.releaseMode", draft.target.releaseMode, "Release mode");
  if (draft.target.hmiProduct !== "none") {
    requireField(issues, 2, "target.hmiVersion", draft.target.hmiVersion, "HMI version");
    requireField(issues, 2, "target.terminalModel", draft.target.terminalModel, "HMI terminal or model");
    requireField(issues, 2, "target.resolution", draft.target.resolution, "HMI resolution");
  }

  requireField(issues, 3, "standards.namingStandard", draft.standards.namingStandard, "Naming standard");
  requireField(issues, 3, "standards.alarmPhilosophy", draft.standards.alarmPhilosophy, "Alarm philosophy");
  requireField(issues, 3, "standards.controllerTemplate", draft.standards.controllerTemplate, "Controller template");
  requireField(issues, 3, "standards.testTemplateSet", draft.standards.testTemplateSet, "Test template set");

  if (draft.files.length === 0) {
    issues.push({
      step: 4,
      field: "files",
      message: "Add at least one source file before analysis.",
      severity: "error"
    });
  }
  if (draft.files.some((file) => file.validation === "unsupported")) {
    issues.push({
      step: 4,
      field: "files",
      message: "Remove unsupported files or replace them with a supported local format.",
      severity: "error"
    });
  }
  if (draft.files.some((file) => file.validation === "needs_reselection")) {
    issues.push({
      step: 4,
      field: "files",
      message: "Reselect restored files before submission.",
      severity: "error"
    });
  }
  if (!draft.safetyAcknowledged) {
    issues.push({
      step: 5,
      field: "safetyAcknowledged",
      message: "Acknowledge that AI output is advisory and restricted work requires human approval.",
      severity: "error"
    });
  }
  return issues;
}

/**
 * Fill contract-required fields that are intentionally optional in quick intake.
 * Explicit user values always win; these defaults remain visible in Advanced
 * settings so an engineer can replace them before launch.
 */
export function prepareIntakeSubmissionDraft(draft: IntakeDraft): IntakeDraft {
  return {
    ...draft,
    identity: {
      ...draft.identity,
      plantSite: draft.identity.plantSite.trim() || QUICK_INTAKE_DEFAULTS.plantSite,
      description: draft.identity.description.trim() || QUICK_INTAKE_DEFAULTS.description,
      projectType: draft.identity.projectType || QUICK_INTAKE_DEFAULTS.projectType,
      units: draft.identity.units || "metric",
      locale: draft.identity.locale.trim() || "en-US"
    },
    standards: {
      ...draft.standards,
      namingStandard: draft.standards.namingStandard.trim() || QUICK_INTAKE_DEFAULTS.namingStandard,
      alarmPhilosophy: draft.standards.alarmPhilosophy.trim() || QUICK_INTAKE_DEFAULTS.alarmPhilosophy,
      controllerTemplate: draft.standards.controllerTemplate.trim() || QUICK_INTAKE_DEFAULTS.controllerTemplate,
      testTemplateSet: draft.standards.testTemplateSet.trim() || QUICK_INTAKE_DEFAULTS.testTemplateSet
    },
    analysisConfirmed: draft.safetyAcknowledged
  };
}

export function issuesForStep(issues: IntakeValidationIssue[], step: number): IntakeValidationIssue[] {
  return issues.filter((issue) => issue.step === step);
}

export function fileExtension(name: string): string {
  const separator = name.lastIndexOf(".");
  return separator >= 0 ? name.slice(separator).toLowerCase() : "";
}

export function stageIntakeFile(file: File, documentRole = "unclassified"): StagedIntakeFile {
  const extension = fileExtension(file.name);
  const supported = SUPPORTED_EXTENSIONS.has(extension);
  return {
    id: `${file.name}:${file.size}:${file.lastModified}`,
    name: file.name,
    sizeBytes: file.size,
    extension,
    documentRole,
    validation: supported ? "ready" : "unsupported",
    validationMessage: supported
      ? "Supported local format; the backend will perform authoritative validation."
      : `Unsupported extension ${extension || "(none)"}. This file will not be submitted.`
  };
}

export function buildProjectIdentityInput(draft: IntakeDraft): {
  name: string;
  description: string;
  client_reference: string;
} {
  return {
    name: draft.identity.name.trim(),
    description: draft.identity.description.trim(),
    client_reference: draft.identity.customerLabel.trim()
  };
}
