import { afterEach, describe, expect, it, vi } from "vitest";

async function loadClient(apiBase?: string) {
  vi.resetModules();
  vi.doMock("../config", () => ({ configuredApiBase: () => apiBase || "/api" }));
  return import("./client");
}

function responseJson(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    ...init
  });
}

describe("engineering API client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.doUnmock("../config");
    vi.resetModules();
  });

  it("does not add Content-Type to bodyless GET requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      responseJson({ status: "ok", contract_version: "1.0.0" })
    );
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient();

    await engineeringApi.health();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${window.location.origin}/api/health`);
    expect(init.credentials).toBe("include");
    expect(new Headers(init.headers).has("Content-Type")).toBe(false);
  });

  it("adds JSON headers only for JSON bodies and leaves FormData boundary-safe", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson({ access_token: "token" }))
      .mockResolvedValueOnce(responseJson({ accepted_count: 1, rejected_count: 0, results: [] }));
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient();

    await engineeringApi.login("engineer@esco.internal", "password");
    await engineeringApi.uploadFiles("project-1", [new File(["row"], "io.csv")], "io_list");

    const [, loginInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(loginInit.headers).get("Content-Type")).toBe("application/json");
    expect(loginInit.body).toBe(
      JSON.stringify({ email: "engineer@esco.internal", password: "password" })
    );

    const [, uploadInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(uploadInit.body).toBeInstanceOf(FormData);
    expect(new Headers(uploadInit.headers).has("Content-Type")).toBe(false);
  });

  it("constructs REST, report, and secure WebSocket URLs from an absolute API base", async () => {
    const fetchMock = vi.fn().mockResolvedValue(responseJson({ items: [], total: 0, limit: 50, offset: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi, projectSocketUrl, reportDownloadUrl } = await loadClient(
      "https://backend.example.test/api"
    );

    await engineeringApi.projects();

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "https://backend.example.test/api/projects?limit=50"
    );
    expect(reportDownloadUrl("/api/reports/report-1/content")).toBe(
      "https://backend.example.test/api/reports/report-1/content"
    );
    expect(() => reportDownloadUrl("https://files.example.test/report.pdf")).toThrow(
      "Report downloads must remain on the configured API host."
    );
    expect(projectSocketUrl("project-1", 42)).toBe(
      "wss://backend.example.test/ws/projects/project-1?since_seq=42"
    );
  });

  it("surfaces the stable backend error envelope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      responseJson(
        { error: { code: "permission_denied", message: "Reviewer access is required." } },
        { status: 403 }
      )
    );
    vi.stubGlobal("fetch", fetchMock);
    const { ApiError, engineeringApi } = await loadClient();

    const error = await engineeringApi.projects().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      message: "Reviewer access is required.",
      status: 403,
      code: "permission_denied"
    });
  });

  it("falls back to an actionable message for non-JSON failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("proxy unavailable", { status: 502 }))
    );
    const { engineeringApi } = await loadClient();

    await expect(engineeringApi.health()).rejects.toMatchObject({
      message: "Local backend request failed (502).",
      status: 502,
      code: "request_failed"
    });
  });
});
