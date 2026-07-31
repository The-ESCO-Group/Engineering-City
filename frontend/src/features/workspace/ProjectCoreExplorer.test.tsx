import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { createDemoWorkspaceModel } from "./demo";
import { ProjectCoreExplorer } from "./ProjectCoreExplorer";

describe("ProjectCoreExplorer", () => {
  it("filters the tag register and preserves revision provenance", () => {
    const model = createDemoWorkspaceModel();
    render(<ProjectCoreExplorer view="tags" capability={model.capabilities.projectCore} requirements={model.requirements} devices={model.devices} tags={model.tags} plcPrograms={model.plcPrograms} />);
    fireEvent.change(screen.getByPlaceholderText(/Search tag/), { target: { value: "GuardHealthy" } });
    expect(screen.getAllByText("Line4_GuardHealthy").length).toBeGreaterThan(0);
    expect(screen.queryByText("PE_402")).not.toBeInTheDocument();
    expect(screen.getAllByText(/Not approved/).length).toBeGreaterThan(0);
  });

  it("renders structured ladder instructions and does not claim an L5X artifact", () => {
    const model = createDemoWorkspaceModel();
    render(<ProjectCoreExplorer view="plc" capability={model.capabilities.projectCore} requirements={model.requirements} devices={model.devices} tags={model.tags} plcPrograms={model.plcPrograms} />);
    expect(screen.getByText("XIC")).toBeInTheDocument();
    expect(screen.getByText("GuardReset")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /L5X preview/ })).toBeDisabled();
    expect(screen.getByText(/no Studio 5000 import verification/i)).toBeInTheDocument();
  });
});

