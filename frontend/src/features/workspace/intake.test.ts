import { describe, expect, it } from "vitest";
import {
  INTAKE_DRAFT_STORAGE_KEY,
  QUICK_INTAKE_DEFAULTS,
  buildProjectIdentityInput,
  createEmptyIntakeDraft,
  loadIntakeDraft,
  prepareIntakeSubmissionDraft,
  restoreIntakeDraft,
  saveIntakeDraft,
  stageIntakeFile,
  validateIntakeDraft
} from "./intake";
import type { IntakeDraft } from "./types";

function validDraft(): IntakeDraft {
  const draft = createEmptyIntakeDraft();
  return {
    ...draft,
    identity: {
      name: "Synthetic Line Upgrade",
      customerLabel: "Internal demo",
      plantSite: "Synthetic site",
      description: "Review a synthetic conveyor control package.",
      projectType: "upgrade",
      units: "metric",
      locale: "en-US"
    },
    target: {
      ...draft.target,
      controllerFamily: "compactlogix",
      catalogNumber: "Synthetic catalog",
      firmware: "35",
      studio5000Version: "35"
    },
    standards: {
      ...draft.standards,
      namingStandard: "Synthetic ESCO naming v1",
      alarmPhilosophy: "Synthetic alarm philosophy",
      controllerTemplate: "Synthetic controller template",
      testTemplateSet: "Synthetic FAT template"
    },
    files: [{
      id: "source:10:1",
      name: "source.l5x",
      sizeBytes: 10,
      extension: ".l5x",
      documentRole: "control_logic",
      validation: "ready",
      validationMessage: "Ready"
    }],
    safetyAcknowledged: true,
    analysisConfirmed: true
  };
}

describe("intake draft", () => {
  it("requires only the quick-launch essentials and supplies safe advanced defaults", () => {
    const draft = createEmptyIntakeDraft();
    const fields = validateIntakeDraft(draft).map((issue) => issue.field);
    expect(fields).toEqual(expect.arrayContaining([
      "identity.name",
      "target.controllerFamily",
      "target.catalogNumber",
      "target.firmware",
      "target.studio5000Version",
      "files",
      "safetyAcknowledged"
    ]));
    expect(fields).not.toEqual(expect.arrayContaining([
      "identity.customerLabel",
      "identity.plantSite",
      "identity.description",
      "analysisConfirmed",
      "standards.alarmPhilosophy"
    ]));
    expect(draft.identity.projectType).toBe("other");
    expect(draft.standards.namingStandard).toBe(QUICK_INTAKE_DEFAULTS.namingStandard);
  });

  it("normalizes optional quick-launch fields before preserving the backend contract", () => {
    const draft = validDraft();
    draft.identity.plantSite = "";
    draft.identity.description = "";
    draft.analysisConfirmed = false;
    const prepared = prepareIntakeSubmissionDraft(draft);
    expect(prepared.identity.plantSite).toBe(QUICK_INTAKE_DEFAULTS.plantSite);
    expect(prepared.identity.description).toBe(QUICK_INTAKE_DEFAULTS.description);
    expect(prepared.analysisConfirmed).toBe(true);
    expect(validateIntakeDraft(prepared)).toEqual([]);
  });

  it("accepts a complete synthetic draft and sends identity fields only", () => {
    const draft = validDraft();
    expect(validateIntakeDraft(draft)).toEqual([]);
    expect(buildProjectIdentityInput(draft)).toEqual({
      name: "Synthetic Line Upgrade",
      description: "Review a synthetic conveyor control package.",
      client_reference: "Internal demo"
    });
  });

  it("requires the rest of the HMI target profile only when HMI is in scope", () => {
    const draft = validDraft();
    draft.target.hmiProduct = "factorytalk_view_me";
    const fields = validateIntakeDraft(draft).map((issue) => issue.field);
    expect(fields).toEqual(expect.arrayContaining(["target.hmiVersion", "target.terminalModel", "target.resolution"]));
  });

  it("persists text metadata but marks restored files for reselection", () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key)
    };
    saveIntakeDraft(storage, validDraft());
    expect(values.has(INTAKE_DRAFT_STORAGE_KEY)).toBe(true);
    const restored = loadIntakeDraft(storage);
    expect(restored.identity.name).toBe("Synthetic Line Upgrade");
    expect(restored.files[0].validation).toBe("needs_reselection");
    expect(restored.analysisConfirmed).toBe(true);
  });

  it("recovers safely from malformed persisted JSON", () => {
    expect(restoreIntakeDraft("{not json").identity.name).toBe("");
  });

  it("stages supported local formats and blocks unsupported executable files", () => {
    const supported = stageIntakeFile(new File(["synthetic"], "Main.L5X", { type: "application/xml", lastModified: 10 }));
    const unsupported = stageIntakeFile(new File(["synthetic"], "unsafe.exe", { lastModified: 11 }));
    expect(supported).toMatchObject({ extension: ".l5x", validation: "ready" });
    expect(unsupported).toMatchObject({ extension: ".exe", validation: "unsupported" });
  });
});
