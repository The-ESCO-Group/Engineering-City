import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReleaseRoute } from "./ReleaseRoute";
import type { ReleaseCandidateSummary } from "./types";

const candidate: ReleaseCandidateSummary = {
  id: "10000000-0000-4000-8000-000000000002",
  number: 2,
  name: "Line 4 candidate 2",
  revision: 8,
  status: "approved",
  validationLevel: "round_trip_parsed",
  artifactCount: 12,
  expectedArtifactCount: 12,
  limitationCount: 1,
  unsupportedItemCount: 0,
  generatorVersion: "2.0.0",
  packageBytes: 1024,
  packageSha256: "a".repeat(64),
  createdAt: "2026-07-31T14:00:00Z"
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ReleaseRoute", () => {
  it("uses a direct state highlight when reduced motion is requested", () => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
      matches: true,
      media: "(prefers-reduced-motion: reduce)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn()
    }));
    render(<ReleaseRoute candidate={candidate} released={false} />);
    expect(screen.getByText(/Reduced motion is active/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Replay release route" }));
    expect(screen.getByLabelText("Release candidate 2 package").parentElement).toHaveClass("reduced-motion", "is-replaying");
  });
});
