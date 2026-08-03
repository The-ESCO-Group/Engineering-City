import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WORLD_QUALITY_PROFILES } from "../world/quality";
import { WorldQualityControl } from "./WorldQualityControl";

afterEach(cleanup);

describe("WorldQualityControl", () => {
  it.each([
    [61, "target", "61 FPS · on target"],
    [40, "fallback", "40 FPS · fallback range"],
    [18, "below-fallback", "18 FPS · below fallback"]
  ] as const)("reports a sampled %s FPS against the active frame budget", (currentFps, state, label) => {
    render(
      <WorldQualityControl
        requested="high"
        resolved="high"
        profile={WORLD_QUALITY_PROFILES.high}
        fpsStatus={{ currentFps }}
        onChange={vi.fn()}
      />
    );

    expect(screen.getByRole("region", { name: "World rendering quality" })).toHaveAttribute("data-fps-state", state);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("does not pretend an FPS measurement exists before the renderer samples it", () => {
    render(
      <WorldQualityControl
        requested="balanced"
        resolved="balanced"
        profile={WORLD_QUALITY_PROFILES.balanced}
        fpsStatus={null}
        onChange={vi.fn()}
      />
    );

    expect(screen.queryByText(/on target|fallback range|below fallback/)).not.toBeInTheDocument();
    expect(screen.getByText("Target 45 FPS · fallback 30 FPS")).toBeInTheDocument();
  });
});
