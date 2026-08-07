import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { engineeringApi } from "../../api/client";
import type { ValidationResultRead } from "../../api/types";
import { ValidationBlockersPanel } from "./ValidationBlockersPanel";

const blocker: ValidationResultRead = {
  id: "validation-1",
  project_id: "project-1",
  validation_run_id: "run-1",
  code: "PLC-INTERLOCK-001",
  discipline: "interlocks",
  severity: "error",
  message: "Guard interlock has no implementing logic.",
  detail: {},
  object_type: "interlock",
  object_id: "object-1",
  object_label: "Guard door interlock",
  suggested_action: "Implement or formally accept the documented risk.",
  requires_human_decision: true,
  is_waived: false,
  waived_by_id: null,
  waiver_reason: "",
  created_at: "2026-08-05T10:00:00Z",
  updated_at: "2026-08-05T10:15:00Z"
};

function renderPanel(canWaive = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ValidationBlockersPanel projectId="project-1" canWaive={canWaive} />
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ValidationBlockersPanel", () => {
  it("requires a reason and explicit confirmation and sends the current timestamp", async () => {
    const validationResults = vi.spyOn(engineeringApi, "validationResults").mockResolvedValue([blocker]);
    const waive = vi.spyOn(engineeringApi, "waiveValidationResult").mockResolvedValue({
      result: { ...blocker, is_waived: true, waiver_reason: "Reviewed risk." },
      waived_by_id: "reviewer-1",
      waived_at: "2026-08-05T10:20:00Z",
      decision_replayed: false,
      unblocked_task_count: 2
    });
    renderPanel();

    expect(await screen.findByText(blocker.message)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review waiver" }));
    const submit = screen.getByRole("button", { name: "Confirm waiver" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Engineering reason/), { target: { value: "Reviewed risk." } });
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/I confirm this result/));
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(waive).toHaveBeenCalledWith(
      blocker.id,
      "Reviewed risk.",
      blocker.updated_at
    ));
    expect(await screen.findByText(/2 task\(s\) unblocked/)).toBeInTheDocument();
    expect(validationResults).toHaveBeenCalledTimes(2);
  });

  it("fails closed when authoritative validation results cannot be loaded", async () => {
    vi.spyOn(engineeringApi, "validationResults").mockRejectedValue(new Error("backend offline"));
    const waive = vi.spyOn(engineeringApi, "waiveValidationResult");
    renderPanel();

    expect(await screen.findByRole("alert")).toHaveTextContent(/results are unavailable/i);
    expect(screen.queryByRole("button", { name: "Review waiver" })).not.toBeInTheDocument();
    expect(waive).not.toHaveBeenCalled();
  });

  it("shows results but disables waiver entry for a non-reviewer", async () => {
    vi.spyOn(engineeringApi, "validationResults").mockResolvedValue([blocker]);
    renderPanel(false);

    expect(await screen.findByText(blocker.message)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review waiver" })).toBeDisabled();
    expect(screen.getByText(/require a reviewer or administrator/)).toBeInTheDocument();
  });
});
