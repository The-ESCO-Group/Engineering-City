import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "../routing";
import {
  Footprints,
  Gauge,
  ListTree,
  Map,
  Pause,
  Play,
  RotateCcw,
  X,
  ZoomIn,
  ZoomOut
} from "lucide-react";
import { useEngineeringData } from "../api/live";
import type { EventEnvelope } from "../api/types";
import { useUI } from "../store";
import type { AgentId } from "../world/ids";
import { buildCityAgentPresence } from "../world/cityAgentPresence";
import { CITY_LOCATIONS, type CityLocation } from "../world/locations";
import {
  selectWorldQuality,
  type WorldHardwareHints,
  type WorldQualityMode
} from "../world/quality";
import { deriveWorldReplayView } from "../world/replay";
import { AgentInspector } from "./AgentInspector";
import { CityDirectory } from "./CityDirectory";
import { CityHomeChrome, type CityHomeLayer, type CityViewMode } from "./CityHomeChrome";
import { CityLayerLegend } from "./CityLayerLegend";
import { CityMapHud } from "./CityMapHud";
import { ThreeCity, type CityLabelPositions } from "./ThreeCity";
import {
  WorldTimeline,
  type WorldTimelinePlaybackRate,
  type WorldTimelinePlaybackState,
  type WorldTimelineStreamStatus
} from "./WorldTimeline";
import { WorldQualityControl } from "./WorldQualityControl";
import { WorkflowAgentDock } from "./WorkflowAgentDock";

const WORLD_QUALITY_STORAGE_KEY = "esco.world-quality";
const TERMINAL_PROJECT_STATUSES = new Set(["draft", "completed", "failed", "archived"]);

function eventLabel(event: EventEnvelope) {
  const payload = event.payload;
  const detail = [payload.title, payload.name, payload.display_name]
    .find((value): value is string => typeof value === "string" && value.trim().length > 0);
  const action = event.event_type
    .split(".")
    .map((part) => part.replaceAll("_", " "))
    .join(" · ");
  return detail ? `${action} — ${detail}` : action;
}

function initialWorldQualityMode(): WorldQualityMode {
  if (typeof window === "undefined") return "auto";
  let stored: string | null = null;
  try {
    stored = window.localStorage.getItem(WORLD_QUALITY_STORAGE_KEY);
  } catch {
    return "auto";
  }
  return stored === "high" || stored === "balanced" || stored === "performance"
    ? stored
    : "auto";
}

function detectWorldHardware(): WorldHardwareHints {
  if (typeof navigator === "undefined" || typeof window === "undefined") return {};
  const hardwareNavigator = navigator as Navigator & { deviceMemory?: number };
  const logicalProcessors = navigator.hardwareConcurrency || null;
  const deviceMemoryGb = hardwareNavigator.deviceMemory ?? null;
  const mobile = window.matchMedia?.("(pointer: coarse)").matches ?? false;
  const constrained = mobile ||
    (logicalProcessors !== null && logicalProcessors < 4) ||
    (deviceMemoryGb !== null && deviceMemoryGb < 4);
  const capable = !mobile &&
    (logicalProcessors === null || logicalProcessors >= 8) &&
    (deviceMemoryGb === null || deviceMemoryGb >= 8);

  return {
    logicalProcessors,
    deviceMemoryGb,
    mobile,
    gpuTier: constrained ? "low" : capable ? "high" : "medium",
    powerPreference: constrained ? "low-power" : "high-performance"
  };
}

export function City() {
  const engineering = useEngineeringData();
  const [searchParams] = useSearchParams();
  const requestedLocationId = searchParams.get("location");
  const [viewResetKey, setViewResetKey] = useState(0);
  const [activeLayer, setActiveLayer] = useState<CityHomeLayer>("overview");
  const [viewMode, setViewMode] = useState<CityViewMode>("overview");
  const [labelPositions, setLabelPositions] = useState<CityLabelPositions>({});
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const [qualityOpen, setQualityOpen] = useState(false);
  const [qualityMode, setQualityMode] = useState<WorldQualityMode>(initialWorldQualityMode);
  const [worldFps, setWorldFps] = useState<number | null>(null);
  const [focusedLocation, setFocusedLocation] = useState<CityLocation | null>(null);
  const [focusRequestKey, setFocusRequestKey] = useState(0);
  const [selectionRequestKey, setSelectionRequestKey] = useState(0);
  const [playbackState, setPlaybackState] = useState<WorldTimelinePlaybackState>("live");
  const [timelineIndex, setTimelineIndex] = useState(-1);
  const [selectedAgentId, setSelectedAgentId] = useState<AgentId | null>(null);
  const directoryTriggerRef = useRef<HTMLButtonElement>(null);
  const directoryDialogRef = useRef<HTMLDivElement>(null);
  const qualityTriggerRef = useRef<HTMLButtonElement>(null);
  const appliedDeepLinkRef = useRef<string | null>(null);
  const updateLabelPositions = useCallback((positions: CityLabelPositions) => {
    setLabelPositions(positions);
  }, []);
  const { selectedBuilding, selectBuilding, reducedMotion, setReducedMotion, speed, setSpeed, zoom, setZoom } = useUI();
  const paused = playbackState === "paused";
  const hardwareHints = useMemo(detectWorldHardware, []);
  const qualitySelection = useMemo(
    () => selectWorldQuality(qualityMode, hardwareHints, reducedMotion),
    [hardwareHints, qualityMode, reducedMotion]
  );
  const updateWorldFps = useCallback((fps: number) => setWorldFps(fps), []);
  useEffect(() => setWorldFps(null), [qualitySelection.resolved]);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const syncPreference = (event: MediaQueryListEvent) => setReducedMotion(event.matches);
    media.addEventListener("change", syncPreference);
    return () => media.removeEventListener("change", syncPreference);
  }, [setReducedMotion]);
  const isWalking = viewMode === "first-person";
  const playbackRate: WorldTimelinePlaybackRate = speed === 0.5 || speed === 2 ? speed : 1;
  const projectId = engineering.projectId ?? engineering.project?.id ?? "unavailable-project";
  const orderedEvents = useMemo(
    () => [...engineering.events]
      .filter((event) => event.project_id === projectId)
      .sort((left, right) => left.seq - right.seq || left.id.localeCompare(right.id)),
    [engineering.events, projectId]
  );
  const lastTimelineIndex = orderedEvents.length - 1;
  useEffect(() => {
    if (playbackState === "live") setTimelineIndex(lastTimelineIndex);
    else setTimelineIndex((index) => Math.min(index, lastTimelineIndex));
  }, [lastTimelineIndex, playbackState]);
  useEffect(() => {
    if (playbackState !== "playing" || orderedEvents.length === 0) return;
    if (timelineIndex >= lastTimelineIndex) {
      setPlaybackState("live");
      return;
    }
    const timer = window.setTimeout(
      () => setTimelineIndex((index) => Math.min(index + 1, lastTimelineIndex)),
      900 / playbackRate
    );
    return () => window.clearTimeout(timer);
  }, [lastTimelineIndex, orderedEvents.length, playbackRate, playbackState, timelineIndex]);
  const replayView = useMemo(() => {
    const prefix = timelineIndex < 0 ? [] : orderedEvents.slice(0, timelineIndex + 1);
    const firstSequence = prefix[0]?.seq ?? 1;
    return deriveWorldReplayView(projectId, prefix, {
      afterSequence: Math.max(0, firstSequence - 1)
    });
  }, [orderedEvents, projectId, timelineIndex]);
  const projectActive = Boolean(
    engineering.project && !TERMINAL_PROJECT_STATUSES.has(engineering.project.status)
  );
  const cityAgents = useMemo(
    () => buildCityAgentPresence({
      replayAgents: replayView.agents,
      tasks: replayView.tasks,
      liveInstances: engineering.agents,
      projectId,
      projectActive,
      includeLivePresence: playbackState === "live",
      throughSequence: replayView.state.lastAppliedSequence
    }),
    [
      engineering.agents,
      playbackState,
      projectActive,
      projectId,
      replayView.agents,
      replayView.state.lastAppliedSequence,
      replayView.tasks
    ]
  );
  const timelinePoints = useMemo(
    () => orderedEvents.map((event) => ({
      sequence: event.seq,
      label: eventLabel(event),
      occurredAt: event.occurred_at
    })),
    [orderedEvents]
  );
  const streamStatus = useMemo<WorldTimelineStreamStatus>(() => {
    if (replayView.sync.status === "waiting_for_gap") {
      return {
        state: "gap",
        expectedSequence: replayView.sync.nextSequence,
        firstBufferedSequence: replayView.sync.firstBufferedSequence,
        detail: `${replayView.sync.bufferedCount} later event${replayView.sync.bufferedCount === 1 ? " is" : "s are"} buffered.`
      };
    }
    if (replayView.sync.status === "resync_required") {
      return { state: "stale", detail: replayView.sync.issue.message };
    }
    if (
      engineering.dataMode === "unavailable" ||
      engineering.connection === "offline" ||
      engineering.connection === "incompatible" ||
      engineering.connection === "authentication_required"
    ) {
      return { state: "offline", detail: engineering.fallbackReason };
    }
    if (engineering.connection === "connecting" || engineering.connection === "reconnecting") {
      return { state: "stale", detail: "The local event transport is reconnecting." };
    }
    if ((orderedEvents[0]?.seq ?? 1) > 1) {
      return {
        state: "stale",
        detail: `Replay begins at retained sequence ${orderedEvents[0].seq}; earlier events are outside this client window.`
      };
    }
    return { state: "ready" };
  }, [engineering.connection, engineering.dataMode, engineering.fallbackReason, orderedEvents, replayView.sync]);
  const liveActivityAgents = cityAgents.map((agent) => ({
    id: agent.id,
    name: agent.displayName,
    state: agent.status === "working" || agent.status === "walking" || agent.status === "reviewing"
      ? "active" as const
      : agent.status === "waiting" || agent.status === "blocked" || agent.status === "awaiting_approval"
        ? "waiting" as const
        : "complete" as const,
    task: agent.currentActivity || String(agent.locationId ?? "Ready")
  }));
  const activeAgentCount = liveActivityAgents.filter((agent) => agent.state === "active").length;
  const selectedAgent = cityAgents.find((agent) => agent.id === selectedAgentId) ?? null;
  const selectedAgentTask = selectedAgent?.taskId
    ? replayView.tasks.find((task) => task.id === selectedAgent.taskId) ?? null
    : null;
  const selectedLocation = selectedAgent?.locationId
    ? CITY_LOCATIONS.find((location) => location.id === selectedAgent.locationId)
    : null;
  const selectedAgentEvents = selectedAgent
    ? orderedEvents
      .slice(0, timelineIndex + 1)
      .filter((event) =>
        event.agent_id === selectedAgent.id || event.payload.agent_id === selectedAgent.id
      )
      .slice(-5)
      .reverse()
      .map((event) => ({
        id: event.id,
        label: eventLabel(event),
        detail: typeof event.payload.current_activity === "string"
          ? event.payload.current_activity
          : typeof event.payload.detail === "string"
            ? event.payload.detail
            : undefined,
        occurredAt: event.occurred_at
      }))
    : [];
  const citySummary = {
    activeAgents: activeAgentCount,
    openFindings: engineering.dataMode === "demo"
      ? engineering.project?.counts.findings_open || 0
      : engineering.findings.filter((finding) => finding.status === "open").length,
    pendingApprovals: engineering.dataMode === "demo"
      ? engineering.project?.counts.approvals_pending || 0
      : engineering.approvals.filter((approval) => approval.status === "pending").length,
    releaseReadiness: engineering.dataMode === "demo"
      ? 68
      : engineering.project?.status === "completed" ? 100 : null
  };
  const projectReference = engineering.project?.client_reference || "NO PROJECT";
  const projectName = engineering.project?.name || "Local project data unavailable";
  const changePlaybackState = (nextState: WorldTimelinePlaybackState) => {
    if (nextState === "live") setTimelineIndex(lastTimelineIndex);
    if (nextState === "playing" && timelineIndex >= lastTimelineIndex) setTimelineIndex(0);
    setPlaybackState(nextState);
  };
  const toggleTimelinePlayback = () => {
    changePlaybackState(paused ? "live" : "paused");
  };
  const resetCityView = () => {
    setZoom(1);
    setFocusedLocation(null);
    selectBuilding(null);
    setViewResetKey((key) => key + 1);
  };
  const changeViewMode = (nextViewMode: CityViewMode) => {
    if (nextViewMode === viewMode) return;
    setViewMode(nextViewMode);
    setDirectoryOpen(false);
    setQualityOpen(false);
    setFocusedLocation(null);
    selectBuilding(null);
    setViewResetKey((key) => key + 1);
  };
  const changeQualityMode = (nextMode: WorldQualityMode) => {
    setQualityMode(nextMode);
    try {
      window.localStorage.setItem(WORLD_QUALITY_STORAGE_KEY, nextMode);
    } catch {
      // The preference remains effective for this session when storage is unavailable.
    }
  };
  const selectWorldBuilding = (buildingId: Parameters<typeof selectBuilding>[0]) => {
    setFocusedLocation(null);
    setSelectionRequestKey((key) => key + 1);
    selectBuilding(buildingId);
  };
  const closeDirectory = (restoreFocus = true) => {
    setDirectoryOpen(false);
    if (restoreFocus) window.requestAnimationFrame(() => directoryTriggerRef.current?.focus());
  };
  const closeQuality = () => {
    setQualityOpen(false);
    window.requestAnimationFrame(() => qualityTriggerRef.current?.focus());
  };
  const focusSemanticLocation = (
    buildingId: Parameters<typeof selectBuilding>[0],
    location: CityLocation
  ) => {
    setFocusedLocation(location);
    setFocusRequestKey((key) => key + 1);
    selectBuilding(buildingId);
    closeDirectory(false);
  };
  useEffect(() => {
    if (!requestedLocationId) {
      appliedDeepLinkRef.current = null;
      return;
    }
    if (appliedDeepLinkRef.current === requestedLocationId) return;
    const location = CITY_LOCATIONS.find((candidate) => candidate.id === requestedLocationId);
    if (!location) return;
    appliedDeepLinkRef.current = requestedLocationId;
    setDirectoryOpen(false);
    setQualityOpen(false);
    setFocusedLocation(location);
    setFocusRequestKey((key) => key + 1);
    selectBuilding(location.focusBuildingId);
  }, [requestedLocationId, selectBuilding]);
  return (
    <section
      className={`city-panel${isWalking ? " is-first-person" : ""}`}
      aria-label="Engineering city workflow visualization"
    >
      <div className="city-toolbar">
        <div className="city-command-identity">
          <span className="eyebrow">{projectReference} · ENGINEERING WORLD</span>
          <h2>{projectName}</h2>
        </div>
        {isWalking ? (
          <button className="city-walk-exit" type="button" onClick={() => changeViewMode("overview")}>
            <Map aria-hidden="true" />
            <span>Exit walk</span>
            <small>Map view</small>
          </button>
        ) : (
          <div className="city-view-switch" role="group" aria-label="City view mode">
            <button
              type="button"
              className="is-active"
              aria-pressed="true"
              title="Flat-world map: drag to orbit 360°, scroll or pinch to zoom"
              onClick={() => changeViewMode("overview")}
            >
              <Map aria-hidden="true" />
              <span>Map</span>
            </button>
            <button
              type="button"
              aria-pressed="false"
              onClick={() => changeViewMode("first-person")}
            >
              <Footprints aria-hidden="true" />
              <span>Walk</span>
            </button>
          </div>
        )}
        <div className="city-command-demo" aria-label="Synthetic demonstration status">
          <span>{engineering.dataMode === "live" ? "Local" : engineering.dataMode === "demo" ? "Demo" : "Offline"}</span>
          <i aria-hidden="true" />
          <b>{paused ? "Workflow paused" : `${activeAgentCount} agents deployed`}</b>
        </div>
        <div className="tool-row">
          {!isWalking && (
            <button
              ref={directoryTriggerRef}
              className="icon-button city-directory-trigger"
              type="button"
              onClick={() => {
                setDirectoryOpen((open) => !open);
                setQualityOpen(false);
              }}
              aria-label="Open city directory"
              aria-controls="city-directory-dialog"
              aria-expanded={directoryOpen}
              title="Search semantic city destinations"
            >
              <ListTree />
            </button>
          )}
          {!isWalking && (
            <button
              ref={qualityTriggerRef}
              className="icon-button city-quality-trigger"
              type="button"
              onClick={() => {
                setQualityOpen((open) => !open);
                setDirectoryOpen(false);
              }}
              aria-label="World quality settings"
              aria-controls="world-quality-panel"
              aria-expanded={qualityOpen}
              title="World rendering quality"
            >
              <Gauge />
            </button>
          )}
          <button className="icon-button" onClick={toggleTimelinePlayback} aria-label={paused ? "Return timeline to live" : "Pause timeline"}>{paused ? <Play /> : <Pause />}</button>
          <button
            className="icon-button"
            onClick={() => setZoom(Math.max(0.82, zoom - 0.1))}
            disabled={isWalking || zoom <= 0.82}
            aria-label="Zoom map out"
            title={isWalking ? "Zoom is available in Map view" : "Zoom map out"}
          >
            <ZoomOut />
          </button>
          <button
            className="icon-button"
            onClick={() => setZoom(Math.min(1.28, zoom + 0.1))}
            disabled={isWalking || zoom >= 1.28}
            aria-label="Zoom map in"
            title={isWalking ? "Zoom is available in Map view" : "Zoom map in"}
          >
            <ZoomIn />
          </button>
          <button className="icon-button" onClick={resetCityView} aria-label={isWalking ? "Reset walk position" : "Reset city view"}><RotateCcw /></button>
        </div>
      </div>
      <div className={`city-map ${paused || reducedMotion ? "is-paused" : ""}`}>
        <ThreeCity
          selectedBuilding={selectedBuilding}
          selectBuilding={selectWorldBuilding}
          paused={paused}
          reducedMotion={reducedMotion}
          speed={speed}
          zoom={zoom}
          resetKey={viewResetKey}
          viewMode={viewMode}
          activeLayer={activeLayer}
          qualityProfile={qualitySelection.profile}
          selectionRequestKey={selectionRequestKey}
          focusLocationId={focusedLocation?.id ?? null}
          focusRequestKey={focusRequestKey}
          worldAgents={cityAgents}
          worldTasks={replayView.tasks}
          worldPackages={replayView.packages}
          selectedAgentId={selectedAgentId}
          onSelectAgent={setSelectedAgentId}
          onLabelPositionsChange={updateLabelPositions}
          onFpsSample={updateWorldFps}
        />
        {!isWalking && <CityLayerLegend activeLayer={activeLayer} />}
        {!isWalking && focusedLocation && (
          <div className="city-focused-location" role="status">
            <span>Focused destination</span>
            <strong>{focusedLocation.displayName}</strong>
            <small>{focusedLocation.code}</small>
          </div>
        )}
        {!isWalking && (
          <AgentInspector
            open={selectedAgent !== null}
            agent={selectedAgent}
            task={selectedAgentTask}
            project={engineering.project ? { id: engineering.project.id, name: engineering.project.name } : null}
            supplemental={{
              role: selectedAgent?.agentKind,
              locationName: selectedLocation?.displayName,
              blocker: selectedAgentTask?.status === "blocked" || selectedAgentTask?.status === "awaiting_approval"
                ? selectedAgentTask.detail || selectedAgentTask.sourceStatus
                : undefined,
              recentEvents: selectedAgentEvents,
              warnings: replayView.sync.status === "resync_required"
                ? [replayView.sync.issue.message]
                : undefined
            }}
            onClose={() => setSelectedAgentId(null)}
          />
        )}
        {!isWalking && (
          <WorkflowAgentDock
            agents={cityAgents}
            selectedAgentId={selectedAgentId}
            onSelectAgent={setSelectedAgentId}
          />
        )}
        {!isWalking && (
          <CityMapHud
            selectedBuilding={selectedBuilding}
            selectBuilding={selectWorldBuilding}
            projectLabel={`${projectReference} · ${projectName}`}
            dataMode={engineering.dataMode}
            summary={citySummary}
            labelDensity={qualitySelection.profile.labelDensity}
            labelPositions={labelPositions}
          />
        )}
        {!isWalking && qualityOpen && (
          <div
            id="world-quality-panel"
            className="world-quality-panel"
            onKeyDown={(event) => {
              if (event.key === "Escape") closeQuality();
            }}
          >
            <WorldQualityControl
              requested={qualitySelection.requested}
              resolved={qualitySelection.resolved}
              profile={qualitySelection.profile}
              fpsStatus={worldFps === null ? null : { currentFps: worldFps }}
              onChange={changeQualityMode}
              autoFocus
              className="is-city-hud"
            />
          </div>
        )}
        {!isWalking && directoryOpen && (
          <div
            className="city-directory-overlay"
            role="dialog"
            aria-modal="true"
            aria-label="City directory"
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                closeDirectory();
                return;
              }
              if (event.key !== "Tab") return;
              const focusable = Array.from(
                directoryDialogRef.current?.querySelectorAll<HTMLElement>(
                  "button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])"
                ) ?? []
              );
              const first = focusable[0];
              const last = focusable.at(-1);
              if (!first || !last) return;
              if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
              } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
              }
            }}
          >
            <div
              ref={directoryDialogRef}
              className="city-directory-dialog"
              id="city-directory-dialog"
            >
              <button
                className="city-directory-dialog__close"
                type="button"
                onClick={() => closeDirectory()}
                aria-label="Close city directory"
                autoFocus
              >
                <X />
              </button>
              <CityDirectory
                onFocusAnchor={focusSemanticLocation}
              />
            </div>
          </div>
        )}
        <CityHomeChrome
          activeLayer={activeLayer}
          onLayerChange={setActiveLayer}
          onZoomIn={() => setZoom(Math.min(1.28, zoom + 0.1))}
          onZoomOut={() => setZoom(Math.max(0.82, zoom - 0.1))}
          onLocate={resetCityView}
          viewMode={viewMode}
          paused={paused}
          dataMode={engineering.dataMode}
          activityAgents={liveActivityAgents}
          onSelectAgent={(agentId) => setSelectedAgentId(agentId as AgentId)}
          timeline={(
            <WorldTimeline
              className="world-timeline--city"
              points={timelinePoints}
              currentIndex={timelineIndex}
              playbackState={playbackState}
              playbackRate={playbackRate}
              streamStatus={streamStatus}
              disabled={!engineering.project}
              onPlaybackStateChange={changePlaybackState}
              onPlaybackRateChange={setSpeed}
              onScrub={({ index }) => {
                setTimelineIndex(index);
                setPlaybackState("paused");
              }}
            />
          )}
        />
      </div>
    </section>
  );
}
