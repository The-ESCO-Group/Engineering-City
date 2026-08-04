import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectIntake } from "./ProjectIntake";
import { QUICK_INTAKE_DEFAULTS } from "./intake";

const capability = { level: "available" as const, reason: "Local workflow is ready." };

beforeEach(() => window.localStorage.clear());
afterEach(cleanup);

describe("ProjectIntake quick launch", () => {
  it("presents one concise launch screen without a wizard", () => {
    render(<ProjectIntake mode="live" capability={capability} providerNotice="Local backend connected." onSubmit={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Start a project" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /continue/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create project & start agents/i })).toBeDisabled();
    expect(screen.getByText(/Advanced project settings/)).toBeInTheDocument();
  });

  it("launches with essential inputs and carries advanced defaults into submission", async () => {
    const onSubmit = vi.fn().mockResolvedValue({ projectId: "project-1" });
    render(<ProjectIntake mode="live" capability={capability} providerNotice="Local backend connected." onSubmit={onSubmit} />);

    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Line 3 upgrade" } });
    fireEvent.change(screen.getByLabelText("Controller family"), { target: { value: "compactlogix" } });
    fireEvent.change(screen.getByLabelText("Catalog number"), { target: { value: "5069-L306ER" } });
    fireEvent.change(screen.getByLabelText("Firmware"), { target: { value: "35" } });
    fireEvent.change(screen.getByLabelText("Studio 5000 version"), { target: { value: "35" } });
    fireEvent.change(screen.getByLabelText(/Select or drop engineering files/), {
      target: { files: [new File(["synthetic"], "requirements.pdf", { type: "application/pdf", lastModified: 10 })] }
    });
    fireEvent.click(screen.getByLabelText(/Engineering responsibility acknowledged/));

    const launch = screen.getByRole("button", { name: /create project & start agents/i });
    expect(launch).toBeEnabled();
    fireEvent.click(launch);

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    const submission = onSubmit.mock.calls[0][0];
    expect(submission.draft).toMatchObject({
      identity: {
        name: "Line 3 upgrade",
        plantSite: QUICK_INTAKE_DEFAULTS.plantSite,
        description: QUICK_INTAKE_DEFAULTS.description,
        projectType: "other"
      },
      standards: {
        namingStandard: QUICK_INTAKE_DEFAULTS.namingStandard,
        alarmPhilosophy: QUICK_INTAKE_DEFAULTS.alarmPhilosophy
      },
      safetyAcknowledged: true,
      analysisConfirmed: true
    });
    expect(submission.files).toHaveLength(1);
    expect(await screen.findByRole("status")).toHaveTextContent(/engineering agents were started/i);
  });
});
