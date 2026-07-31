import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
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

describe("EngineeringWorkspace", () => {
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
