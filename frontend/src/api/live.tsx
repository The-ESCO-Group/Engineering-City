/* eslint-disable react-refresh/only-export-components -- provider and its typed hooks form one API */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode
} from "react";
import { CONTRACT_VERSION, configuredProjectId, productFeatureFlags } from "../config";
import { ApiError, engineeringApi, projectSocketUrl } from "./client";
import {
  demoAgentInstances,
  demoEvents,
  demoProject,
  demoProjects,
  demoTasks,
  demoWorkflow
} from "./demo";
import type {
  AgentDefinitionRead,
  AgentInstanceRead,
  AgentMessageRead,
  AgentRunDetail,
  AgentRunRead,
  ApprovalRead,
  CapabilityStatus,
  ClarificationInbox,
  CurrentUserResponse,
  EventEnvelope,
  FindingDetail,
  FindingRead,
  HealthResponse,
  ParserCapability,
  ProjectDetail,
  ProjectRead,
  ProviderCapability,
  ExportArtifactRead,
  ReleaseCandidateRead,
  ReleaseReadinessRead,
  ReportRead,
  RequirementRead,
  TaskRead,
  UploadedFileRead,
  WebSocketHello,
  WorkflowStateResponse
} from "./types";

export type ConnectionState =
  | "demo"
  | "connecting"
  | "authentication_required"
  | "live"
  | "reconnecting"
  | "offline"
  | "incompatible";

export type DataMode = "demo" | "live" | "unavailable";

export interface EngineeringData {
  projectId: string | null;
  project: ProjectDetail | null;
  projects: ProjectRead[];
  projectTotal: number;
  agentDefinitions: AgentDefinitionRead[];
  agents: AgentInstanceRead[];
  agentRuns: AgentRunRead[];
  messages: AgentMessageRead[];
  findings: FindingRead[];
  approvals: ApprovalRead[];
  reports: ReportRead[];
  parsers: ParserCapability[];
  providers: ProviderCapability[];
  capabilities: CapabilityStatus[];
  clarifications: ClarificationInbox | null;
  requirements: RequirementRead[];
  releaseReadiness: ReleaseReadinessRead | null;
  releaseCandidates: ReleaseCandidateRead[];
  releaseArtifacts: ExportArtifactRead[];
  files: UploadedFileRead[];
  tasks: TaskRead[];
  workflow: WorkflowStateResponse | null;
  events: EventEnvelope[];
  health: HealthResponse | null;
  currentUser: CurrentUserResponse | null;
  dataMode: DataMode;
  isLive: boolean;
  backendAvailable: boolean;
  contractCompatible: boolean;
  connection: ConnectionState;
  fallbackReason: string;
  authBusy: boolean;
  authError: string;
  selectProject: (projectId: string) => void;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const EMPTY_DATA: EngineeringData = {
  projectId: null,
  project: null,
  projects: [],
  projectTotal: 0,
  agentDefinitions: [],
  agents: [],
  agentRuns: [],
  messages: [],
  findings: [],
  approvals: [],
  reports: [],
  parsers: [],
  providers: [],
  capabilities: [],
  clarifications: null,
  requirements: [],
  releaseReadiness: null,
  releaseCandidates: [],
  releaseArtifacts: [],
  files: [],
  tasks: [],
  workflow: null,
  events: [],
  health: null,
  currentUser: null,
  dataMode: "demo",
  isLive: false,
  backendAvailable: false,
  contractCompatible: true,
  connection: "demo",
  fallbackReason: "Synthetic demo mode is active.",
  authBusy: false,
  authError: "",
  selectProject: () => undefined,
  login: async () => undefined,
  logout: async () => undefined,
  refresh: async () => undefined
};

const EngineeringDataContext = createContext<EngineeringData>(EMPTY_DATA);
const EVENT_HISTORY_LIMIT = 10_000;

export function retainEventHistory(events: EventEnvelope[]): EventEnvelope[] {
  return [...events]
    .sort((left, right) => left.seq - right.seq)
    .slice(-EVENT_HISTORY_LIMIT);
}

export interface ContiguousEventMerge {
  cursor: number;
  pending: Map<number, EventEnvelope>;
  committed: EventEnvelope[];
}

export function mergeContiguousEvents(
  cursor: number,
  pendingEvents: ReadonlyMap<number, EventEnvelope>,
  incomingEvents: readonly EventEnvelope[]
): ContiguousEventMerge {
  const pending = new Map(pendingEvents);
  incomingEvents.forEach((event) => {
    if (event.seq > cursor) pending.set(event.seq, event);
  });

  const committed: EventEnvelope[] = [];
  while (pending.has(cursor + 1)) {
    const next = pending.get(cursor + 1)!;
    pending.delete(cursor + 1);
    committed.push(next);
    cursor = next.seq;
  }

  return { cursor, pending, committed };
}

export function deriveContiguousEventCursor(events: readonly EventEnvelope[]): number {
  const retained = retainEventHistory([...events]);
  if (retained.length === 0) return 0;

  // A full retained window has intentionally discarded its older prefix. A
  // shorter window has not, so it must still begin at sequence one before its
  // high-water mark is safe to resume from.
  let cursor = retained.length === EVENT_HISTORY_LIMIT ? retained[0].seq - 1 : 0;
  for (const event of retained) {
    if (event.seq <= cursor) continue;
    if (event.seq !== cursor + 1) break;
    cursor = event.seq;
  }
  return cursor;
}

function eventSequenceKey(projectId: string): string {
  return `esco:event-seq:${projectId}`;
}

function eventHistoryKey(projectId: string): string {
  return `esco:event-history:${projectId}`;
}

export function persistContiguousEventHistory(
  storage: Pick<Storage, "setItem">,
  projectId: string,
  events: readonly EventEnvelope[],
  cursor: number
): boolean {
  try {
    // History is the recoverable source of truth. Write it before the cursor so
    // a quota failure causes harmless duplicate replay after restart rather
    // than a cursor that skips events the client never retained.
    storage.setItem(eventHistoryKey(projectId), JSON.stringify(events));
    storage.setItem(eventSequenceKey(projectId), String(cursor));
    return true;
  } catch {
    return false;
  }
}

function readEventHistory(projectId: string): EventEnvelope[] {
  try {
    const value = JSON.parse(window.localStorage.getItem(eventHistoryKey(projectId)) || "[]");
    return Array.isArray(value) ? retainEventHistory(value as EventEnvelope[]) : [];
  } catch {
    return [];
  }
}

function invalidateForEvent(
  event: EventEnvelope,
  projectId: string,
  invalidate: (key: readonly unknown[]) => void
) {
  const prefix = event.event_type.split(".")[0];
  if (prefix === "agent") {
    invalidate(["engineering", projectId, "agents"]);
    invalidate(["engineering", projectId, "agent-runs"]);
    invalidate(["engineering", projectId, "messages"]);
  } else if (prefix === "finding") {
    invalidate(["engineering", projectId, "findings"]);
    const findingId = event.payload.finding_id;
    if (typeof findingId === "string") invalidate(["engineering", "finding", findingId]);
  } else if (prefix === "approval") {
    invalidate(["engineering", projectId, "approvals"]);
    invalidate(["engineering", projectId, "findings"]);
  } else if (prefix === "report") {
    invalidate(["engineering", projectId, "reports"]);
  } else if (["release", "export", "validation", "gate"].includes(prefix)) {
    invalidate(["engineering", projectId, "release-readiness"]);
    invalidate(["engineering", projectId, "release-candidates"]);
    invalidate(["engineering", projectId, "release-artifacts"]);
  } else if (prefix === "task" || prefix === "workflow") {
    invalidate(["engineering", projectId, "tasks"]);
    invalidate(["engineering", projectId, "agent-runs"]);
    invalidate(["engineering", projectId, "workflow"]);
    invalidate(["engineering", projectId, "project"]);
  } else {
    invalidate(["engineering", projectId, "project"]);
  }
}

export function EngineeringDataProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const flags = productFeatureFlags();
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(() =>
    flags.liveBackend ? configuredProjectId() : null
  );
  const [socketConnection, setSocketConnection] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<EventEnvelope[]>([]);

  const healthQuery = useQuery({
    queryKey: ["engineering", "health"],
    queryFn: engineeringApi.health,
    enabled: flags.liveBackend,
    retry: 1,
    refetchInterval: 15_000,
    refetchOnWindowFocus: true
  });
  const backendAvailable = Boolean(healthQuery.data);
  const contractCompatible =
    !healthQuery.data || healthQuery.data.contract_version === CONTRACT_VERSION;

  const meQuery = useQuery({
    queryKey: ["engineering", "me"],
    queryFn: engineeringApi.me,
    enabled: flags.liveBackend && backendAvailable && contractCompatible,
    retry: false
  });
  const authenticated = Boolean(meQuery.data?.user);

  const projectsQuery = useQuery({
    queryKey: ["engineering", "projects"],
    queryFn: engineeringApi.projects,
    enabled: authenticated,
    retry: false,
    refetchOnWindowFocus: false
  });

  const projectId = selectedProjectId || projectsQuery.data?.items[0]?.id || null;
  const enabled = authenticated && Boolean(projectId);

  useEffect(() => {
    if (!projectId) return;
    window.localStorage.setItem("esco:selected-project", projectId);
    if (selectedProjectId !== projectId) setSelectedProjectId(projectId);
  }, [projectId, selectedProjectId]);

  const projectQuery = useQuery({
    queryKey: ["engineering", projectId, "project"],
    queryFn: () => engineeringApi.project(projectId!),
    enabled,
    retry: false
  });
  const agentDefinitionsQuery = useQuery({
    queryKey: ["engineering", "agent-definitions"],
    queryFn: engineeringApi.agentDefinitions,
    enabled: authenticated,
    retry: false
  });
  const agentsQuery = useQuery({
    queryKey: ["engineering", projectId, "agents"],
    queryFn: () => engineeringApi.agents(projectId!),
    enabled,
    retry: false
  });
  const agentRunsQuery = useQuery({
    queryKey: ["engineering", projectId, "agent-runs"],
    queryFn: () => engineeringApi.agentRuns(projectId!),
    enabled,
    retry: false
  });
  const messagesQuery = useQuery({
    queryKey: ["engineering", projectId, "messages"],
    queryFn: () => engineeringApi.messages(projectId!),
    enabled,
    retry: false
  });
  const findingsQuery = useQuery({
    queryKey: ["engineering", projectId, "findings"],
    queryFn: () => engineeringApi.findings(projectId!),
    enabled,
    retry: false
  });
  const approvalsQuery = useQuery({
    queryKey: ["engineering", projectId, "approvals"],
    queryFn: () => engineeringApi.approvals(projectId!),
    enabled,
    retry: false
  });
  const clarificationsQuery = useQuery({
    queryKey: ["engineering", projectId, "clarifications"],
    queryFn: () => engineeringApi.clarifications(projectId!),
    enabled,
    retry: false
  });
  const requirementsQuery = useQuery({
    queryKey: ["engineering", projectId, "requirements"],
    queryFn: () => engineeringApi.requirements(projectId!),
    enabled,
    retry: false
  });
  const releaseReadinessQuery = useQuery({
    queryKey: ["engineering", projectId, "release-readiness"],
    queryFn: () => engineeringApi.releaseReadiness(projectId!),
    enabled,
    retry: false
  });
  const releaseCandidatesQuery = useQuery({
    queryKey: ["engineering", projectId, "release-candidates"],
    queryFn: () => engineeringApi.releaseCandidates(projectId!),
    enabled,
    retry: false
  });
  const currentReleaseCandidateId = releaseCandidatesQuery.data?.items[0]?.id ?? null;
  const releaseArtifactsQuery = useQuery({
    queryKey: ["engineering", projectId, "release-artifacts", currentReleaseCandidateId],
    queryFn: () => engineeringApi.releaseArtifacts(currentReleaseCandidateId!),
    enabled: enabled && Boolean(currentReleaseCandidateId),
    retry: false
  });
  const reportsQuery = useQuery({
    queryKey: ["engineering", projectId, "reports"],
    queryFn: () => engineeringApi.reports(projectId!),
    enabled,
    retry: false
  });
  const parsersQuery = useQuery({
    queryKey: ["engineering", projectId, "parsers"],
    queryFn: () => engineeringApi.parsers(projectId!),
    enabled,
    retry: false
  });
  const providersQuery = useQuery({
    queryKey: ["engineering", "providers"],
    queryFn: engineeringApi.providers,
    enabled: authenticated,
    retry: false
  });
  const capabilitiesQuery = useQuery({
    queryKey: ["engineering", "capabilities"],
    queryFn: engineeringApi.capabilities,
    enabled: backendAvailable,
    retry: false
  });
  const filesQuery = useQuery({
    queryKey: ["engineering", projectId, "files"],
    queryFn: () => engineeringApi.files(projectId!),
    enabled,
    retry: false
  });
  const tasksQuery = useQuery({
    queryKey: ["engineering", projectId, "tasks"],
    queryFn: () => engineeringApi.tasks(projectId!),
    enabled,
    retry: false
  });
  const workflowQuery = useQuery({
    queryKey: ["engineering", projectId, "workflow"],
    queryFn: () => engineeringApi.workflow(projectId!),
    enabled,
    retry: false
  });

  const refresh = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ["engineering"] });
  }, [queryClient]);

  const loginMutation = useMutation({
    mutationFn: ({ email, password }: { email: string; password: string }) =>
      engineeringApi.login(email, password),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["engineering", "me"] });
      await queryClient.invalidateQueries({ queryKey: ["engineering", "projects"] });
    }
  });
  const logoutMutation = useMutation({
    mutationFn: engineeringApi.logout,
    onSuccess: async () => {
      setSelectedProjectId(null);
      setEvents([]);
      window.localStorage.removeItem("esco:selected-project");
      queryClient.removeQueries({ queryKey: ["engineering"] });
      await queryClient.invalidateQueries({ queryKey: ["engineering", "health"] });
    }
  });

  useEffect(() => {
    if (!projectId || !projectQuery.data) {
      setEvents([]);
      return;
    }
    const restoredHistory = readEventHistory(projectId);
    setEvents(restoredHistory);

    let reconnectTimer = 0;
    let pollTimer = 0;
    let socket: WebSocket | null = null;
    let stopped = false;
    let reconnectAttempt = 0;
    let catchUpActive = false;
    let currentSequence = deriveContiguousEventCursor(restoredHistory);
    let pendingEvents = new Map(
      restoredHistory
        .filter((event) => event.seq > currentSequence)
        .map((event) => [event.seq, event] as const)
    );

    const invalidate = (key: readonly unknown[]) => {
      void queryClient.invalidateQueries({ queryKey: key });
    };
    const refreshProject = () => {
      void queryClient.invalidateQueries({ queryKey: ["engineering", projectId] });
    };
    const storeEvents = (incomingEvents: readonly EventEnvelope[]) => {
      const merged = mergeContiguousEvents(currentSequence, pendingEvents, incomingEvents);
      pendingEvents = merged.pending;
      if (merged.committed.length === 0) return;
      currentSequence = merged.cursor;
      const committedIds = new Set(merged.committed.map((event) => event.id));
      const committedSequences = new Set(merged.committed.map((event) => event.seq));
      setEvents((previous) => {
        const next = retainEventHistory([
          ...previous.filter((item) => !committedIds.has(item.id) && !committedSequences.has(item.seq)),
          ...merged.committed
        ]);
        persistContiguousEventHistory(window.localStorage, projectId, next, merged.cursor);
        return next;
      });
      merged.committed.forEach((event) => invalidateForEvent(event, projectId, invalidate));
    };
    const catchUp = async () => {
      if (catchUpActive || stopped) return;
      catchUpActive = true;
      try {
        const replay = await engineeringApi.events(projectId, currentSequence);
        storeEvents(replay.sort((left, right) => left.seq - right.seq));
      } catch {
        setSocketConnection((state) => (state === "live" ? "reconnecting" : state));
      } finally {
        catchUpActive = false;
      }
    };
    const connect = () => {
      if (stopped) return;
      setSocketConnection(reconnectAttempt > 0 ? "reconnecting" : "connecting");
      socket = new WebSocket(projectSocketUrl(projectId, currentSequence));
      socket.onopen = () => {
        reconnectAttempt = 0;
        setSocketConnection("live");
      };
      socket.onmessage = (message) => {
        try {
          const frame = JSON.parse(String(message.data)) as
            | EventEnvelope
            | WebSocketHello
            | { type: string };
          if ("type" in frame) {
            if (frame.type === "hello" && (frame as WebSocketHello).replay_truncated) {
              refreshProject();
              void catchUp();
            }
            return;
          }
          const gapDetected = frame.seq > currentSequence + 1;
          storeEvents([frame]);
          if (gapDetected) void catchUp();
        } catch {
          setSocketConnection("reconnecting");
          void catchUp();
        }
      };
      socket.onclose = () => {
        if (stopped) return;
        setSocketConnection("reconnecting");
        reconnectAttempt += 1;
        const delay = Math.min(30_000, 1_000 * 2 ** Math.min(reconnectAttempt, 5));
        reconnectTimer = window.setTimeout(connect, delay);
      };
      socket.onerror = () => socket?.close();
    };

    connect();
    pollTimer = window.setInterval(() => void catchUp(), 12_000);
    return () => {
      stopped = true;
      window.clearTimeout(reconnectTimer);
      window.clearInterval(pollTimer);
      socket?.close();
    };
  }, [projectId, projectQuery.data, queryClient]);

  const isLive = Boolean(authenticated && projectId && projectQuery.data);
  const dataMode: DataMode = !flags.liveBackend ? "demo" : isLive ? "live" : "unavailable";
  let connection: ConnectionState;
  if (!flags.liveBackend) connection = "demo";
  else if (healthQuery.isPending) connection = "connecting";
  else if (healthQuery.isError || !healthQuery.data) connection = "offline";
  else if (!contractCompatible) connection = "incompatible";
  else if (meQuery.isPending) connection = "connecting";
  else if (!authenticated) connection = "authentication_required";
  else connection = isLive ? socketConnection : "live";

  let fallbackReason = "Synthetic demo data is active.";
  if (!flags.liveBackend) fallbackReason = "Demo mode was explicitly requested for this session.";
  else if (connection === "offline") fallbackReason = "The local backend is not reachable; no cloud fallback was attempted.";
  else if (connection === "incompatible") {
    fallbackReason = `Backend contract ${healthQuery.data?.contract_version || "unknown"} does not match client contract ${CONTRACT_VERSION}.`;
  } else if (connection === "authentication_required") {
    fallbackReason = "Sign in to the local backend to replace synthetic data with an authorized project.";
  } else if (authenticated && !projectId) {
    fallbackReason = "The local account has no accessible project yet.";
  }

  const login = useCallback(async (email: string, password: string) => {
    await loginMutation.mutateAsync({ email, password });
  }, [loginMutation]);
  const logout = useCallback(async () => {
    await logoutMutation.mutateAsync();
  }, [logoutMutation]);
  const selectProject = useCallback((nextProjectId: string) => {
    setSelectedProjectId(nextProjectId);
  }, []);

  const value = useMemo<EngineeringData>(
    () => ({
      projectId: dataMode === "demo" ? demoProject.id : projectId,
      project: dataMode === "demo" ? demoProject : projectQuery.data || null,
      projects: dataMode === "demo" ? demoProjects : projectsQuery.data?.items || [],
      projectTotal: dataMode === "demo" ? demoProjects.length : projectsQuery.data?.total || 0,
      agentDefinitions: dataMode === "demo" ? [] : agentDefinitionsQuery.data || [],
      agents: dataMode === "demo" ? demoAgentInstances : agentsQuery.data || [],
      agentRuns: dataMode === "demo" ? [] : agentRunsQuery.data || [],
      messages: messagesQuery.data || [],
      findings: findingsQuery.data?.items || [],
      approvals: approvalsQuery.data || [],
      reports: reportsQuery.data || [],
      parsers: parsersQuery.data || [],
      providers: providersQuery.data || [],
      capabilities: capabilitiesQuery.data || healthQuery.data?.capabilities || [],
      clarifications: clarificationsQuery.data || null,
      requirements: requirementsQuery.data || [],
      releaseReadiness: releaseReadinessQuery.data || null,
      releaseCandidates: releaseCandidatesQuery.data?.items || [],
      releaseArtifacts: releaseArtifactsQuery.data?.items || [],
      files: filesQuery.data || [],
      tasks: dataMode === "demo" ? demoTasks : tasksQuery.data || [],
      workflow: dataMode === "demo" ? demoWorkflow : workflowQuery.data || null,
      events: dataMode === "demo" ? demoEvents : events,
      health: healthQuery.data || null,
      currentUser: meQuery.data || null,
      dataMode,
      isLive,
      backendAvailable,
      contractCompatible,
      connection,
      fallbackReason,
      authBusy: loginMutation.isPending || logoutMutation.isPending,
      authError: loginMutation.error instanceof Error ? loginMutation.error.message : "",
      selectProject,
      login,
      logout,
      refresh
    }),
    [
      projectId,
      projectQuery.data,
      projectsQuery.data,
      agentDefinitionsQuery.data,
      agentsQuery.data,
      agentRunsQuery.data,
      messagesQuery.data,
      findingsQuery.data,
      approvalsQuery.data,
      reportsQuery.data,
      parsersQuery.data,
      providersQuery.data,
      capabilitiesQuery.data,
      clarificationsQuery.data,
      requirementsQuery.data,
      releaseReadinessQuery.data,
      releaseCandidatesQuery.data,
      releaseArtifactsQuery.data,
      filesQuery.data,
      tasksQuery.data,
      workflowQuery.data,
      events,
      healthQuery.data,
      meQuery.data,
      dataMode,
      isLive,
      backendAvailable,
      contractCompatible,
      connection,
      fallbackReason,
      loginMutation.isPending,
      loginMutation.error,
      logoutMutation.isPending,
      selectProject,
      login,
      logout,
      refresh
    ]
  );

  return <EngineeringDataContext.Provider value={value}>{children}</EngineeringDataContext.Provider>;
}

export function useEngineeringData(): EngineeringData {
  return useContext(EngineeringDataContext);
}

export function useFindingDetail(findingId: string | null, enabled: boolean) {
  return useQuery<FindingDetail>({
    queryKey: ["engineering", "finding", findingId],
    queryFn: () => engineeringApi.finding(findingId!),
    enabled: enabled && Boolean(findingId),
    retry: false
  });
}

export function useAgentRunDetail(runId: string | null, enabled: boolean) {
  return useQuery<AgentRunDetail>({
    queryKey: ["engineering", "agent-run", runId],
    queryFn: () => engineeringApi.agentRun(runId!),
    enabled: enabled && Boolean(runId),
    retry: false
  });
}

export function useApprovalDecision(projectId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ approvalId, approved, notes }: { approvalId: string; approved: boolean; notes: string }) =>
      engineeringApi.decideApproval(approvalId, approved, notes),
    onSuccess: () => {
      if (projectId) {
        void queryClient.invalidateQueries({ queryKey: ["engineering", projectId, "approvals"] });
        void queryClient.invalidateQueries({ queryKey: ["engineering", projectId, "findings"] });
      }
    }
  });
}

export { ApiError };
