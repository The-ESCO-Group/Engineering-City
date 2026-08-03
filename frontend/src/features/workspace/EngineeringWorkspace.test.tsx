import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EngineeringWorkspace } from "./EngineeringWorkspace";
import type { WorkspaceSource } from "./types";

const demoSource: WorkspaceSource = {
  dataMode: "demo",
  fallbackReason: "Synthetic demo mode is active.",
  project: null,
  files: [],
  tasks: [],
  findings: [],
  approvals: [],
  events: []
};

afterEach(cleanup);

describe("EngineeringWorkspace", () => {
  it("exposes synthetic HMI, alarm, test, and documentation records without claiming runtime verification", () => {
    const openReleaseCenter = vi.fn();
    render(<EngineeringWorkspace source={demoSource} operations={{ openReleaseCenter }} />);

    fireEvent.click(screen.getByRole("button", { name: "HMI" }));
    expect(screen.getByRole("heading", { name: "HMI engineering" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Line4_Overview" })).toBeInTheDocument();
    expect(screen.getAllByText(/target import verification not performed/i)).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Alarms" }));
    expect(screen.getByRole("heading", { name: "L4-JAM-402" })).toBeInTheDocument();
    expect(screen.getAllByText(/Trigger tag unresolved/i)).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Tests" }));
    expect(screen.getByRole("heading", { name: /DEMO-FAT-014/ })).toBeInTheDocument();
    expect(screen.getByText(/No physical equipment operation is authorized/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Documentation" }));
    expect(screen.getByRole("heading", { name: "Detailed engineering discrepancy report" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Release Center" }));
    expect(openReleaseCenter).toHaveBeenCalledOnce();
  });

  it("labels all synthetic records prominently and supports a local demo approval decision", async () => {
    render(<EngineeringWorkspace source={demoSource} />);
    expect(screen.getByText(/DEMO DATA · SYNTHETIC OFFLINE/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Approvals/ }));
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(screen.getByText(/A comment is required for this decision/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Engineering comment/), { target: { value: "Synthetic review comment only." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit signed decision" }));
    await waitFor(() => expect(screen.getByText(/Approve recorded for Disposition guard reset finding/)).toBeInTheDocument());
  });
});
