import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ChangeReviewWorkspace } from "./ChangeReviewWorkspace";
import { createDemoWorkspaceModel } from "./demo";

describe("ChangeReviewWorkspace", () => {
  it("shows before/after tag rename impact across PLC, HMI, alarms, tests, and documentation", () => {
    const model = createDemoWorkspaceModel();
    render(<ChangeReviewWorkspace view="changesets" plans={model.plans} changesets={model.changesets} revisions={model.revisions} changesetCapability={model.capabilities.changesetOperations} revisionCapability={model.capabilities.revisionComparison} />);
    expect(screen.getAllByText("Conveyor_4.JamAlarm").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Line4_Infeed_Jam").length).toBeGreaterThan(0);
    expect(screen.getByText("PLC reference")).toBeInTheDocument();
    expect(screen.getByText("HMI binding")).toBeInTheDocument();
    expect(screen.getByText("Alarm")).toBeInTheDocument();
    expect(screen.getByText("Test")).toBeInTheDocument();
    expect(screen.getByText("Documentation")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Approve/ })).toBeDisabled();
  });
});

