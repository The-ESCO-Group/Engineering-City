import { describe, expect, it } from "vitest";
import { agents, buildings, findings } from "./domain";

describe("workflow domain", () => {
  it("maps every agent to a stable building", () => {
    const ids = new Set(buildings.map((building) => building.id));
    expect(agents.every((agent) => ids.has(agent.building))).toBe(true);
  });
  it("never hides low-confidence findings", () => {
    expect(findings.filter((finding) => finding.confidence < 0.8).map((finding) => finding.id)).toContain("F-112");
  });
  it("uses exact synthetic evidence locations", () => {
    expect(findings.every((finding) => finding.source && finding.location)).toBe(true);
  });
});

