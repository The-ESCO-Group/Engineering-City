import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { createDemoWorkspaceModel } from "./demo";
import { EvidenceViewer } from "./EvidenceViewer";

describe("EvidenceViewer", () => {
  it("renders an exact L5X location and lets keyboard-equivalent buttons switch citations", () => {
    const evidence = createDemoWorkspaceModel().findings[0].evidence;
    render(<EvidenceViewer evidence={evidence} />);
    expect(screen.getByText("Heading 4.3 · Conveyor permissives · Paragraph 2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Synthetic MainProgram.L5X/ }));
    expect(screen.getByText("Program MainProgram · Routine Safety · Rung 14")).toBeInTheDocument();
    expect(screen.getByText(/stopped-state proof belongs in the common reset path/)).toBeInTheDocument();
  });

  it("shows an honest empty state instead of synthesizing a citation", () => {
    render(<EvidenceViewer evidence={[]} />);
    expect(screen.getByText("No evidence citation is available")).toBeInTheDocument();
    expect(screen.getByText(/does not infer a source or location/)).toBeInTheDocument();
  });
});
