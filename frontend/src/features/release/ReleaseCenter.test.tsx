import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReleaseCenter } from "./ReleaseCenter";
import { createDemoReleaseSource } from "./demo";
import type { ReleaseSource } from "./types";

function reportSource(): ReleaseSource {
  const source = createDemoReleaseSource();
  const reportId = "10000000-0000-4000-8000-000000000005";
  return {
    ...source,
    dataMode: "live",
    fallbackReason: "",
    reports: [{
      id: reportId,
      report_type: "executive_summary",
      export_format: "pdf",
      provider_name: "ESCO reporting",
      title: "Executive Summary",
      status: "generated",
      media_type: "application/pdf",
      size_bytes: 8192,
      sha256: "c".repeat(64),
      finding_ids: [],
      summary: {
        validation_level: "round_trip_parsed",
        report_version: "1.0.0",
        document_status: "DRAFT - GENERATED; NOT REPORT-APPROVED"
      },
      error_message: "",
      download_path: "/api/reports/summary/content",
      created_at: "2026-07-31T14:00:00Z"
    }],
    artifactDownloads: {
      [reportId]: { url: "/downloads/executive-summary.pdf", filename: "executive-summary.pdf" }
    }
  };
}

describe("ReleaseCenter", () => {
  it("labels synthetic release state, explains disabled actions, and exposes every package folder", () => {
    render(<ReleaseCenter source={createDemoReleaseSource()} />);
    expect(screen.getByText(/DEMO DATA · SYNTHETIC RELEASE RECORDS/)).toBeInTheDocument();
    const validation = screen.getByRole("button", { name: "Run Validation" });
    expect(validation).toBeDisabled();
    expect(screen.getAllByText(/Synthetic demo mode never calls release or generation APIs/).length).toBeGreaterThan(0);
    fireEvent.click(within(screen.getByRole("navigation", { name: "Release Center sections" })).getByRole("button", { name: /Artifacts/ }));
    expect(screen.getByText("00 Manifest")).toBeInTheDocument();
    expect(screen.getByText("01 Controller")).toBeInTheDocument();
    expect(screen.getByText("02 HMI")).toBeInTheDocument();
    expect(screen.getByText("03 Alarms")).toBeInTheDocument();
    expect(screen.getByText("04 Testing")).toBeInTheDocument();
    expect(screen.getByText("05 Reports")).toBeInTheDocument();
    expect(screen.getByText("06 Audit")).toBeInTheDocument();
    expect(screen.getByText(/No fake download is offered/)).toBeInTheDocument();
  });

  it("renders the complete required report catalog and a real API-backed link", () => {
    render(<ReleaseCenter source={reportSource()} initialView="reports" />);
    expect(screen.getByText("Executive Summary", { selector: "h3" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Known Limitations/ })).toBeInTheDocument();
    const download = screen.getByRole("link", { name: /Download generated PDF/ });
    expect(download).toHaveAttribute("href", "/downloads/executive-summary.pdf");
    expect(screen.getByText("DRAFT - GENERATED; NOT REPORT-APPROVED")).toBeInTheDocument();
  });
});
