import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { createDemoWorkspaceModel } from "./demo";
import { ProjectCoreExplorer } from "./ProjectCoreExplorer";

afterEach(cleanup);

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

  it("opens a requested Project Core record and emits router-safe deep links", () => {
    const model = createDemoWorkspaceModel();
    render(<ProjectCoreExplorer view="requirements" initialRecordId="demo-req-002" capability={model.capabilities.projectCore} requirements={model.requirements} devices={model.devices} tags={model.tags} plcPrograms={model.plcPrograms} />);
    expect(screen.getByRole("heading", { name: "URS-2.8" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Copyable deep link/ })).toHaveAttribute("href", "#/workspace/requirements?record=demo-req-002");
  });

  it("does not substitute a record for an invalid deep link", () => {
    const model = createDemoWorkspaceModel();
    render(<ProjectCoreExplorer view="requirements" initialRecordId="missing-requirement" capability={model.capabilities.projectCore} requirements={model.requirements} devices={model.devices} tags={model.tags} plcPrograms={model.plcPrograms} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Requirement link not found");
    expect(screen.queryByRole("heading", { name: "SRS-4.3.2" })).not.toBeInTheDocument();
  });

  it("applies a route selection once without snapping back after live data refresh", () => {
    const model = createDemoWorkspaceModel();
    const props = { view: "requirements" as const, initialRecordId: "demo-req-002", capability: model.capabilities.projectCore, requirements: model.requirements, devices: model.devices, tags: model.tags, plcPrograms: model.plcPrograms };
    const { rerender } = render(<ProjectCoreExplorer {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /SRS-4\.3\.2/ }));
    expect(screen.getByRole("heading", { name: "SRS-4.3.2" })).toBeInTheDocument();
    rerender(<ProjectCoreExplorer {...props} requirements={[...model.requirements]} />);
    expect(screen.getByRole("heading", { name: "SRS-4.3.2" })).toBeInTheDocument();
  });
});
