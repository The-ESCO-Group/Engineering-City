import { afterEach, describe, expect, it, vi } from "vitest";

async function loadClient(apiBase?: string, desktop = false) {
  vi.resetModules();
  vi.doMock("../config", () => ({ configuredApiBase: () => apiBase || "/api" }));
  vi.doMock("@tauri-apps/api/core", () => ({ isTauri: () => desktop }));
  return import("./client");
}

function responseJson(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    ...init
  });
}

function project(index: number) {
  return {
    id: `project-${index}`,
    name: `Project ${index}`,
    description: "",
    client_reference: `PRJ-${index}`,
    status: "active",
    is_paused: false,
    event_seq: 0,
    created_at: "2026-08-03T12:00:00Z",
    updated_at: "2026-08-03T12:00:00Z",
    attributes: {}
  };
}

describe("engineering API client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.doUnmock("../config");
    vi.doUnmock("@tauri-apps/api/core");
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

  it("retains a successful desktop login token in memory for subsequent REST requests", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson({
        access_token: "desktop-access-token",
        token_type: "bearer",
        expires_at: "2026-08-04T22:00:00Z",
        user: { id: "user-1", email: "engineer@esco.internal", display_name: "Engineer", roles: [] }
      }))
      .mockResolvedValueOnce(responseJson({
        user: { id: "user-1", email: "engineer@esco.internal", display_name: "Engineer", roles: [] },
        project_roles: {}
      }));
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient("http://127.0.0.1:8010/api", true);

    await engineeringApi.login("engineer@esco.internal", "password");
    await engineeringApi.me();

    const calls = fetchMock.mock.calls as Array<[string, RequestInit]>;
    expect(new Headers(calls[0][1].headers).has("Authorization")).toBe(false);
    expect(new Headers(calls[1][1].headers).get("Authorization")).toBe(
      "Bearer desktop-access-token"
    );
    expect(calls[1][1].credentials).toBe("include");
  });

  it("includes the in-memory desktop token in the project WebSocket URL", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(responseJson({
      access_token: "header.payload.signature",
      token_type: "bearer",
      expires_at: "2026-08-04T22:00:00Z",
      user: { id: "user-1", email: "engineer@esco.internal", display_name: "Engineer", roles: [] }
    })));
    const { engineeringApi, projectSocketUrl } = await loadClient(
      "http://127.0.0.1:8010/api",
      true
    );

    await engineeringApi.login("engineer@esco.internal", "password");
    const socket = new URL(projectSocketUrl("project-1", 42));

    expect(socket.origin).toBe("ws://127.0.0.1:8010");
    expect(socket.pathname).toBe("/ws/projects/project-1");
    expect(socket.searchParams.get("since_seq")).toBe("42");
    expect(socket.searchParams.get("token")).toBe("header.payload.signature");
  });

  it("uses the desktop bearer for logout and clears it after logout succeeds", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson({
        access_token: "desktop-access-token",
        token_type: "bearer",
        expires_at: "2026-08-04T22:00:00Z",
        user: { id: "user-1", email: "engineer@esco.internal", display_name: "Engineer", roles: [] }
      }))
      .mockResolvedValueOnce(responseJson({ ok: true }))
      .mockResolvedValueOnce(responseJson({ status: "ok", contract_version: "1.1.0" }));
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi, projectSocketUrl } = await loadClient(
      "http://127.0.0.1:8010/api",
      true
    );

    await engineeringApi.login("engineer@esco.internal", "password");
    await engineeringApi.logout();
    await engineeringApi.health();

    const calls = fetchMock.mock.calls as Array<[string, RequestInit]>;
    expect(new Headers(calls[1][1].headers).get("Authorization")).toBe(
      "Bearer desktop-access-token"
    );
    expect(new Headers(calls[2][1].headers).has("Authorization")).toBe(false);
    expect(new URL(projectSocketUrl("project-1", 0)).searchParams.has("token")).toBe(false);
  });

  it("clears the desktop token when an authenticated request returns 401", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson({
        access_token: "expired-desktop-token",
        token_type: "bearer",
        expires_at: "2026-08-04T22:00:00Z",
        user: { id: "user-1", email: "engineer@esco.internal", display_name: "Engineer", roles: [] }
      }))
      .mockResolvedValueOnce(responseJson(
        { error: { code: "authentication_required", message: "Authentication required." } },
        { status: 401 }
      ))
      .mockResolvedValueOnce(responseJson({ status: "ok", contract_version: "1.1.0" }));
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi, projectSocketUrl } = await loadClient(
      "http://127.0.0.1:8010/api",
      true
    );

    await engineeringApi.login("engineer@esco.internal", "password");
    await expect(engineeringApi.me()).rejects.toMatchObject({ status: 401 });
    await engineeringApi.health();

    const calls = fetchMock.mock.calls as Array<[string, RequestInit]>;
    expect(new Headers(calls[1][1].headers).get("Authorization")).toBe(
      "Bearer expired-desktop-token"
    );
    expect(new Headers(calls[2][1].headers).has("Authorization")).toBe(false);
    expect(new URL(projectSocketUrl("project-1", 0)).searchParams.has("token")).toBe(false);
  });

  it("keeps browser authentication cookie-only and does not reuse its login token", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson({
        access_token: "browser-response-token",
        token_type: "bearer",
        expires_at: "2026-08-04T22:00:00Z",
        user: { id: "user-1", email: "engineer@esco.internal", display_name: "Engineer", roles: [] }
      }))
      .mockResolvedValueOnce(responseJson({
        user: { id: "user-1", email: "engineer@esco.internal", display_name: "Engineer", roles: [] },
        project_roles: {}
      }));
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi, projectSocketUrl } = await loadClient(
      "http://127.0.0.1:8010/api",
      false
    );

    await engineeringApi.login("engineer@esco.internal", "password");
    await engineeringApi.me();

    const calls = fetchMock.mock.calls as Array<[string, RequestInit]>;
    expect(new Headers(calls[0][1].headers).has("Authorization")).toBe(false);
    expect(new Headers(calls[1][1].headers).has("Authorization")).toBe(false);
    expect(calls[1][1].credentials).toBe("include");
    expect(new URL(projectSocketUrl("project-1", 42)).searchParams.has("token")).toBe(false);
  });

  it("constructs REST, report, and secure WebSocket URLs from an absolute API base", async () => {
    const fetchMock = vi.fn().mockResolvedValue(responseJson({ items: [], total: 0, limit: 500, offset: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi, projectSocketUrl, reportDownloadUrl } = await loadClient(
      "https://backend.example.test/api"
    );

    await engineeringApi.projects();

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "https://backend.example.test/api/projects?limit=500&offset=0"
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

  it("pages the authorized project portfolio with contracted limits until the final partial page", async () => {
    const totalProjects = 1_250;
    const fetchMock = vi.fn(async (input: string) => {
      const url = new URL(input);
      const offset = Number(url.searchParams.get("offset"));
      const limit = Number(url.searchParams.get("limit"));
      const count = Math.min(limit, Math.max(0, totalProjects - offset));
      return responseJson({
        items: Array.from({ length: count }, (_, index) => project(offset + index + 1)),
        total: totalProjects,
        limit,
        offset
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient();

    const page = await engineeringApi.projects();

    expect(page.items).toHaveLength(totalProjects);
    expect(page.items.at(-1)?.id).toBe("project-1250");
    expect(page).toMatchObject({ total: totalProjects, limit: 10_000, offset: 0 });
    const urls = fetchMock.mock.calls.map(([input]) => new URL(input));
    expect(urls.map((url) => url.searchParams.get("offset"))).toEqual(["0", "500", "1000"]);
    expect(urls.map((url) => url.searchParams.get("limit"))).toEqual(["500", "500", "500"]);
  });

  it("terminates project paging at the reported total without requesting an empty page", async () => {
    const totalProjects = 1_000;
    const fetchMock = vi.fn(async (input: string) => {
      const url = new URL(input);
      const offset = Number(url.searchParams.get("offset"));
      const limit = Number(url.searchParams.get("limit"));
      return responseJson({
        items: Array.from({ length: limit }, (_, index) => project(offset + index + 1)),
        total: totalProjects,
        limit,
        offset
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient();

    const page = await engineeringApi.projects();

    expect(page.items).toHaveLength(totalProjects);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([input]) => new URL(input).searchParams.get("offset"))).toEqual(["0", "500"]);
  });

  it("preserves the reported project total when the bounded portfolio reaches its safety cap", async () => {
    const totalProjects = 10_750;
    const fetchMock = vi.fn(async (input: string) => {
      const url = new URL(input);
      const offset = Number(url.searchParams.get("offset"));
      const limit = Number(url.searchParams.get("limit"));
      return responseJson({
        items: Array.from({ length: limit }, (_, index) => project(offset + index + 1)),
        total: totalProjects,
        limit,
        offset
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient();

    const page = await engineeringApi.projects();

    expect(page.items).toHaveLength(10_000);
    expect(page).toMatchObject({ total: totalProjects, limit: 10_000, offset: 0 });
    expect(fetchMock).toHaveBeenCalledTimes(20);
    expect(fetchMock.mock.calls.map(([input]) => new URL(input).searchParams.get("offset"))).toEqual(
      Array.from({ length: 20 }, (_, index) => String(index * 500))
    );
  });

  it("uses only contracted read endpoints for agent definitions, runs, run detail, and messages", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseJson([]))
      .mockResolvedValueOnce(responseJson([]))
      .mockResolvedValueOnce(responseJson({
        id: "run-1",
        project_id: "project-1",
        agent_kind: "plc_logic",
        status: "completed"
      }))
      .mockResolvedValueOnce(responseJson([]));
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient();

    await engineeringApi.agentDefinitions();
    await engineeringApi.agentRuns("project-1");
    await engineeringApi.agentRun("run-1");
    await engineeringApi.messages("project-1");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      `${window.location.origin}/api/agents`,
      `${window.location.origin}/api/projects/project-1/agent-runs?limit=200`,
      `${window.location.origin}/api/agent-runs/run-1`,
      `${window.location.origin}/api/projects/project-1/messages?limit=500`
    ]);
    for (const [, init] of fetchMock.mock.calls) {
      expect(init.method).toBeUndefined();
    }
  });

  it("cursor-pages the oldest-first message feed so activity beyond the first page is loaded", async () => {
    const totalMessages = 1_250;
    const fetchMock = vi.fn(async (input: string) => {
      const url = new URL(input);
      const since = url.searchParams.get("since");
      const offset = since ? Number(since.replace("message-", "")) : 0;
      const limit = Number(url.searchParams.get("limit"));
      const count = Math.min(limit, Math.max(0, totalMessages - offset));
      return responseJson(Array.from({ length: count }, (_, index) => ({
        id: `message-${offset + index + 1}`,
        project_id: "project-1",
        role: "agent",
        agent_instance_id: "agent-1",
        body: `Message ${offset + index + 1}`,
        created_at: "2026-08-03T12:00:00Z"
      })));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient();

    const messages = await engineeringApi.messages("project-1");

    expect(messages).toHaveLength(totalMessages);
    expect(messages.at(-1)?.id).toBe("message-1250");
    const urls = fetchMock.mock.calls.map(([input]) => new URL(input));
    expect(urls.map((url) => url.searchParams.get("since"))).toEqual([
      null,
      "message-500",
      "message-1000"
    ]);
    expect(urls.map((url) => url.searchParams.get("limit"))).toEqual(["500", "500", "500"]);
  });

  it("pages event replay with unique offsets until the final partial batch", async () => {
    const totalEvents = 2_450;
    const fetchMock = vi.fn(async (input: string) => {
      const url = new URL(input);
      const offset = Number(url.searchParams.get("offset"));
      const limit = Number(url.searchParams.get("limit"));
      const count = Math.min(limit, Math.max(0, totalEvents - offset));
      return responseJson(Array.from({ length: count }, (_, index) => ({
        id: `event-${offset + index + 1}`,
        schema_version: 1,
        seq: offset + index + 1,
        event_type: "task.created",
        project_id: "project-1",
        payload: {},
        occurred_at: "2026-08-03T12:00:00Z"
      })));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient();

    const events = await engineeringApi.events("project-1", 17);

    expect(events).toHaveLength(totalEvents);
    const urls = fetchMock.mock.calls.map(([input]) => new URL(input));
    expect(urls.map((url) => url.searchParams.get("offset"))).toEqual(["0", "1000", "2000"]);
    expect(urls.map((url) => url.searchParams.get("since_seq"))).toEqual(["17", "17", "17"]);
    expect(urls.map((url) => url.searchParams.get("limit"))).toEqual(["1000", "1000", "1000"]);
  });

  it("caps event replay at ten thousand records without repeating an offset", async () => {
    const fetchMock = vi.fn(async (input: string) => {
      const url = new URL(input);
      const offset = Number(url.searchParams.get("offset"));
      const limit = Number(url.searchParams.get("limit"));
      return responseJson(Array.from({ length: limit }, (_, index) => ({
        id: `event-${offset + index + 1}`,
        schema_version: 1,
        seq: offset + index + 1,
        event_type: "task.created",
        project_id: "project-1",
        payload: {},
        occurred_at: "2026-08-03T12:00:00Z"
      })));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient();

    const events = await engineeringApi.events("project-1");

    expect(events).toHaveLength(10_000);
    const offsets = fetchMock.mock.calls.map(([input]) => (
      new URL(input).searchParams.get("offset")
    ));
    expect(offsets).toEqual(["0", "1000", "2000", "3000", "4000", "5000", "6000", "7000", "8000", "9000"]);
    expect(new Set(offsets).size).toBe(offsets.length);
  });

  it("uses the persisted release candidate, artifact, and finalization routes", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      responseJson({ items: [], total: 0, limit: 200, offset: 0 })
    ));
    vi.stubGlobal("fetch", fetchMock);
    const { engineeringApi } = await loadClient();

    await engineeringApi.releaseReadiness("project-1");
    await engineeringApi.releaseCandidates("project-1");
    await engineeringApi.releaseArtifacts("candidate-1");
    await engineeringApi.createReleaseCandidate("project-1");
    await engineeringApi.generateReleaseArtifacts("candidate-1");
    await engineeringApi.finalizeRelease("candidate-1");

    const calls = fetchMock.mock.calls as Array<[string, RequestInit]>;
    expect(calls.map(([url]) => new URL(url).pathname)).toEqual([
      "/api/projects/project-1/release-readiness",
      "/api/projects/project-1/release-candidates",
      "/api/release-candidates/candidate-1/artifacts",
      "/api/projects/project-1/release-candidates",
      "/api/release-candidates/candidate-1/artifacts",
      "/api/release-candidates/candidate-1/finalize"
    ]);
    expect(calls.map(([, init]) => init.method || "GET")).toEqual([
      "GET", "GET", "GET", "POST", "POST", "POST"
    ]);
    expect(calls[3][1].body).toBe(JSON.stringify({ force: false }));
    expect(calls[5][1].body).toBe(JSON.stringify({ confirm: true }));
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
