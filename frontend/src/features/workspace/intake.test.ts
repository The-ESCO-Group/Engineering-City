import { describe, expect, it } from "vitest";
import {
  INTAKE_DRAFT_STORAGE_KEY,
  buildProjectIdentityInput,
  createEmptyIntakeDraft,
  loadIntakeDraft,
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
      projectType: "Engineering review"
    },
    target: {
      ...draft.target,
      controllerFamily: "CompactLogix",
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
  it("reports required fields by guided step without inventing defaults", () => {
    const issues = validateIntakeDraft(createEmptyIntakeDraft());
    expect(issues.some((issue) => issue.step === 1 && issue.field === "identity.name")).toBe(true);
    expect(issues.some((issue) => issue.step === 2 && issue.field === "target.firmware")).toBe(true);
    expect(issues.some((issue) => issue.step === 4 && issue.field === "files")).toBe(true);
    expect(issues.some((issue) => issue.step === 5 && issue.field === "safetyAcknowledged")).toBe(true);
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
    draft.target.hmiProduct = "FactoryTalk View ME";
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
    expect(restored.analysisConfirmed).toBe(false);
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

