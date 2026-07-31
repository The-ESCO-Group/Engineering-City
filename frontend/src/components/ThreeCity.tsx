import { useEffect, useRef, useState, type CSSProperties } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { buildings, type BuildingId } from "../domain";
import type { AgentId, LocationId, PackageId } from "../world/ids";
import { CITY_LOCATIONS } from "../world/locations";
import {
  WORLD_QUALITY_PROFILES,
  type WorldQualityProfile
} from "../world/quality";
import {
  markerStackOffset,
  planSemanticRoute,
  pointAlongRoute,
  routeLength,
  type WorldPoint
} from "../world/routes";
import {
  getSceneLocationNode,
  type SceneLocationNode
} from "../world/sceneLocations";
import type { WorldAgent, WorldPackage, WorldTask } from "../world/types";
import type { CityHomeLayer } from "./CityHomeChrome";

export interface ThreeCityProps {
  selectedBuilding: BuildingId | null;
  selectBuilding: (id: BuildingId) => void;
  paused: boolean;
  reducedMotion: boolean;
  speed: number;
  zoom: number;
  resetKey: number;
  viewMode: "overview" | "first-person";
  activeLayer?: CityHomeLayer;
  qualityProfile?: WorldQualityProfile;
  selectionRequestKey?: number;
  focusLocationId?: LocationId | null;
  focusRequestKey?: number;
  worldAgents?: readonly WorldAgent[];
  worldTasks?: readonly WorldTask[];
  worldPackages?: readonly WorldPackage[];
  selectedAgentId?: AgentId | null;
  onSelectAgent?: (agentId: AgentId) => void;
  onLabelPositionsChange?: (positions: CityLabelPositions) => void;
}

export interface CityLabelProjection {
  deltaX: number;
  deltaY: number;
  visible: boolean;
}

export type CityLabelPositions = Record<string, CityLabelProjection>;

type Vec3 = readonly [number, number, number];
type Point2 = readonly [number, number];
type DistrictAccent = "cyan" | "orange" | "purple";
type CityOverlayLayer = Exclude<CityHomeLayer, "overview">;

interface AnchorSpec {
  id: BuildingId;
  position: Point2;
  lot: Point2;
}

interface DistrictSpec {
  label: string;
  center: Point2;
  size: Point2;
  accent: DistrictAccent;
  density: number;
  minHeight: number;
  maxHeight: number;
  skyline: number;
  seed: number;
}

interface RectangularSite {
  center: Point2;
  size: Point2;
}

interface MaterialPalette {
  white: THREE.MeshStandardMaterial;
  light: THREE.MeshStandardMaterial;
  concrete: THREE.MeshStandardMaterial;
  roof: THREE.MeshStandardMaterial;
  glassCyan: THREE.MeshPhysicalMaterial;
  glassOrange: THREE.MeshPhysicalMaterial;
  glassPurple: THREE.MeshPhysicalMaterial;
  blackSteel: THREE.MeshStandardMaterial;
  asphalt: THREE.MeshStandardMaterial;
  water: THREE.MeshPhysicalMaterial;
  contact: THREE.MeshBasicMaterial;
  park: THREE.MeshStandardMaterial;
  foliage: THREE.MeshStandardMaterial;
  cyanEmissive: THREE.MeshStandardMaterial;
  orangeEmissive: THREE.MeshStandardMaterial;
  cyanGlow: THREE.MeshBasicMaterial;
  orangeGlow: THREE.MeshBasicMaterial;
  purpleGlow: THREE.MeshBasicMaterial;
  greenGlow: THREE.MeshBasicMaterial;
  mobilityGlow: THREE.MeshBasicMaterial;
  agentOrange: THREE.MeshStandardMaterial;
}

interface BuildingRuntime {
  accent: THREE.MeshStandardMaterial;
  selection: THREE.MeshBasicMaterial;
  ambientAccent: number;
}

interface AgentRuntime {
  id: AgentId;
  root: THREE.Group;
  halo: THREE.Mesh<THREE.TorusGeometry, THREE.MeshBasicMaterial>;
  coreMaterial: THREE.MeshStandardMaterial;
  workPackage: THREE.Mesh<THREE.BoxGeometry, THREE.MeshStandardMaterial>;
  baseY: number;
  phase: number;
  destinationLocationId: LocationId;
  lastSequence: number;
  route: readonly WorldPoint[];
  routeLength: number;
  routeTravelled: number;
  destination: WorldPoint;
}

interface PackageRuntime {
  id: PackageId;
  root: THREE.Group;
  coreMaterial: THREE.MeshStandardMaterial;
  halo: THREE.Mesh<THREE.TorusGeometry, THREE.MeshBasicMaterial>;
  baseY: number;
  phase: number;
  destinationLocationId: LocationId;
  lastSequence: number;
  route: readonly WorldPoint[];
  routeLength: number;
  routeTravelled: number;
  destination: WorldPoint;
}

interface LinearMotion {
  from: Point2;
  to: Point2;
  length: number;
  progress: number;
  speed: number;
  direction: 1 | -1;
  laneOffset: number;
  surfaceY: number;
}

interface VehicleRuntime {
  body: THREE.InstancedMesh;
  cabin: THREE.InstancedMesh;
  lamps: THREE.InstancedMesh;
  motions: LinearMotion[];
  dummy: THREE.Object3D;
}

interface PedestrianMotion {
  center: Point2;
  radius: number;
  angle: number;
  speed: number;
  phase: number;
  groundY: number;
  idle: boolean;
}

interface PedestrianRuntime {
  bodies: THREE.InstancedMesh;
  heads: THREE.InstancedMesh;
  motions: PedestrianMotion[];
  dummy: THREE.Object3D;
}

interface PulseMotion extends LinearMotion {
  scale: Vec3;
}

interface PulseRuntime {
  mesh: THREE.InstancedMesh;
  motions: PulseMotion[];
  dummy: THREE.Object3D;
}

interface SignalSpec {
  position: Vec3;
  phase: number;
}

interface SignalRuntime {
  mesh: THREE.InstancedMesh;
  signals: SignalSpec[];
  dummy: THREE.Object3D;
}

interface LayerSceneRuntime {
  scene: THREE.Scene;
  overlays: Record<CityOverlayLayer, THREE.Group>;
  vehicleLife: VehicleRuntime;
  pedestrianLife: PedestrianRuntime;
  waterPulses: PulseRuntime;
  routePulses: PulseRuntime;
  rooftopSignals: SignalRuntime;
}

interface CameraFlight {
  startedAt: number;
  duration: number;
  fromPosition: THREE.Vector3;
  toPosition: THREE.Vector3;
  fromTarget: THREE.Vector3;
  toTarget: THREE.Vector3;
}

interface OverviewCameraSnapshot {
  position: THREE.Vector3;
  target: THREE.Vector3;
  zoom: number;
}

interface RoadSegment {
  from: Point2;
  to: Point2;
  width: number;
}

interface FirstPersonLook {
  yaw: number;
  pitch: number;
}

const HUB: Point2 = [-16, 9];
const LANDMARK: Point2 = [-17, -37];
const PARK: Point2 = [14, 65];
const OVERVIEW_POSITION: Vec3 = [60, 184, 270];
const OVERVIEW_TARGET: Vec3 = [0, 0, 30];
const OVERVIEW_FOV = 44;
const FIRST_PERSON_START: Vec3 = [38, 1.86, 78];
const FIRST_PERSON_TARGET: Vec3 = [-16, 3.1, 9];
const FIRST_PERSON_BOUNDS = {
  minX: -216,
  maxX: 174,
  minZ: -182,
  maxZ: 132
};
const MOVEMENT_KEYS = new Set([
  "KeyW",
  "KeyA",
  "KeyS",
  "KeyD",
  "ArrowUp",
  "ArrowLeft",
  "ArrowDown",
  "ArrowRight",
  "ShiftLeft",
  "ShiftRight"
]);
const CITY_BOUNDS = {
  minX: -214,
  maxX: 172,
  minZ: -178,
  maxZ: 130
};
const METRO_VERTICAL_STREETS = [
  -208, -190, -172, -154, -136, -118, -100, -82, -64, -46, -28, -10, 8, 26, 44,
  62, 80, 98, 116, 134, 152, 170
];
const METRO_HORIZONTAL_STREETS = [
  -174, -158, -142, -126, -110, -94, -78, -62, -46, -30, -14, 2, 18, 34, 50,
  66, 82, 98, 114, 130
];

const WATER_BANDS: ReadonlyArray<RectangularSite> = [
  { center: [-18, -132], size: [402, 12.5] },
  { center: [-18, -89], size: [402, 11.5] },
  { center: [-12, -28], size: [390, 9] },
  { center: [116, -73], size: [11, 178] },
  { center: [-154, 18], size: [10, 190] },
  { center: [28, 122], size: [316, 18] }
];

const FEATURE_SITES: ReadonlyArray<RectangularSite> = [
  { center: [-191, -98], size: [27, 18] },
  { center: [-106, 121], size: [30, 24] },
  { center: [-76, 123], size: [30, 20] },
  { center: [-124, -98], size: [25, 18] },
  { center: [29, -165], size: [31, 18] },
  { center: [124, -168], size: [31, 22] },
  { center: [-32, 124], size: [27, 18] },
  { center: [30, 119], size: [30, 18] },
  { center: [62, 86], size: [31, 20] },
  { center: [131, 52], size: [29, 20] },
  { center: [96, 126], size: [65, 18] }
];

const ANCHOR_SPECS: AnchorSpec[] = [
  { id: "intake", position: [-95, -9], lot: [12, 9] },
  { id: "automation", position: [-53, -44], lot: [11, 10] },
  { id: "hmi", position: [39, -57], lot: [11, 9] },
  { id: "electrical", position: [85, -36], lot: [14, 10] },
  { id: "qa", position: [46, 21], lot: [13, 10] },
  { id: "documentation", position: [-24, 56], lot: [11, 9] },
  { id: "approval", position: [-80, 36], lot: [12, 9] },
  { id: "release", position: [78, 40], lot: [15, 11] }
];

const CITY_LABEL_ANCHORS: ReadonlyArray<{
  key: string;
  position: readonly [number, number, number];
}> = [
  ...ANCHOR_SPECS.map(({ id, position }) => ({
    key: id,
    position: [position[0], 5.5, position[1]] as const
  })),
  { key: "North Gateway", position: [-191, 7, -98] },
  { key: "Research Park", position: [-124, 3, -98] },
  { key: "Innovation District", position: [-67, 5, -145] },
  { key: "Energy Plant", position: [29, 4, -165] },
  { key: "Water Treatment", position: [124, 3, -168] },
  { key: "Industrial Zone", position: [134, 5, -73] },
  { key: "South Gateway", position: [149, 5, -8] },
  { key: "Central Park", position: [PARK[0], 2, PARK[1]] },
  { key: "Maintenance Depot", position: [131, 3, 52] },
  { key: "Design District", position: [-62, 4, 104] },
  { key: "Training Academy", position: [-24, 3, 94] },
  { key: "Supply Chain Hub", position: [62, 4, 86] },
  { key: "Wind Farm", position: [-106, 4, 121] },
  { key: "Testing Grounds", position: [-32, 3, 124] },
  { key: "Data Center", position: [30, 3, 119] },
  { key: "Requirements Hall", position: [-76, 6.5, -22] },
  { key: "ESCO Project Core", position: [LANDMARK[0], 30, LANDMARK[1]] },
  { key: "Template Library", position: [-82, 8, -72] },
  { key: "Tag Foundry", position: [10, 7.5, -73] },
  { key: "Ladder Logic Lab", position: [-27, 10, -53] },
  { key: "Alarm Center", position: [67, 7, 4] },
  { key: "Port Terminal", position: [96, 7, 126] }
];

const DISTRICTS: DistrictSpec[] = [
  {
    label: "CENTRAL OPERATIONS",
    center: [-17, -37],
    size: [76, 54],
    accent: "cyan",
    density: 0.95,
    minHeight: 5,
    maxHeight: 30,
    skyline: 1.42,
    seed: 1103
  },
  {
    label: "NORTH DISTRICT",
    center: [-95, -9],
    size: [50, 30],
    accent: "cyan",
    density: 0.86,
    minHeight: 3,
    maxHeight: 15,
    skyline: 0.82,
    seed: 2207
  },
  {
    label: "FINANCIAL / DOCS",
    center: [38, -52],
    size: [48, 32],
    accent: "orange",
    density: 0.9,
    minHeight: 4,
    maxHeight: 18,
    skyline: 1,
    seed: 3301
  },
  {
    label: "EAST DISTRICT",
    center: [85, -36],
    size: [48, 34],
    accent: "orange",
    density: 0.88,
    minHeight: 3,
    maxHeight: 16,
    skyline: 0.92,
    seed: 4409
  },
  {
    label: "WEST DISTRICT",
    center: [-80, 32],
    size: [48, 34],
    accent: "purple",
    density: 0.84,
    minHeight: 3,
    maxHeight: 14,
    skyline: 0.8,
    seed: 5501
  },
  {
    label: "INDUSTRIAL ZONE",
    center: [-62, 79],
    size: [48, 32],
    accent: "purple",
    density: 0.74,
    minHeight: 2.5,
    maxHeight: 9,
    skyline: 0.54,
    seed: 6607
  },
  {
    label: "SOUTH DISTRICT",
    center: [-24, 58],
    size: [52, 36],
    accent: "purple",
    density: 0.87,
    minHeight: 3,
    maxHeight: 15,
    skyline: 0.84,
    seed: 7703
  },
  {
    label: "INNOVATION DISTRICT",
    center: [44, 20],
    size: [50, 36],
    accent: "purple",
    density: 0.78,
    minHeight: 3,
    maxHeight: 15,
    skyline: 0.94,
    seed: 8803
  },
  {
    label: "LOGISTICS HUB",
    center: [78, 42],
    size: [44, 34],
    accent: "orange",
    density: 0.68,
    minHeight: 2,
    maxHeight: 8,
    skyline: 0.46,
    seed: 9901
  },
  {
    label: "NORTH GATEWAY",
    center: [-191, -98],
    size: [46, 28],
    accent: "cyan",
    density: 0.84,
    minHeight: 3,
    maxHeight: 17,
    skyline: 1.04,
    seed: 10103
  },
  {
    label: "RESEARCH PARK",
    center: [-124, -98],
    size: [35, 25],
    accent: "purple",
    density: 0.7,
    minHeight: 2.4,
    maxHeight: 9,
    skyline: 0.62,
    seed: 11113
  },
  {
    label: "INNOVATION BELT",
    center: [-67, -145],
    size: [39, 27],
    accent: "purple",
    density: 0.86,
    minHeight: 3,
    maxHeight: 14,
    skyline: 0.9,
    seed: 12109
  },
  {
    label: "ENERGY CAMPUS",
    center: [29, -165],
    size: [43, 27],
    accent: "cyan",
    density: 0.57,
    minHeight: 2,
    maxHeight: 7,
    skyline: 0.42,
    seed: 13103
  },
  {
    label: "WATER WORKS",
    center: [124, -168],
    size: [44, 30],
    accent: "cyan",
    density: 0.64,
    minHeight: 2,
    maxHeight: 8,
    skyline: 0.48,
    seed: 14107
  },
  {
    label: "EAST INDUSTRIAL",
    center: [134, -73],
    size: [46, 39],
    accent: "orange",
    density: 0.72,
    minHeight: 2,
    maxHeight: 10,
    skyline: 0.58,
    seed: 15101
  },
  {
    label: "SOUTH GATEWAY",
    center: [149, -8],
    size: [39, 31],
    accent: "orange",
    density: 0.76,
    minHeight: 2.5,
    maxHeight: 11,
    skyline: 0.66,
    seed: 16111
  },
  {
    label: "MAINTENANCE DEPOT",
    center: [131, 52],
    size: [44, 32],
    accent: "orange",
    density: 0.63,
    minHeight: 2,
    maxHeight: 7,
    skyline: 0.4,
    seed: 17107
  },
  {
    label: "SUPPLY CHAIN",
    center: [62, 86],
    size: [47, 33],
    accent: "orange",
    density: 0.7,
    minHeight: 2,
    maxHeight: 8,
    skyline: 0.48,
    seed: 18113
  },
  {
    label: "DATA CENTER",
    center: [30, 119],
    size: [43, 28],
    accent: "cyan",
    density: 0.62,
    minHeight: 2,
    maxHeight: 8,
    skyline: 0.44,
    seed: 19121
  },
  {
    label: "TESTING GROUNDS",
    center: [-32, 124],
    size: [43, 29],
    accent: "cyan",
    density: 0.67,
    minHeight: 2,
    maxHeight: 9,
    skyline: 0.52,
    seed: 20107
  },
  {
    label: "TRAINING ACADEMY",
    center: [-24, 94],
    size: [37, 27],
    accent: "purple",
    density: 0.74,
    minHeight: 2.4,
    maxHeight: 10,
    skyline: 0.62,
    seed: 21101
  },
  {
    label: "DESIGN DISTRICT",
    center: [-62, 104],
    size: [43, 31],
    accent: "purple",
    density: 0.76,
    minHeight: 2.5,
    maxHeight: 11,
    skyline: 0.68,
    seed: 22109
  },
  {
    label: "WEST GATEWAY",
    center: [-174, -143],
    size: [54, 27],
    accent: "cyan",
    density: 0.84,
    minHeight: 3,
    maxHeight: 15,
    skyline: 0.92,
    seed: 23117
  },
  {
    label: "NORTH INNOVATION",
    center: [-92, -158],
    size: [50, 28],
    accent: "purple",
    density: 0.86,
    minHeight: 3,
    maxHeight: 14,
    skyline: 0.88,
    seed: 24107
  },
  {
    label: "UTILITY RIDGE",
    center: [10, -166],
    size: [52, 27],
    accent: "cyan",
    density: 0.66,
    minHeight: 2,
    maxHeight: 8,
    skyline: 0.46,
    seed: 25111
  },
  {
    label: "NORTH WATERFRONT",
    center: [103, -160],
    size: [54, 29],
    accent: "orange",
    density: 0.73,
    minHeight: 2.5,
    maxHeight: 10,
    skyline: 0.58,
    seed: 26113
  }
];

const ROAD_SEGMENTS: RoadSegment[] = [
  { from: [-80, 36], to: [-95, -9], width: 4.2 },
  { from: [-95, -9], to: [-53, -44], width: 4.4 },
  { from: [-53, -44], to: HUB, width: 4.7 },
  { from: HUB, to: [39, -57], width: 4.8 },
  { from: [39, -57], to: [85, -36], width: 4.5 },
  { from: HUB, to: [46, 21], width: 4.8 },
  { from: [46, 21], to: [78, 40], width: 4.5 },
  { from: HUB, to: [-24, 56], width: 4.6 },
  { from: [-24, 56], to: [-80, 36], width: 4.3 },
  { from: HUB, to: [-80, 36], width: 4.5 },
  { from: [39, -57], to: [29, -165], width: 4.3 },
  { from: [85, -36], to: [134, -73], width: 4.4 },
  { from: [134, -73], to: [124, -168], width: 4.2 },
  { from: [78, 40], to: [149, -8], width: 4.4 },
  { from: [78, 40], to: [131, 52], width: 4.3 },
  { from: [78, 40], to: [62, 86], width: 4.3 },
  { from: [-24, 56], to: [-24, 94], width: 4 },
  { from: [-24, 94], to: [-32, 124], width: 4 },
  { from: [-24, 56], to: [-62, 104], width: 4.1 },
  { from: [-95, -9], to: [-124, -98], width: 4.2 },
  { from: [-124, -98], to: [-191, -98], width: 4.2 },
  { from: [-53, -44], to: [-67, -145], width: 4.2 },
  { from: [29, -165], to: [124, -168], width: 4.1 },
  { from: [62, 86], to: [30, 119], width: 4.1 },
  { from: [30, 119], to: [96, 126], width: 4.1 },
  { from: [131, 52], to: [96, 126], width: 4.1 },
  { from: [46, 21], to: PARK, width: 4 },
  { from: PARK, to: [62, 86], width: 4.2 },
  { from: [-191, -98], to: [-174, -143], width: 3.9 },
  { from: [-124, -98], to: [-92, -158], width: 3.9 },
  { from: [-67, -145], to: [10, -166], width: 4 },
  { from: [29, -165], to: [103, -160], width: 4 }
];

const ROOT_STYLE: CSSProperties = {
  position: "absolute",
  inset: 0,
  overflow: "hidden",
  background: "#4d8398"
};

const CANVAS_MOUNT_STYLE: CSSProperties = {
  position: "absolute",
  inset: 0,
  zIndex: 0
};

const MODE_BADGE_STYLE: CSSProperties = {
  position: "absolute",
  zIndex: 2,
  top: 10,
  left: 12,
  padding: "6px 9px",
  border: "1px solid rgba(255,255,255,.28)",
  background: "rgba(2,3,3,.82)",
  color: "#f3f5f5",
  fontSize: 8,
  fontWeight: 800,
  letterSpacing: "1.1px",
  pointerEvents: "none"
};

const FIRST_PERSON_HUD_STYLE: CSSProperties = {
  position: "absolute",
  zIndex: 4,
  top: 42,
  left: "50%",
  transform: "translateX(-50%)",
  display: "grid",
  justifyItems: "center",
  gap: 2,
  minWidth: 250,
  padding: "7px 12px",
  border: "1px solid rgba(92,221,255,.34)",
  borderRadius: 3,
  background: "rgba(3,8,23,.78)",
  boxShadow: "0 8px 28px rgba(0,0,0,.32)",
  color: "#dbe9ee",
  fontSize: 8,
  fontWeight: 700,
  letterSpacing: ".65px",
  lineHeight: 1.4,
  pointerEvents: "auto"
};

const CROSSHAIR_STYLE: CSSProperties = {
  position: "absolute",
  zIndex: 4,
  top: "50%",
  left: "50%",
  width: 16,
  height: 16,
  transform: "translate(-50%, -50%)",
  color: "#bdefff",
  fontSize: 20,
  fontWeight: 300,
  lineHeight: "14px",
  textAlign: "center",
  textShadow: "0 0 8px rgba(20,221,255,.9)",
  pointerEvents: "none",
  userSelect: "none"
};

const BUILDING_NAV_STYLE: CSSProperties = {
  position: "absolute",
  zIndex: 3,
  right: 10,
  bottom: 9,
  left: 166,
  display: "flex",
  justifyContent: "flex-end",
  gap: 4,
  overflowX: "auto",
  pointerEvents: "auto"
};

const ERROR_STYLE: CSSProperties = {
  position: "absolute",
  zIndex: 5,
  top: "50%",
  left: "50%",
  width: "min(460px, calc(100% - 40px))",
  transform: "translate(-50%, -50%)",
  padding: 18,
  border: "1px solid #ff6a00",
  background: "rgba(8,8,8,.96)",
  boxShadow: "0 18px 60px #000",
  color: "#dfe3e5",
  fontSize: 11,
  lineHeight: 1.5
};

const makeStandard = (color: number, roughness: number, metalness: number) =>
  new THREE.MeshStandardMaterial({ color, roughness, metalness });

const makeGlass = (color: number, emissive: number, emissiveIntensity: number) =>
  new THREE.MeshPhysicalMaterial({
    color,
    emissive,
    emissiveIntensity,
    roughness: 0.15,
    metalness: 0.52,
    clearcoat: 1,
    clearcoatRoughness: 0.08,
    reflectivity: 0.9
  });

const makeEmissive = (color: number, emissive: number) =>
  new THREE.MeshStandardMaterial({
    color,
    emissive,
    emissiveIntensity: 0.92,
    roughness: 0.32,
    metalness: 0.48
  });

const makeGlow = (color: number, opacity: number) =>
  new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    toneMapped: false
  });

function createPalette(): MaterialPalette {
  return {
    white: makeStandard(0xdde7e5, 0.46, 0.13),
    light: makeStandard(0xbacacc, 0.52, 0.18),
    concrete: makeStandard(0x789098, 0.74, 0.08),
    roof: makeStandard(0x879ba0, 0.56, 0.28),
    glassCyan: makeGlass(0x06344a, 0x00c8f2, 0.66),
    glassOrange: makeGlass(0x3b1b0d, 0xff5a00, 0.58),
    glassPurple: makeGlass(0x2d1548, 0xa34fff, 0.62),
    blackSteel: makeStandard(0x1a303c, 0.34, 0.68),
    asphalt: makeStandard(0x263d49, 0.84, 0.07),
    water: new THREE.MeshPhysicalMaterial({
      color: 0x1b6282,
      emissive: 0x005a73,
      emissiveIntensity: 0.36,
      roughness: 0.17,
      metalness: 0.36,
      clearcoat: 1,
      clearcoatRoughness: 0.08
    }),
    contact: new THREE.MeshBasicMaterial({
      color: 0x020405,
      transparent: true,
      opacity: 0.27,
      depthWrite: false
    }),
    park: makeEmissive(0x1d6841, 0x0b4227),
    foliage: new THREE.MeshStandardMaterial({
      color: 0x48d878,
      emissive: 0x0b6a35,
      emissiveIntensity: 0.32,
      roughness: 0.82,
      metalness: 0.01
    }),
    cyanEmissive: makeEmissive(0x38d8ed, 0x00bfe8),
    orangeEmissive: makeEmissive(0xff8340, 0xff4d00),
    cyanGlow: makeGlow(0x14ddff, 0.55),
    orangeGlow: makeGlow(0xff6a22, 0.84),
    purpleGlow: makeGlow(0xa45cff, 0.54),
    greenGlow: makeGlow(0x36ff7a, 0.58),
    mobilityGlow: makeGlow(0xff6a22, 0.34),
    agentOrange: new THREE.MeshStandardMaterial({
      color: 0xff6a00,
      roughness: 0.34,
      metalness: 0.36,
      emissive: 0x8c2500,
      emissiveIntensity: 0.85
    })
  };
}

function anchorAccentFor(id: BuildingId): DistrictAccent {
  if (id === "documentation" || id === "release") return "orange";
  if (id === "intake" || id === "qa" || id === "hmi") return "purple";
  return "cyan";
}

function accentHex(accent: DistrictAccent) {
  return accent === "cyan" ? 0x17d8f4 : accent === "orange" ? 0xff6a22 : 0xa45cff;
}

function accentGlass(palette: MaterialPalette, accent: DistrictAccent) {
  return accent === "cyan"
    ? palette.glassCyan
    : accent === "orange"
      ? palette.glassOrange
      : palette.glassPurple;
}

function makeAccentMaterial(ambientAccent: number) {
  return new THREE.MeshStandardMaterial({
    color: ambientAccent,
    roughness: 0.34,
    metalness: 0.48,
    emissive: ambientAccent,
    emissiveIntensity: 0.46
  });
}

function addBox(
  parent: THREE.Object3D,
  size: Vec3,
  position: Vec3,
  material: THREE.Material,
  castShadow = true
) {
  const object = new THREE.Mesh(new THREE.BoxGeometry(...size), material);
  object.position.set(...position);
  object.castShadow = castShadow;
  object.receiveShadow = true;
  parent.add(object);
  return object;
}

function addCylinder(
  parent: THREE.Object3D,
  radius: number,
  height: number,
  position: Vec3,
  material: THREE.Material,
  radialSegments = 16
) {
  const object = new THREE.Mesh(
    new THREE.CylinderGeometry(radius, radius, height, radialSegments),
    material
  );
  object.position.set(...position);
  object.castShadow = true;
  object.receiveShadow = true;
  parent.add(object);
  return object;
}

function addOverlayPath(
  parent: THREE.Object3D,
  points: ReadonlyArray<Point2>,
  color: number,
  opacity: number,
  y = 0.72,
  closed = false
) {
  const geometry = new THREE.BufferGeometry().setFromPoints(
    points.map(([x, z]) => new THREE.Vector3(x, y, z))
  );
  const material = new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity,
    depthTest: false,
    depthWrite: false,
    toneMapped: false
  });
  const line = closed
    ? new THREE.LineLoop(geometry, material)
    : new THREE.Line(geometry, material);
  line.renderOrder = 18;
  parent.add(line);
  return line;
}

function addOverlayRing(
  parent: THREE.Object3D,
  center: Point2,
  radius: number,
  color: number,
  opacity = 0.72,
  y = 0.78
) {
  const material = new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity,
    depthTest: false,
    depthWrite: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    toneMapped: false
  });
  const ring = new THREE.Mesh(
    new THREE.RingGeometry(radius * 0.76, radius, 40),
    material
  );
  ring.rotation.x = -Math.PI / 2;
  ring.position.set(center[0], y, center[1]);
  ring.renderOrder = 19;
  parent.add(ring);
  return ring;
}

function addOverlayBeacon(
  parent: THREE.Object3D,
  center: Point2,
  height: number,
  color: number
) {
  const geometry = new THREE.BufferGeometry().setFromPoints([
    new THREE.Vector3(center[0], 0.72, center[1]),
    new THREE.Vector3(center[0], height, center[1])
  ]);
  const material = new THREE.LineBasicMaterial({
    color,
    transparent: true,
    opacity: 0.78,
    depthTest: false,
    depthWrite: false,
    toneMapped: false
  });
  const beacon = new THREE.Line(geometry, material);
  beacon.renderOrder = 19;
  parent.add(beacon);

  const cap = new THREE.Mesh(
    new THREE.OctahedronGeometry(0.46, 0),
    new THREE.MeshBasicMaterial({
      color,
      transparent: true,
      opacity: 0.9,
      depthTest: false,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      toneMapped: false
    })
  );
  cap.position.set(center[0], height, center[1]);
  cap.renderOrder = 20;
  parent.add(cap);
}

function addOverlayFootprint(
  parent: THREE.Object3D,
  center: Point2,
  size: Point2,
  color: number,
  opacity = 0.72
) {
  const halfWidth = size[0] / 2 + 1.25;
  const halfDepth = size[1] / 2 + 1.25;
  addOverlayPath(
    parent,
    [
      [center[0] - halfWidth, center[1] - halfDepth],
      [center[0] + halfWidth, center[1] - halfDepth],
      [center[0] + halfWidth, center[1] + halfDepth],
      [center[0] - halfWidth, center[1] + halfDepth]
    ],
    color,
    opacity,
    0.74,
    true
  );
}

function createLayerOverlays(scene: THREE.Scene) {
  const overlays = {
    infrastructure: new THREE.Group(),
    utilities: new THREE.Group(),
    environment: new THREE.Group(),
    security: new THREE.Group(),
    mobility: new THREE.Group()
  } satisfies Record<CityOverlayLayer, THREE.Group>;

  Object.entries(overlays).forEach(([name, group]) => {
    group.name = `city-layer-${name}`;
    group.visible = false;
    scene.add(group);
  });

  ANCHOR_SPECS.forEach((anchor, index) => {
    addOverlayFootprint(
      overlays.infrastructure,
      anchor.position,
      anchor.lot,
      index % 2 === 0 ? 0x5ce8ff : 0xa4f4ff
    );
    addOverlayBeacon(
      overlays.infrastructure,
      anchor.position,
      8 + (index % 3) * 2.2,
      0x5ce8ff
    );
  });

  const utilityNodes: ReadonlyArray<Point2> = [
    LANDMARK,
    ANCHOR_SPECS.find(({ id }) => id === "automation")!.position,
    ANCHOR_SPECS.find(({ id }) => id === "hmi")!.position,
    ANCHOR_SPECS.find(({ id }) => id === "electrical")!.position,
    [29, -165],
    [124, -168]
  ];
  utilityNodes.slice(1).forEach((node, index) => {
    addOverlayPath(
      overlays.utilities,
      [utilityNodes[0], node],
      index % 2 === 0 ? 0x42dfff : 0xae64ff,
      0.78,
      0.88
    );
    addOverlayRing(
      overlays.utilities,
      node,
      index < 3 ? 3.4 : 5.2,
      index % 2 === 0 ? 0x42dfff : 0xae64ff
    );
  });
  addOverlayBeacon(overlays.utilities, LANDMARK, 24, 0x42dfff);

  WATER_BANDS.forEach(({ center, size }, index) => {
    addOverlayFootprint(
      overlays.environment,
      center,
      size,
      index % 2 === 0 ? 0x45e5ff : 0x4cff8b,
      0.56
    );
  });
  for (const [center, radius] of [
    [PARK, 15.5],
    [[-124, -98] as Point2, 10.5],
    [[124, -168] as Point2, 9.5]
  ] as ReadonlyArray<readonly [Point2, number]>) {
    addOverlayRing(overlays.environment, center, radius, 0x4cff8b, 0.7);
    addOverlayRing(overlays.environment, center, radius * 0.62, 0x45e5ff, 0.48, 0.8);
  }

  const securityAnchors = ANCHOR_SPECS.filter(({ id }) =>
    (["intake", "qa", "approval", "release"] as BuildingId[]).includes(id)
  );
  addOverlayPath(
    overlays.security,
    securityAnchors.map(({ position }) => position),
    0xb86cff,
    0.82,
    0.92,
    true
  );
  securityAnchors.forEach((anchor, index) => {
    addOverlayFootprint(overlays.security, anchor.position, anchor.lot, 0xb86cff, 0.82);
    addOverlayRing(overlays.security, anchor.position, 4.2 + index * 0.45, 0xe9fbff, 0.62);
    addOverlayBeacon(overlays.security, anchor.position, 10 + index * 1.4, 0xb86cff);
  });

  ROAD_SEGMENTS.forEach((segment, index) => {
    addOverlayPath(
      overlays.mobility,
      [segment.from, segment.to],
      index % 3 === 0 ? 0xffa14a : 0xff6a22,
      0.62,
      0.9
    );
  });
  for (const node of [HUB, [-95, -9], [39, -57], [78, 40]] as Point2[]) {
    addOverlayRing(overlays.mobility, node, node === HUB ? 6.5 : 4.2, 0xff8a32, 0.74);
  }

  return overlays;
}

const LAYER_SKY_COLORS: Record<CityHomeLayer, number> = {
  overview: 0x4d8398,
  infrastructure: 0x467a91,
  utilities: 0x426f91,
  environment: 0x4d857f,
  security: 0x586f85,
  mobility: 0x52798d
};

function applyCityLayer(runtime: LayerSceneRuntime, layer: CityHomeLayer) {
  Object.entries(runtime.overlays).forEach(([overlayLayer, group]) => {
    group.visible = overlayLayer === layer;
  });

  const showVehicles = layer === "overview" || layer === "mobility";
  runtime.vehicleLife.body.visible = showVehicles;
  runtime.vehicleLife.cabin.visible = showVehicles;
  runtime.vehicleLife.lamps.visible = showVehicles;

  const showPedestrians =
    layer === "overview" || layer === "environment" || layer === "mobility";
  runtime.pedestrianLife.bodies.visible = showPedestrians;
  runtime.pedestrianLife.heads.visible = showPedestrians;

  runtime.waterPulses.mesh.visible =
    layer === "overview" || layer === "utilities" || layer === "environment";
  runtime.routePulses.mesh.visible = layer === "overview" || layer === "mobility";
  runtime.rooftopSignals.mesh.visible =
    layer === "overview" ||
    layer === "infrastructure" ||
    layer === "utilities" ||
    layer === "security";

  const skyColor = LAYER_SKY_COLORS[layer];
  if (runtime.scene.background instanceof THREE.Color) {
    runtime.scene.background.setHex(skyColor);
  }
  if (runtime.scene.fog instanceof THREE.Fog) {
    runtime.scene.fog.color.setHex(skyColor);
  }
}

function addFacadeBands(
  parent: THREE.Object3D,
  width: number,
  depth: number,
  height: number,
  rows: number,
  material: THREE.Material,
  x = 0,
  z = 0
) {
  for (let row = 1; row <= rows; row += 1) {
    const y = (height / (rows + 1)) * row;
    addBox(parent, [width + 0.045, 0.16, depth + 0.045], [x, y, z], material, false);
  }
}

function createWorldGround(scene: THREE.Scene, palette: MaterialPalette) {
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(1800, 1800),
    new THREE.MeshStandardMaterial({
      color: 0x17343f,
      roughness: 0.98,
      metalness: 0.02
    })
  );
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.02;
  ground.receiveShadow = true;
  scene.add(ground);

  const urbanGround = new THREE.Mesh(
    new THREE.PlaneGeometry(440, 340),
    new THREE.MeshStandardMaterial({
      color: 0x183846,
      roughness: 0.96,
      metalness: 0.035
    })
  );
  urbanGround.rotation.x = -Math.PI / 2;
  urbanGround.position.y = 0.004;
  urbanGround.receiveShadow = true;
  scene.add(urbanGround);

  const chunkGeometry = new THREE.BoxGeometry(1, 1, 1);
  const chunkMatrices: THREE.Matrix4[][] = [[], [], [], []];
  const chunkSize = 16;
  for (let chunkX = -42; chunkX <= 42; chunkX += 1) {
    for (let chunkZ = -42; chunkZ <= 42; chunkZ += 1) {
      const x = chunkX * chunkSize;
      const z = chunkZ * chunkSize;
      if (Math.abs(x) < 236 && Math.abs(z) < 190) continue;
      const variant = Math.abs((chunkX * 17 + chunkZ * 31 + chunkX * chunkZ * 3) % 4);
      chunkMatrices[variant].push(
        boxMatrix(x, -0.045, z, chunkSize + 0.035, 0.08, chunkSize + 0.035)
      );
    }
  }
  const chunkMaterials = [0x173743, 0x193b46, 0x153540, 0x1b3d47].map(
    (color) =>
      new THREE.MeshStandardMaterial({
        color,
        roughness: 0.99,
        metalness: 0.01
      })
  );
  chunkMatrices.forEach((matrices, index) => {
    addInstancedBoxes(scene, chunkGeometry, matrices, chunkMaterials[index], false);
  });

  WATER_BANDS.forEach((band) => createWaterBand(scene, palette, band.center, band.size));
}

function createWaterBand(
  scene: THREE.Scene,
  palette: MaterialPalette,
  center: Point2,
  size: Point2
) {
  addBox(scene, [size[0], 0.08, size[1]], [center[0], 0.2, center[1]], palette.water, false);
  const flowSize: Vec3 =
    size[0] > size[1]
      ? [size[0] * 0.985, 0.018, Math.min(0.42, size[1] * 0.08)]
      : [Math.min(0.42, size[0] * 0.08), 0.018, size[1] * 0.985];
  addBox(scene, flowSize, [center[0], 0.246, center[1]], palette.cyanGlow, false);
  if (size[0] > size[1]) {
    for (const offset of [-size[1] / 2, size[1] / 2]) {
      addBox(
        scene,
        [size[0], 0.18, 0.34],
        [center[0], 0.252, center[1] + offset],
        palette.concrete,
        false
      );
    }
  } else {
    for (const offset of [-size[0] / 2, size[0] / 2]) {
      addBox(
        scene,
        [0.34, 0.18, size[1]],
        [center[0] + offset, 0.252, center[1]],
        palette.concrete,
        false
      );
    }
  }
}

function makeClippedDistrictShape(width: number, depth: number) {
  const cut = Math.min(4.2, width * 0.1, depth * 0.14);
  const shape = new THREE.Shape();
  shape.moveTo(-width / 2 + cut, -depth / 2);
  shape.lineTo(width / 2 - cut, -depth / 2);
  shape.lineTo(width / 2, -depth / 2 + cut);
  shape.lineTo(width / 2, depth / 2 - cut);
  shape.lineTo(width / 2 - cut, depth / 2);
  shape.lineTo(-width / 2 + cut, depth / 2);
  shape.lineTo(-width / 2, depth / 2 - cut);
  shape.lineTo(-width / 2, -depth / 2 + cut);
  shape.closePath();
  return shape;
}

function createDistrictPads(scene: THREE.Scene, palette: MaterialPalette) {
  DISTRICTS.forEach((district, index) => {
    const [width, depth] = district.size;
    const [x, z] = district.center;
    const shape = makeClippedDistrictShape(width, depth);
    const accent = district.accent;
    const surfaceMaterial = new THREE.MeshStandardMaterial({
      color:
        accent === "orange"
          ? 0x334044
          : accent === "purple"
            ? 0x2d3946
            : index % 2 === 0
              ? 0x29414c
              : 0x2c4550,
      emissive: accent === "orange" ? 0x1a0900 : accent === "purple" ? 0x120821 : 0x001820,
      emissiveIntensity: 0.08,
      roughness: 0.92,
      metalness: 0.035
    });
    const districtRoute = makeGlow(accentHex(accent), 0.18);

    const surface = new THREE.Mesh(new THREE.ShapeGeometry(shape), surfaceMaterial);
    surface.rotation.x = -Math.PI / 2;
    surface.position.set(x, 0.105, z);
    surface.receiveShadow = true;
    surface.renderOrder = 2;
    scene.add(surface);

    addBox(scene, [width - 6, 0.07, 1.05], [x, 0.155, z], palette.asphalt, false);
    addBox(scene, [1.05, 0.07, depth - 6], [x, 0.156, z], palette.asphalt, false);
    addBox(
      scene,
      [width - 7, 0.025, 0.065],
      [x, 0.198, z],
      districtRoute,
      false
    );
    addBox(
      scene,
      [0.065, 0.025, depth - 7],
      [x, 0.199, z],
      districtRoute,
      false
    );
  });
}

function pointToSegmentDistance(point: Point2, segment: RoadSegment) {
  const [px, pz] = point;
  const [ax, az] = segment.from;
  const [bx, bz] = segment.to;
  const dx = bx - ax;
  const dz = bz - az;
  const lengthSquared = dx * dx + dz * dz;
  const t =
    lengthSquared === 0
      ? 0
      : THREE.MathUtils.clamp(((px - ax) * dx + (pz - az) * dz) / lengthSquared, 0, 1);
  return Math.hypot(px - (ax + t * dx), pz - (az + t * dz));
}

function createRoadNetwork(scene: THREE.Scene, palette: MaterialPalette) {
  const roadMatrices: THREE.Matrix4[] = [];
  const curbMatrices: THREE.Matrix4[] = [];
  const glowMatrices: THREE.Matrix4[] = [];
  const dashMatrices: THREE.Matrix4[] = [];

  for (const segment of ROAD_SEGMENTS) {
    const [ax, az] = segment.from;
    const [bx, bz] = segment.to;
    const dx = bx - ax;
    const dz = bz - az;
    const length = Math.hypot(dx, dz);
    const midpoint: Point2 = [(ax + bx) / 2, (az + bz) / 2];
    const rotation = -Math.atan2(dz, dx);
    const normalX = -dz / length;
    const normalZ = dx / length;
    const unitX = dx / length;
    const unitZ = dz / length;

    roadMatrices.push(
      boxMatrix(midpoint[0], 0.235, midpoint[1], length, 0.14, segment.width, rotation)
    );
    glowMatrices.push(
      boxMatrix(midpoint[0], 0.334, midpoint[1], length, 0.026, 0.18, rotation)
    );
    for (const side of [-1, 1]) {
      curbMatrices.push(
        boxMatrix(
          midpoint[0] + normalX * side * segment.width * 0.49,
          0.32,
          midpoint[1] + normalZ * side * segment.width * 0.49,
          length,
          0.1,
          0.22,
          rotation
        )
      );
    }
    for (let distance = 1.1; distance < length - 1; distance += 2.35) {
      dashMatrices.push(
        boxMatrix(
          ax + unitX * distance,
          0.336,
          az + unitZ * distance,
          Math.min(1.25, length - distance),
          0.035,
          0.16,
          rotation
        )
      );
    }
  }

  WATER_BANDS.forEach(({ center, size }, bandIndex) => {
    if (size[0] > size[1]) {
      METRO_VERTICAL_STREETS.forEach((x, streetIndex) => {
        if ((streetIndex + bandIndex) % 2 !== 0) return;
        roadMatrices.push(boxMatrix(x, 0.25, center[1], 1.85, 0.16, size[1] + 2.4));
        glowMatrices.push(boxMatrix(x, 0.345, center[1], 0.08, 0.025, size[1] + 1.8));
      });
    } else {
      METRO_HORIZONTAL_STREETS.forEach((z, streetIndex) => {
        if ((streetIndex + bandIndex) % 2 !== 0) return;
        roadMatrices.push(boxMatrix(center[0], 0.25, z, size[0] + 2.4, 0.16, 1.85));
        glowMatrices.push(boxMatrix(center[0], 0.345, z, size[0] + 1.8, 0.025, 0.08));
      });
    }
  });

  const unitBox = new THREE.BoxGeometry(1, 1, 1);
  const curbMaterial = new THREE.MeshBasicMaterial({
    color: 0x8ba7b5,
    transparent: true,
    opacity: 0.68
  });
  addInstancedBoxes(scene, unitBox, roadMatrices, palette.asphalt, false);
  addInstancedBoxes(scene, unitBox, curbMatrices, curbMaterial, false);
  addInstancedBoxes(scene, unitBox, glowMatrices, palette.mobilityGlow, false);
  addInstancedBoxes(scene, unitBox, dashMatrices, palette.mobilityGlow, false);

  for (const [x, z] of [
    [-84, -58],
    [-31, -58],
    [24, -58],
    [72, -58],
    [-58, 27],
    [-7, 27],
    [31, 27],
    [76, 27],
    [93, -42],
    [93, -5],
    [93, 35],
    [-116, 15],
    [-116, 70]
  ] as Point2[]) {
    addCylinder(scene, 0.32, 1.1, [x, -0.36, z], palette.concrete, 12);
  }
}

function boxMatrix(
  x: number,
  y: number,
  z: number,
  width: number,
  height: number,
  depth: number,
  rotation = 0
) {
  const quaternion = new THREE.Quaternion().setFromAxisAngle(
    new THREE.Vector3(0, 1, 0),
    rotation
  );
  return new THREE.Matrix4().compose(
    new THREE.Vector3(x, y, z),
    quaternion,
    new THREE.Vector3(width, height, depth)
  );
}

function addInstancedBoxes(
  scene: THREE.Scene,
  geometry: THREE.BoxGeometry,
  matrices: THREE.Matrix4[],
  material: THREE.Material,
  castShadow = true
) {
  if (matrices.length === 0) return;
  const mesh = new THREE.InstancedMesh(geometry, material, matrices.length);
  matrices.forEach((matrix, index) => mesh.setMatrixAt(index, matrix));
  mesh.instanceMatrix.setUsage(THREE.StaticDrawUsage);
  mesh.instanceMatrix.needsUpdate = true;
  mesh.castShadow = castShadow;
  mesh.receiveShadow = true;
  mesh.computeBoundingSphere();
  scene.add(mesh);
}

function addStaticInstances(
  scene: THREE.Scene,
  geometry: THREE.BufferGeometry,
  matrices: THREE.Matrix4[],
  material: THREE.Material,
  castShadow = true
) {
  if (matrices.length === 0) return;
  const mesh = new THREE.InstancedMesh(geometry, material, matrices.length);
  matrices.forEach((matrix, index) => mesh.setMatrixAt(index, matrix));
  mesh.instanceMatrix.setUsage(THREE.StaticDrawUsage);
  mesh.instanceMatrix.needsUpdate = true;
  mesh.castShadow = castShadow;
  mesh.receiveShadow = true;
  mesh.computeBoundingSphere();
  scene.add(mesh);
}

function createUrbanDetails(scene: THREE.Scene, palette: MaterialPalette) {
  const streetlightSupports: THREE.Matrix4[] = [];
  const streetlightHeads: THREE.Matrix4[] = [];

  for (const segment of ROAD_SEGMENTS) {
    const dx = segment.to[0] - segment.from[0];
    const dz = segment.to[1] - segment.from[1];
    const length = Math.hypot(dx, dz);
    const unitX = dx / length;
    const unitZ = dz / length;
    const normalX = -unitZ;
    const normalZ = unitX;
    const lightCount = Math.max(1, Math.floor((length - 7) / 9.5));

    for (let index = 0; index <= lightCount; index += 1) {
      const distance = 3.5 + ((length - 7) * index) / Math.max(1, lightCount);
      for (const side of [-1, 1]) {
        const offset = segment.width / 2 + 0.58;
        const x = segment.from[0] + unitX * distance + normalX * side * offset;
        const z = segment.from[1] + unitZ * distance + normalZ * side * offset;
        if (Math.hypot(x - HUB[0], z - HUB[1]) < 13.2) continue;

        streetlightSupports.push(boxMatrix(x, 1.69, z, 0.1, 2.72, 0.1));
        const inwardX = -normalX * side;
        const inwardZ = -normalZ * side;
        const armRotation = -Math.atan2(inwardZ, inwardX);
        streetlightSupports.push(
          boxMatrix(
            x + inwardX * 0.2,
            3.02,
            z + inwardZ * 0.2,
            0.48,
            0.08,
            0.08,
            armRotation
          )
        );
        streetlightHeads.push(
          boxMatrix(
            x + inwardX * 0.42,
            2.97,
            z + inwardZ * 0.42,
            0.34,
            0.16,
            0.26,
            armRotation
          )
        );
      }
    }
  }

  const crosswalkMatrices: THREE.Matrix4[] = [];
  const crosswalks: Point2[] = [
    [-64, -53],
    [-25, -18],
    [16, -4],
    [37, 12],
    [59, 40],
    [78, 55],
    [-45, 40],
    [-5, 55]
  ];
  for (const [x, z] of crosswalks) {
    for (let stripe = 0; stripe < 6; stripe += 1) {
      const spread = (stripe - 2.5) * 0.24;
      crosswalkMatrices.push(boxMatrix(x, 0.17, z - 1.28 + spread, 1.16, 0.025, 0.14));
      crosswalkMatrices.push(boxMatrix(x + 1.28 + spread, 0.171, z, 0.14, 0.025, 1.16));
    }
  }

  const benchMatrices: THREE.Matrix4[] = [];
  const benchLegMatrices: THREE.Matrix4[] = [];
  const planterMatrices: THREE.Matrix4[] = [];
  const planterFoliageMatrices: THREE.Matrix4[] = [];

  const addBenchRing = (center: Point2, radius: number, count: number, phase: number) => {
    for (let index = 0; index < count; index += 1) {
      const angle = phase + (index / count) * Math.PI * 2;
      const radialX = Math.cos(angle);
      const radialZ = Math.sin(angle);
      const x = center[0] + radialX * radius;
      const z = center[1] + radialZ * radius;
      const tangentX = -radialZ;
      const tangentZ = radialX;
      const rotation = -Math.atan2(tangentZ, tangentX);
      benchMatrices.push(boxMatrix(x, 0.58, z, 1.28, 0.16, 0.4, rotation));
      benchMatrices.push(
        boxMatrix(
          x + radialX * 0.17,
          0.86,
          z + radialZ * 0.17,
          1.28,
          0.48,
          0.11,
          rotation
        )
      );
      for (const legOffset of [-0.42, 0.42]) {
        benchLegMatrices.push(
          boxMatrix(
            x + tangentX * legOffset,
            0.43,
            z + tangentZ * legOffset,
            0.1,
            0.3,
            0.24,
            rotation
          )
        );
      }
    }
  };

  const addPlanterRing = (center: Point2, radius: number, count: number, phase: number) => {
    for (let index = 0; index < count; index += 1) {
      const angle = phase + (index / count) * Math.PI * 2;
      const x = center[0] + Math.cos(angle) * radius;
      const z = center[1] + Math.sin(angle) * radius;
      planterMatrices.push(boxMatrix(x, 0.57, z, 0.52, 0.5, 0.52, angle));
      planterFoliageMatrices.push(
        boxMatrix(x, 1.02, z, 0.7, 0.56 + (index % 3) * 0.08, 0.7, angle)
      );
    }
  };

  addBenchRing(HUB, 6.6, 8, Math.PI / 8);
  addBenchRing(PARK, 11.9, 10, 0);
  addPlanterRing(HUB, 7.8, 7, 0);
  addPlanterRing(PARK, 13.2, 9, Math.PI / 9);

  const unitBox = new THREE.BoxGeometry(1, 1, 1);
  const crosswalkMaterial = new THREE.MeshBasicMaterial({
    color: 0xeaf7f5,
    transparent: true,
    opacity: 0.82,
    depthWrite: false
  });
  addInstancedBoxes(scene, unitBox, streetlightSupports, palette.blackSteel);
  addInstancedBoxes(scene, unitBox, streetlightHeads, palette.cyanEmissive, false);
  addInstancedBoxes(scene, unitBox, crosswalkMatrices, crosswalkMaterial, false);
  addInstancedBoxes(scene, unitBox, benchMatrices, palette.roof);
  addInstancedBoxes(scene, unitBox, benchLegMatrices, palette.blackSteel);
  addStaticInstances(
    scene,
    new THREE.CylinderGeometry(1, 1, 1, 12),
    planterMatrices,
    palette.concrete
  );
  addStaticInstances(
    scene,
    new THREE.SphereGeometry(1, 10, 7),
    planterFoliageMatrices,
    palette.foliage
  );
}

function seededRandom(seed: number) {
  let state = seed >>> 0;
  return () => {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function isNearMetroStreet(x: number, z: number, clearance = 1.65) {
  const onVertical =
    z >= CITY_BOUNDS.minZ &&
    z <= CITY_BOUNDS.maxZ &&
    METRO_VERTICAL_STREETS.some((streetX) => Math.abs(x - streetX) < clearance);
  const onHorizontal =
    x >= CITY_BOUNDS.minX &&
    x <= CITY_BOUNDS.maxX &&
    METRO_HORIZONTAL_STREETS.some((streetZ) => Math.abs(z - streetZ) < clearance);
  return onVertical || onHorizontal;
}

function isInsideDistrict(x: number, z: number, margin = 0) {
  return DISTRICTS.some(
    (district) =>
      Math.abs(x - district.center[0]) < district.size[0] / 2 + margin &&
      Math.abs(z - district.center[1]) < district.size[1] / 2 + margin
  );
}

function isInsideWater(x: number, z: number) {
  return WATER_BANDS.some(
    ({ center, size }) =>
      Math.abs(x - center[0]) < size[0] / 2 + 0.35 &&
      Math.abs(z - center[1]) < size[1] / 2 + 0.35
  );
}

function metroAccentAt(x: number, z: number): DistrictAccent {
  if (x > 58 || (x > 18 && z > 62)) return "orange";
  if (x < -18 || z < -62) return "purple";
  return "cyan";
}

function createMetroFabric(scene: THREE.Scene, palette: MaterialPalette) {
  const streetMatrices: THREE.Matrix4[] = [];
  const laneMatrices: THREE.Matrix4[] = [];
  const cityWidth = CITY_BOUNDS.maxX - CITY_BOUNDS.minX;
  const cityDepth = CITY_BOUNDS.maxZ - CITY_BOUNDS.minZ;
  const cityCenterX = (CITY_BOUNDS.minX + CITY_BOUNDS.maxX) / 2;
  const cityCenterZ = (CITY_BOUNDS.minZ + CITY_BOUNDS.maxZ) / 2;
  for (const x of METRO_VERTICAL_STREETS) {
    streetMatrices.push(boxMatrix(x, 0.105, cityCenterZ, 1.4, 0.07, cityDepth));
    laneMatrices.push(boxMatrix(x, 0.148, cityCenterZ, 0.045, 0.018, cityDepth - 3));
  }
  for (const z of METRO_HORIZONTAL_STREETS) {
    streetMatrices.push(boxMatrix(cityCenterX, 0.106, z, cityWidth, 0.07, 1.4));
    laneMatrices.push(boxMatrix(cityCenterX, 0.149, z, cityWidth - 4, 0.018, 0.045));
  }

  const random = seededRandom(140219);
  const whiteBodies: THREE.Matrix4[] = [];
  const lightBodies: THREE.Matrix4[] = [];
  const contacts: THREE.Matrix4[] = [];
  const roofs: THREE.Matrix4[] = [];
  const glassBands: Record<DistrictAccent, THREE.Matrix4[]> = {
    cyan: [],
    orange: [],
    purple: []
  };
  const entranceDoors: Record<DistrictAccent, THREE.Matrix4[]> = {
    cyan: [],
    orange: [],
    purple: []
  };
  const pitch = 4.8;
  for (let x = CITY_BOUNDS.minX + 3; x <= CITY_BOUNDS.maxX - 3; x += pitch) {
    for (let z = CITY_BOUNDS.minZ + 3; z <= CITY_BOUNDS.maxZ - 3; z += pitch) {
      if (random() > 0.7) continue;
      const buildingX = x + (random() - 0.5) * 0.55;
      const buildingZ = z + (random() - 0.5) * 0.55;
      if (
        isInsideDistrict(buildingX, buildingZ, 1.4) ||
        isInsideWater(buildingX, buildingZ) ||
        isReserved(buildingX, buildingZ)
      ) {
        continue;
      }

      const width = 2.45 + random() * 1.25;
      const depth = 2.45 + random() * 1.25;
      const height = 2.3 + Math.pow(random(), 1.45) * 5.4;
      const rotation = random() > 0.88 ? Math.PI / 2 : 0;
      const accent = metroAccentAt(buildingX, buildingZ);
      contacts.push(
        boxMatrix(
          buildingX + 0.1,
          0.064,
          buildingZ + 0.12,
          width + 0.55,
          0.025,
          depth + 0.55,
          rotation
        )
      );
      const body = boxMatrix(
        buildingX,
        0.1 + height / 2,
        buildingZ,
        width,
        height,
        depth,
        rotation
      );
      (random() > 0.45 ? whiteBodies : lightBodies).push(body);
      glassBands[accent].push(
        boxMatrix(
          buildingX,
          0.1 + height * 0.62,
          buildingZ,
          width + 0.045,
          0.14,
          depth + 0.045,
          rotation
        )
      );
      const doorWidth = THREE.MathUtils.clamp(width * 0.28, 0.62, 0.88);
      const frontOffset = depth / 2 + 0.045;
      entranceDoors[accent].push(
        boxMatrix(
          buildingX + Math.sin(rotation) * frontOffset,
          0.8,
          buildingZ + Math.cos(rotation) * frontOffset,
          doorWidth,
          1.32,
          0.075,
          rotation
        )
      );
      roofs.push(
        boxMatrix(
          buildingX,
          height + 0.22,
          buildingZ,
          width + 0.14,
          0.2,
          depth + 0.14,
          rotation
        )
      );
    }
  }

  const unitBox = new THREE.BoxGeometry(1, 1, 1);
  const laneMaterial = new THREE.MeshBasicMaterial({
    color: 0x5a7b8e,
    transparent: true,
    opacity: 0.3,
    depthWrite: false
  });
  addInstancedBoxes(scene, unitBox, streetMatrices, palette.asphalt, false);
  addInstancedBoxes(scene, unitBox, laneMatrices, laneMaterial, false);
  addInstancedBoxes(scene, unitBox, contacts, palette.contact, false);
  addInstancedBoxes(scene, unitBox, whiteBodies, palette.white);
  addInstancedBoxes(scene, unitBox, lightBodies, palette.light);
  addInstancedBoxes(scene, unitBox, glassBands.cyan, palette.glassCyan, false);
  addInstancedBoxes(scene, unitBox, glassBands.orange, palette.glassOrange, false);
  addInstancedBoxes(scene, unitBox, glassBands.purple, palette.glassPurple, false);
  addInstancedBoxes(scene, unitBox, entranceDoors.cyan, palette.glassCyan, false);
  addInstancedBoxes(scene, unitBox, entranceDoors.orange, palette.glassOrange, false);
  addInstancedBoxes(scene, unitBox, entranceDoors.purple, palette.glassPurple, false);
  addInstancedBoxes(scene, unitBox, roofs, palette.roof);
}

function setInstanceTransform(
  mesh: THREE.InstancedMesh,
  index: number,
  dummy: THREE.Object3D,
  position: Vec3,
  scale: Vec3,
  rotationY = 0
) {
  dummy.position.set(...position);
  dummy.scale.set(...scale);
  dummy.rotation.set(0, rotationY, 0);
  dummy.updateMatrix();
  mesh.setMatrixAt(index, dummy.matrix);
}

function createVehicleLife(
  scene: THREE.Scene,
  palette: MaterialPalette,
  density = 1
): VehicleRuntime {
  const pathDefinitions: Array<RoadSegment & { surfaceY: number }> = [
    ...ROAD_SEGMENTS.map((segment) => ({ ...segment, surfaceY: 0.52 })),
    ...METRO_VERTICAL_STREETS.map((x) => ({
      from: [x, CITY_BOUNDS.minZ] as Point2,
      to: [x, CITY_BOUNDS.maxZ] as Point2,
      width: 1.4,
      surfaceY: 0.36
    })),
    ...METRO_HORIZONTAL_STREETS.map((z) => ({
      from: [CITY_BOUNDS.minX, z] as Point2,
      to: [CITY_BOUNDS.maxX, z] as Point2,
      width: 1.4,
      surfaceY: 0.36
    }))
  ];
  const random = seededRandom(81731);
  const motions: LinearMotion[] = [];
  pathDefinitions.forEach((path, pathIndex) => {
    for (const direction of [1, -1] as const) {
      const length = Math.hypot(path.to[0] - path.from[0], path.to[1] - path.from[1]);
      motions.push({
        from: path.from,
        to: path.to,
        length,
        progress: (random() + pathIndex * 0.173 + (direction === 1 ? 0 : 0.43)) % 1,
        speed: 3.1 + random() * 3.4,
        direction,
        laneOffset:
          direction * THREE.MathUtils.clamp(path.width * 0.2, 0.23, 0.72),
        surfaceY: path.surfaceY
      });
    }
  });
  motions.splice(Math.max(8, Math.ceil(motions.length * density)));

  const geometry = new THREE.BoxGeometry(1, 1, 1);
  const bodyMaterial = new THREE.MeshStandardMaterial({
    color: 0xffffff,
    roughness: 0.42,
    metalness: 0.28
  });
  const body = new THREE.InstancedMesh(geometry, bodyMaterial, motions.length);
  const cabin = new THREE.InstancedMesh(geometry, palette.glassCyan, motions.length);
  const lamps = new THREE.InstancedMesh(geometry, palette.orangeGlow, motions.length);
  const vehicleColors = [
    0xf4f6f2,
    0x00cfee,
    0xff6528,
    0xac56ff,
    0x39df78,
    0xffc43d
  ];
  motions.forEach((_, index) => {
    body.setColorAt(index, new THREE.Color(vehicleColors[index % vehicleColors.length]));
  });
  if (body.instanceColor) body.instanceColor.needsUpdate = true;
  for (const mesh of [body, cabin, lamps]) {
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    mesh.frustumCulled = false;
    scene.add(mesh);
  }
  body.castShadow = true;
  body.receiveShadow = true;
  cabin.castShadow = false;
  lamps.castShadow = false;

  const runtime = { body, cabin, lamps, motions, dummy: new THREE.Object3D() };
  updateVehicleLife(runtime, 0);
  return runtime;
}

function updateVehicleLife(runtime: VehicleRuntime, delta: number) {
  runtime.motions.forEach((motion, index) => {
    motion.progress =
      (motion.progress + (delta * motion.speed * motion.direction) / motion.length + 1) % 1;
    const dx = motion.to[0] - motion.from[0];
    const dz = motion.to[1] - motion.from[1];
    const unitX = dx / motion.length;
    const unitZ = dz / motion.length;
    const normalX = -unitZ;
    const normalZ = unitX;
    const x = motion.from[0] + dx * motion.progress + normalX * motion.laneOffset;
    const z = motion.from[1] + dz * motion.progress + normalZ * motion.laneOffset;
    const forwardX = unitX * motion.direction;
    const forwardZ = unitZ * motion.direction;
    const rotation = -Math.atan2(forwardZ, forwardX);
    const bodyLength = 1.18 + (index % 4) * 0.12;
    const bodyWidth = 0.5 + (index % 3) * 0.05;
    setInstanceTransform(
      runtime.body,
      index,
      runtime.dummy,
      [x, motion.surfaceY, z],
      [bodyLength, 0.34, bodyWidth],
      rotation
    );
    setInstanceTransform(
      runtime.cabin,
      index,
      runtime.dummy,
      [x - forwardX * 0.08, motion.surfaceY + 0.27, z - forwardZ * 0.08],
      [bodyLength * 0.5, 0.24, bodyWidth * 0.84],
      rotation
    );
    setInstanceTransform(
      runtime.lamps,
      index,
      runtime.dummy,
      [
        x + forwardX * bodyLength * 0.52,
        motion.surfaceY + 0.03,
        z + forwardZ * bodyLength * 0.52
      ],
      [0.08, 0.1, bodyWidth * 0.72],
      rotation
    );
  });
  runtime.body.instanceMatrix.needsUpdate = true;
  runtime.cabin.instanceMatrix.needsUpdate = true;
  runtime.lamps.instanceMatrix.needsUpdate = true;
}

function createPedestrianLife(scene: THREE.Scene, density = 1): PedestrianRuntime {
  const random = seededRandom(32291);
  const motions: PedestrianMotion[] = [];
  const pedestrianCount = Math.max(12, Math.round(84 * density));
  const hubPedestrianCount = Math.max(4, Math.round(pedestrianCount * (30 / 84)));
  const parkPedestrianCount = Math.max(4, Math.round(pedestrianCount * (36 / 84)));
  for (let index = 0; index < pedestrianCount; index += 1) {
    let center: Point2;
    let radius: number;
    let groundY: number;
    if (index < hubPedestrianCount) {
      center = HUB;
      radius = 2.4 + random() * 7.4;
      groundY = 0.34;
    } else if (index < hubPedestrianCount + parkPedestrianCount) {
      center = PARK;
      radius = 2.2 + random() * 11.2;
      groundY = 0.3;
    } else {
      const anchor = ANCHOR_SPECS[
        (index - hubPedestrianCount - parkPedestrianCount) % ANCHOR_SPECS.length
      ];
      center = [
        anchor.position[0] + (random() - 0.5) * anchor.lot[0] * 0.74,
        anchor.position[1] + anchor.lot[1] * 0.56
      ];
      radius = 0.25 + random() * 1.2;
      groundY = 0.32;
    }
    motions.push({
      center,
      radius,
      angle: random() * Math.PI * 2,
      speed: 0.11 + random() * 0.22,
      phase: random() * Math.PI * 2,
      groundY,
      idle: index % 7 === 0
    });
  }

  const bodyMaterial = new THREE.MeshStandardMaterial({
    color: 0xffffff,
    roughness: 0.68,
    metalness: 0.03
  });
  const headMaterial = new THREE.MeshStandardMaterial({
    color: 0xf0c7a5,
    roughness: 0.8,
    metalness: 0
  });
  const bodies = new THREE.InstancedMesh(
    new THREE.CylinderGeometry(0.115, 0.15, 0.62, 8),
    bodyMaterial,
    motions.length
  );
  const heads = new THREE.InstancedMesh(
    new THREE.SphereGeometry(0.155, 8, 6),
    headMaterial,
    motions.length
  );
  const pedestrianColors = [0x00d7f4, 0xff6b2d, 0xb657ff, 0x36e77b, 0xffd43b];
  motions.forEach((_, index) => {
    bodies.setColorAt(index, new THREE.Color(pedestrianColors[index % pedestrianColors.length]));
  });
  if (bodies.instanceColor) bodies.instanceColor.needsUpdate = true;
  for (const mesh of [bodies, heads]) {
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    mesh.frustumCulled = false;
    mesh.castShadow = true;
    scene.add(mesh);
  }
  const runtime = { bodies, heads, motions, dummy: new THREE.Object3D() };
  updatePedestrianLife(runtime, 0, 0);
  return runtime;
}

function updatePedestrianLife(runtime: PedestrianRuntime, delta: number, elapsed: number) {
  runtime.motions.forEach((motion, index) => {
    if (!motion.idle) motion.angle += delta * motion.speed;
    const sway = motion.idle ? Math.sin(elapsed * 1.35 + motion.phase) * 0.08 : 0;
    const x = motion.center[0] + Math.cos(motion.angle) * motion.radius + sway;
    const z = motion.center[1] + Math.sin(motion.angle) * motion.radius;
    const rotation = -motion.angle + Math.PI / 2;
    const bob = Math.max(0, Math.sin(elapsed * 5 + motion.phase)) * 0.035;
    setInstanceTransform(
      runtime.bodies,
      index,
      runtime.dummy,
      [x, motion.groundY + 0.34 + bob, z],
      [1, 1, 1],
      rotation
    );
    setInstanceTransform(
      runtime.heads,
      index,
      runtime.dummy,
      [x, motion.groundY + 0.79 + bob, z],
      [1, 1, 1]
    );
  });
  runtime.bodies.instanceMatrix.needsUpdate = true;
  runtime.heads.instanceMatrix.needsUpdate = true;
}

function createPulseRuntime(
  scene: THREE.Scene,
  material: THREE.Material,
  motions: PulseMotion[]
): PulseRuntime {
  const mesh = new THREE.InstancedMesh(
    new THREE.BoxGeometry(1, 1, 1),
    material,
    motions.length
  );
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  mesh.frustumCulled = false;
  scene.add(mesh);
  const runtime = { mesh, motions, dummy: new THREE.Object3D() };
  updatePulseRuntime(runtime, 0);
  return runtime;
}

function updatePulseRuntime(runtime: PulseRuntime, delta: number) {
  runtime.motions.forEach((motion, index) => {
    motion.progress =
      (motion.progress + (delta * motion.speed * motion.direction) / motion.length + 1) % 1;
    const dx = motion.to[0] - motion.from[0];
    const dz = motion.to[1] - motion.from[1];
    const x = motion.from[0] + dx * motion.progress;
    const z = motion.from[1] + dz * motion.progress;
    setInstanceTransform(
      runtime.mesh,
      index,
      runtime.dummy,
      [x, motion.surfaceY, z],
      motion.scale,
      -Math.atan2(dz * motion.direction, dx * motion.direction)
    );
  });
  runtime.mesh.instanceMatrix.needsUpdate = true;
}

function createWaterPulses(scene: THREE.Scene, palette: MaterialPalette): PulseRuntime {
  const paths: Array<{ from: Point2; to: Point2 }> = WATER_BANDS.map(
    ({ center, size }): { from: Point2; to: Point2 } =>
      size[0] > size[1]
        ? {
            from: [center[0] - size[0] / 2 + 3, center[1]],
            to: [center[0] + size[0] / 2 - 3, center[1]]
          }
        : {
            from: [center[0], center[1] - size[1] / 2 + 3],
            to: [center[0], center[1] + size[1] / 2 - 3]
          }
  );
  const random = seededRandom(90817);
  const motions: PulseMotion[] = [];
  paths.forEach((path, pathIndex) => {
    for (let index = 0; index < 12; index += 1) {
      const length = Math.hypot(path.to[0] - path.from[0], path.to[1] - path.from[1]);
      motions.push({
        from: path.from,
        to: path.to,
        length,
        progress: (index / 12 + random() * 0.05) % 1,
        speed: 2.2 + random() * 2,
        direction: (index + pathIndex) % 2 === 0 ? 1 : -1,
        laneOffset: 0,
        surfaceY: 0.105,
        scale: [1.8, 0.025, 0.18]
      });
    }
  });
  return createPulseRuntime(scene, palette.cyanGlow, motions);
}

function createRoutePulses(scene: THREE.Scene, palette: MaterialPalette): PulseRuntime {
  const random = seededRandom(71603);
  const motions: PulseMotion[] = [];
  ROAD_SEGMENTS.forEach((path, pathIndex) => {
    for (const direction of [1, -1] as const) {
      const length = Math.hypot(path.to[0] - path.from[0], path.to[1] - path.from[1]);
      motions.push({
        from: path.from,
        to: path.to,
        length,
        progress: (random() + pathIndex * 0.137) % 1,
        speed: 4.5 + random() * 3,
        direction,
        laneOffset: 0,
        surfaceY: 0.43,
        scale: [0.62, 0.08, 0.14]
      });
    }
  });
  return createPulseRuntime(scene, palette.mobilityGlow, motions);
}

function createRooftopSignals(scene: THREE.Scene): SignalRuntime {
  const roofHeights: Record<BuildingId, number> = {
    intake: 4.15,
    automation: 14.25,
    hmi: 9.7,
    electrical: 5.2,
    qa: 6.25,
    documentation: 13.45,
    approval: 5.3,
    release: 7.55
  };
  const signals: SignalSpec[] = [];
  ANCHOR_SPECS.forEach((anchor, index) => {
    for (const offset of [-0.65, 0.65]) {
      signals.push({
        position: [
          anchor.position[0] + offset,
          roofHeights[anchor.id],
          anchor.position[1]
        ],
        phase: index * 0.91 + (offset > 0 ? 0.7 : 0)
      });
    }
  });
  signals.push({ position: [LANDMARK[0], 41, LANDMARK[1]], phase: 2.4 });
  const material = new THREE.MeshBasicMaterial({
    color: 0xffffff,
    transparent: true,
    opacity: 0.9,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    toneMapped: false
  });
  const mesh = new THREE.InstancedMesh(
    new THREE.SphereGeometry(0.16, 8, 6),
    material,
    signals.length
  );
  signals.forEach((_, index) => {
    const anchor =
      index >= ANCHOR_SPECS.length * 2
        ? "cyan"
        : anchorAccentFor(ANCHOR_SPECS[Math.floor(index / 2)].id);
    mesh.setColorAt(index, new THREE.Color(accentHex(anchor)));
  });
  if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  mesh.frustumCulled = false;
  scene.add(mesh);
  const runtime = { mesh, signals, dummy: new THREE.Object3D() };
  updateRooftopSignals(runtime, 0);
  return runtime;
}

function updateRooftopSignals(runtime: SignalRuntime, elapsed: number) {
  runtime.signals.forEach((signal, index) => {
    const pulse = 0.78 + (Math.sin(elapsed * 2.3 + signal.phase) + 1) * 0.32;
    setInstanceTransform(
      runtime.mesh,
      index,
      runtime.dummy,
      signal.position,
      [pulse, pulse, pulse]
    );
  });
  runtime.mesh.instanceMatrix.needsUpdate = true;
}

function isReserved(x: number, z: number) {
  if (Math.hypot(x - HUB[0], z - HUB[1]) < 14.2) return true;
  if (Math.hypot(x - LANDMARK[0], z - LANDMARK[1]) < 12.5) return true;
  if (Math.hypot(x - PARK[0], z - PARK[1]) < 15.6) return true;
  if (Math.hypot(x + 88, z - 50) < 7.8) return true;
  if (Math.hypot(x - 91, z - 53) < 7.2) return true;
  if (isInsideWater(x, z)) return true;
  if (
    FEATURE_SITES.some(
      ({ center, size }) =>
        Math.abs(x - center[0]) < size[0] / 2 &&
        Math.abs(z - center[1]) < size[1] / 2
    )
  ) {
    return true;
  }
  if (
    ANCHOR_SPECS.some(
      (anchor) =>
        Math.abs(x - anchor.position[0]) < anchor.lot[0] / 2 + 2 &&
        Math.abs(z - anchor.position[1]) < anchor.lot[1] / 2 + 2
    )
  ) {
    return true;
  }
  if (isNearMetroStreet(x, z)) return true;
  return ROAD_SEGMENTS.some(
    (segment) => pointToSegmentDistance([x, z], segment) < segment.width / 2 + 1.5
  );
}

function createProceduralBuildings(scene: THREE.Scene, palette: MaterialPalette) {
  const whiteBodies: THREE.Matrix4[] = [];
  const lightBodies: THREE.Matrix4[] = [];
  const concretePodiums: THREE.Matrix4[] = [];
  const contactShadows: THREE.Matrix4[] = [];
  const glassBands: Record<DistrictAccent, THREE.Matrix4[]> = {
    cyan: [],
    orange: [],
    purple: []
  };
  const entranceDoors: Record<DistrictAccent, THREE.Matrix4[]> = {
    cyan: [],
    orange: [],
    purple: []
  };
  const roofCaps: THREE.Matrix4[] = [];
  const rooftopUnits: THREE.Matrix4[] = [];
  const rooftopAccents: Record<DistrictAccent, THREE.Matrix4[]> = {
    cyan: [],
    orange: [],
    purple: []
  };

  for (const district of DISTRICTS) {
    const random = seededRandom(district.seed);
    const districtAccent = district.accent;
    const pitch = 3.55;
    const columns = Math.floor((district.size[0] - 6) / pitch);
    const rows = Math.floor((district.size[1] - 6) / pitch);
    for (let column = 0; column < columns; column += 1) {
      for (let row = 0; row < rows; row += 1) {
        if (random() > district.density) continue;
        const localX = (column - (columns - 1) / 2) * pitch + (random() - 0.5) * 0.34;
        const localZ = (row - (rows - 1) / 2) * pitch + (random() - 0.5) * 0.34;
        if (Math.abs(localX) < 1.45 || Math.abs(localZ) < 1.45) continue;
        const x = district.center[0] + localX;
        const z = district.center[1] + localZ;
        if (isReserved(x, z)) continue;

        const edgeDistance = Math.min(
          1,
          Math.hypot(localX / (district.size[0] * 0.5), localZ / (district.size[1] * 0.5))
        );
        const centerBoost = 0.64 + (1 - edgeDistance) * district.skyline;
        let height =
          (district.minHeight +
            Math.pow(random(), 1.55) * (district.maxHeight - district.minHeight)) *
          centerBoost;
        if (random() > 0.945) height *= 1.34;
        height = THREE.MathUtils.clamp(height, district.minHeight, district.maxHeight * 1.18);

        const width = 2.15 + random() * 1.15;
        const depth = 2.15 + random() * 1.15;
        const rotation = random() > 0.84 ? Math.PI / 2 : 0;
        const podiumHeight = height > 9 ? 0.65 + random() * 0.55 : 0.28;
        contactShadows.push(
          boxMatrix(x + 0.12, 0.185, z + 0.14, width + 0.72, 0.035, depth + 0.72, rotation)
        );
        if (height > 9) {
          concretePodiums.push(
            boxMatrix(
              x,
              0.2 + podiumHeight / 2,
              z,
              width + 0.7,
              podiumHeight,
              depth + 0.7,
              rotation
            )
          );
        }
        const bodyBottom = 0.2 + podiumHeight;
        const bodyHeight = height - podiumHeight;
        const bodyMatrix = boxMatrix(
          x,
          bodyBottom + bodyHeight / 2,
          z,
          width,
          bodyHeight,
          depth,
          rotation
        );
        (random() > 0.42 ? whiteBodies : lightBodies).push(bodyMatrix);

        const doorWidth = THREE.MathUtils.clamp(width * 0.3, 0.68, 0.96);
        const frontOffset = depth / 2 + (height > 9 ? 0.39 : 0.05);
        entranceDoors[districtAccent].push(
          boxMatrix(
            x + Math.sin(rotation) * frontOffset,
            0.96,
            z + Math.cos(rotation) * frontOffset,
            doorWidth,
            1.48,
            0.085,
            rotation
          )
        );

        const bandCount = height > 13 ? 6 : height > 7 ? 4 : 2;
        for (let band = 1; band <= bandCount; band += 1) {
          const bandY = bodyBottom + (bodyHeight / (bandCount + 1)) * band;
          glassBands[districtAccent].push(
            boxMatrix(
              x,
              bandY,
              z,
              width + 0.055,
              Math.min(0.24, bodyHeight * 0.035 + 0.08),
              depth + 0.055,
              rotation
            )
          );
        }
        if (height > 5.5) {
          for (const localOffset of [-width * 0.24, width * 0.24]) {
            const offsetX = Math.cos(rotation) * localOffset;
            const offsetZ = -Math.sin(rotation) * localOffset;
            glassBands[districtAccent].push(
              boxMatrix(
                x + offsetX,
                bodyBottom + bodyHeight * 0.51,
                z + offsetZ,
                0.075,
                bodyHeight * 0.9,
                depth + 0.065,
                rotation
              )
            );
          }
        }
        roofCaps.push(
          boxMatrix(x, height + 0.22, z, width + 0.18, 0.24, depth + 0.18, rotation)
        );
        if (random() > 0.82) {
          rooftopAccents[districtAccent].push(
            boxMatrix(
              x,
              height + 0.365,
              z,
              width * 0.58,
              0.055,
              depth * 0.58,
              rotation
            )
          );
        }
        if (height > 8 && random() > 0.72) {
          const unitWidth = width * (0.28 + random() * 0.18);
          const unitDepth = depth * (0.28 + random() * 0.18);
          rooftopUnits.push(
            boxMatrix(
              x + (random() - 0.5) * width * 0.28,
              height + 0.48,
              z + (random() - 0.5) * depth * 0.28,
              unitWidth,
              0.38,
              unitDepth,
              rotation
            )
          );
        }
      }
    }
  }

  const unitBox = new THREE.BoxGeometry(1, 1, 1);
  addInstancedBoxes(scene, unitBox, contactShadows, palette.contact, false);
  addInstancedBoxes(scene, unitBox, concretePodiums, palette.concrete);
  addInstancedBoxes(scene, unitBox, whiteBodies, palette.white);
  addInstancedBoxes(scene, unitBox, lightBodies, palette.light);
  addInstancedBoxes(scene, unitBox, glassBands.cyan, palette.glassCyan, false);
  addInstancedBoxes(scene, unitBox, glassBands.orange, palette.glassOrange, false);
  addInstancedBoxes(scene, unitBox, glassBands.purple, palette.glassPurple, false);
  addInstancedBoxes(scene, unitBox, entranceDoors.cyan, palette.glassCyan, false);
  addInstancedBoxes(scene, unitBox, entranceDoors.orange, palette.glassOrange, false);
  addInstancedBoxes(scene, unitBox, entranceDoors.purple, palette.glassPurple, false);
  addInstancedBoxes(scene, unitBox, roofCaps, palette.roof);
  addInstancedBoxes(scene, unitBox, rooftopUnits, palette.blackSteel);
  addInstancedBoxes(scene, unitBox, rooftopAccents.cyan, palette.cyanGlow, false);
  addInstancedBoxes(scene, unitBox, rooftopAccents.orange, palette.orangeGlow, false);
  addInstancedBoxes(scene, unitBox, rooftopAccents.purple, palette.purpleGlow, false);
}

function createCentralLandmark(scene: THREE.Scene, palette: MaterialPalette) {
  const group = new THREE.Group();
  group.position.set(LANDMARK[0], 0.2, LANDMARK[1]);
  addBox(group, [11, 1.2, 9], [0, 0.6, 0], palette.concrete);
  addBox(group, [8.4, 4.2, 7.2], [0, 3.3, 0], palette.light);
  addBox(group, [6.5, 18, 5.8], [0, 14.4, 0], palette.white);
  addFacadeBands(group, 6.55, 5.85, 18, 11, palette.glassCyan, 0, 0);
  addBox(group, [5.15, 6.8, 4.55], [0, 26.8, 0], palette.light);
  addFacadeBands(group, 5.2, 4.6, 6.8, 4, palette.glassCyan, 0, 0);
  addBox(group, [3.5, 3.6, 3.1], [0, 32, 0], palette.white);
  addFacadeBands(group, 3.55, 3.15, 3.6, 2, palette.glassCyan, 0, 0);
  addCylinder(group, 0.18, 6.4, [0, 37, 0], palette.cyanEmissive, 12);
  addCylinder(group, 0.42, 0.26, [0, 40.2, 0], palette.cyanEmissive, 16);
  addBox(group, [8.7, 0.055, 7.5], [0, 5.45, 0], palette.cyanGlow, false);
  const crownGlow = new THREE.Mesh(
    new THREE.TorusGeometry(2.2, 0.07, 7, 48),
    palette.cyanGlow
  );
  crownGlow.rotation.x = Math.PI / 2;
  crownGlow.position.y = 34.05;
  group.add(crownGlow);
  for (const side of [-1, 1]) {
    addBox(group, [0.22, 17.7, 6.15], [side * 3.32, 14.4, 0], palette.light);
    addBox(group, [6.85, 17.7, 0.19], [0, 14.4, side * 2.99], palette.light);
  }
  for (const [x, z, width, depth, height, rows] of [
    [-8.4, -3.5, 3.7, 3.4, 25, 12],
    [8.1, -3.2, 4.1, 3.7, 22, 10],
    [-8.7, 4.5, 3.2, 3.2, 18, 8],
    [8.5, 4.3, 3.4, 3.1, 17, 8],
    [-3.7, 7.3, 3.1, 3.1, 21, 10],
    [4.1, 7.2, 2.9, 3.1, 16, 7]
  ] as Array<readonly [number, number, number, number, number, number]>) {
    addBox(group, [width + 0.7, 0.9, depth + 0.7], [x, 0.45, z], palette.concrete);
    addBox(group, [width, height, depth], [x, 0.9 + height / 2, z], palette.white);
    addFacadeBands(group, width + 0.04, depth + 0.04, height, rows, palette.glassCyan, x, z);
    addBox(group, [width + 0.2, 0.24, depth + 0.2], [x, height + 1.02, z], palette.roof);
  }
  scene.add(group);
}

function createRoundabout(scene: THREE.Scene, palette: MaterialPalette) {
  const plaza = new THREE.Mesh(new THREE.CircleGeometry(8.3, 64), palette.concrete);
  plaza.rotation.x = -Math.PI / 2;
  plaza.position.set(HUB[0], 0.27, HUB[1]);
  plaza.receiveShadow = true;
  scene.add(plaza);

  const road = new THREE.Mesh(new THREE.TorusGeometry(10.4, 2.05, 12, 72), palette.asphalt);
  road.rotation.x = -Math.PI / 2;
  road.position.set(HUB[0], 0.31, HUB[1]);
  road.receiveShadow = true;
  scene.add(road);

  for (const [radius, material] of [
    [8.35, palette.orangeGlow],
    [10.4, palette.cyanGlow],
    [12.42, palette.orangeGlow]
  ] as Array<readonly [number, THREE.MeshBasicMaterial]>) {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(radius, 0.07, 7, 72), material);
    ring.rotation.x = -Math.PI / 2;
    ring.position.set(HUB[0], 0.39, HUB[1]);
    scene.add(ring);
  }

  for (const radius of [2.4, 4.7, 7]) {
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(radius, 0.045, 6, 64),
      palette.cyanGlow
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.set(HUB[0], 0.34, HUB[1]);
    scene.add(ring);
  }
  addCylinder(scene, 1.2, 0.5, [HUB[0], 0.53, HUB[1]], palette.cyanEmissive, 32);
  addCylinder(scene, 0.16, 5.5, [HUB[0], 3.45, HUB[1]], palette.cyanEmissive, 16);
  addCylinder(scene, 0.48, 0.22, [HUB[0], 6.28, HUB[1]], palette.cyanGlow, 24);
}

function createIndustrialUtilities(scene: THREE.Scene, palette: MaterialPalette) {
  const group = new THREE.Group();
  group.position.set(-88, 0.2, 50);
  for (const [x, z, radius, height] of [
    [-3.2, -2.5, 1.5, 3.2],
    [0.5, -2.4, 1.2, 2.7],
    [3.6, -1.9, 1.35, 3],
    [-1.4, 2, 1.05, 2.5]
  ] as Array<readonly [number, number, number, number]>) {
    addCylinder(group, radius, height, [x, height / 2, z], palette.light, 24);
    const cap = new THREE.Mesh(
      new THREE.ConeGeometry(radius * 1.02, 0.65, 24),
      palette.roof
    );
    cap.position.set(x, height + 0.32, z);
    cap.castShadow = true;
    group.add(cap);
  }
  for (const x of [-4.6, 4.8]) {
    addCylinder(group, 0.38, 8.5, [x, 4.25, 3.7], palette.concrete, 18);
    addCylinder(group, 0.5, 0.38, [x, 8.58, 3.7], palette.cyanEmissive, 18);
  }
  addBox(group, [8.8, 2.6, 3.7], [0, 1.3, 5.7], palette.white);
  addFacadeBands(group, 8.85, 3.75, 2.6, 2, palette.glassCyan, 0, 5.7);
  scene.add(group);
}

function createLogisticsDetails(scene: THREE.Scene, palette: MaterialPalette) {
  const group = new THREE.Group();
  group.position.set(92, 0.2, 53);
  addBox(group, [11.5, 3.4, 7], [0, 1.7, 0], palette.light);
  addBox(group, [11.9, 0.22, 7.4], [0, 3.51, 0], palette.orangeEmissive);
  for (let index = 0; index < 5; index += 1) {
    addBox(
      group,
      [1.55, 1.9, 0.12],
      [-4.6 + index * 2.3, 1.05, 3.56],
      palette.glassOrange,
      false
    );
  }
  for (const [x, z] of [
    [-4.5, 5.1],
    [-1.5, 5.1],
    [1.5, 5.1],
    [4.5, 5.1],
    [-3, -5],
    [2, -5]
  ] as Point2[]) {
    addBox(group, [2.4, 1.2, 1.1], [x, 0.6, z], palette.white);
    addBox(group, [2.48, 0.11, 1.18], [x, 1.24, z], palette.blackSteel);
  }
  scene.add(group);
}

function createExpandedInfrastructure(scene: THREE.Scene, palette: MaterialPalette) {
  const transform = new THREE.Object3D();
  const compose = (
    x: number,
    y: number,
    z: number,
    width: number,
    height: number,
    depth: number,
    rotationX = 0,
    rotationY = 0,
    rotationZ = 0
  ) => {
    transform.position.set(x, y, z);
    transform.rotation.set(rotationX, rotationY, rotationZ);
    transform.scale.set(width, height, depth);
    transform.updateMatrix();
    return transform.matrix.clone();
  };

  const turbineSites: Point2[] = [
    [-121, 112],
    [-112, 118],
    [-101, 114],
    [-118, 126],
    [-107, 129],
    [-95, 123],
    [2, -172],
    [16, -176],
    [30, -172],
    [44, -168],
    [57, -161],
    [70, -166],
    [82, -158]
  ];
  const turbineTowers: THREE.Matrix4[] = [];
  const turbineNacelles: THREE.Matrix4[] = [];
  const turbineHubs: THREE.Matrix4[] = [];
  const turbineBlades: THREE.Matrix4[] = [];
  turbineSites.forEach(([x, z], index) => {
    const height = 8.6 + (index % 3) * 1.25;
    const yaw = -0.48 + (index % 4) * 0.12;
    const rotorX = x + Math.cos(yaw) * 0.66;
    const rotorZ = z - Math.sin(yaw) * 0.66;
    turbineTowers.push(compose(x, height / 2, z, 0.34, height, 0.34));
    turbineNacelles.push(compose(x, height, z, 1.35, 0.44, 0.5, 0, yaw));
    turbineHubs.push(compose(rotorX, height, rotorZ, 0.34, 0.34, 0.34));
    for (let blade = 0; blade < 3; blade += 1) {
      const angle = blade * ((Math.PI * 2) / 3) + (index % 2) * 0.24;
      const localX = -Math.sin(angle) * 2.05;
      const localY = Math.cos(angle) * 2.05;
      turbineBlades.push(
        compose(
          rotorX + Math.cos(yaw) * localX,
          height + localY,
          rotorZ - Math.sin(yaw) * localX,
          0.16,
          4.2,
          0.08,
          0,
          yaw,
          angle
        )
      );
    }
  });
  addStaticInstances(
    scene,
    new THREE.CylinderGeometry(1, 0.72, 1, 12),
    turbineTowers,
    palette.light,
    false
  );
  addInstancedBoxes(scene, new THREE.BoxGeometry(1, 1, 1), turbineNacelles, palette.white, false);
  addStaticInstances(
    scene,
    new THREE.SphereGeometry(1, 10, 8),
    turbineHubs,
    palette.cyanEmissive,
    false
  );
  addInstancedBoxes(scene, new THREE.BoxGeometry(1, 1, 1), turbineBlades, palette.light, false);

  const solarPanels: THREE.Matrix4[] = [];
  const solarSupports: THREE.Matrix4[] = [];
  for (const [centerX, centerZ, columns, rows] of [
    [-76, 123, 8, 5],
    [-32, 124, 7, 4]
  ] as Array<readonly [number, number, number, number]>) {
    for (let column = 0; column < columns; column += 1) {
      for (let row = 0; row < rows; row += 1) {
        const x = centerX + (column - (columns - 1) / 2) * 2.7;
        const z = centerZ + (row - (rows - 1) / 2) * 2.25;
        solarSupports.push(compose(x, 0.38, z, 0.12, 0.65, 0.12));
        solarPanels.push(compose(x, 0.82, z, 2.3, 0.12, 1.45, -0.24, 0.08));
      }
    }
  }
  const solarMaterial = new THREE.MeshPhysicalMaterial({
    color: 0x0b4260,
    emissive: 0x007699,
    emissiveIntensity: 0.34,
    roughness: 0.24,
    metalness: 0.56,
    clearcoat: 0.7
  });
  addInstancedBoxes(scene, new THREE.BoxGeometry(1, 1, 1), solarSupports, palette.blackSteel, false);
  addInstancedBoxes(scene, new THREE.BoxGeometry(1, 1, 1), solarPanels, solarMaterial, false);

  const gateway = new THREE.Group();
  gateway.position.set(-191, 0.18, -98);
  addBox(gateway, [25, 0.2, 17], [0, 0.1, 0], palette.concrete, false);
  for (const [x, z, width, height] of [
    [-7.4, -1.2, 3.2, 22],
    [0, 1.4, 3.7, 18],
    [7.2, -0.8, 3, 20]
  ] as Array<readonly [number, number, number, number]>) {
    addBox(gateway, [width, height, width], [x, 0.25 + height / 2, z], palette.white);
    addFacadeBands(gateway, width + 0.05, width + 0.05, height, 9, palette.glassCyan, x, z);
    addCylinder(gateway, 0.1, 3.2, [x, height + 1.8, z], palette.cyanEmissive, 10);
  }
  scene.add(gateway);

  const research = new THREE.Group();
  research.position.set(-124, 0.18, -98);
  addBox(research, [24, 0.18, 17], [0, 0.1, 0], palette.park, false);
  for (const [x, z] of [
    [-7, -4],
    [0, -4],
    [7, -4],
    [-4, 4],
    [4, 4]
  ] as Point2[]) {
    addBox(research, [5.2, 2.2, 4.2], [x, 1.25, z], palette.white);
    addBox(research, [5.4, 0.18, 4.4], [x, 2.45, z], palette.purpleGlow, false);
  }
  scene.add(research);

  const energy = new THREE.Group();
  energy.position.set(29, 0.18, -165);
  addBox(energy, [30, 0.2, 17], [0, 0.1, 0], palette.concrete, false);
  addBox(energy, [12, 4.2, 7.5], [-3, 2.2, 0], palette.white);
  addFacadeBands(energy, 12.05, 7.55, 4.2, 3, palette.glassCyan, -3, 0);
  for (const x of [5, 9]) {
    addCylinder(energy, 1.7, 3.4, [x, 1.8, -2], palette.light, 24);
    addCylinder(energy, 0.3, 7.8, [x, 4, 4], palette.concrete, 14);
    addCylinder(energy, 0.43, 0.28, [x, 8, 4], palette.cyanEmissive, 14);
  }
  scene.add(energy);

  const treatment = new THREE.Group();
  treatment.position.set(124, 0.18, -168);
  addBox(treatment, [30, 0.2, 21], [0, 0.1, 0], palette.concrete, false);
  for (const [x, z, radius] of [
    [-9, -5, 2.8],
    [-2, -5, 2.8],
    [5, -5, 2.8],
    [-7, 3, 2.3],
    [0, 3, 2.3],
    [7, 3, 2.3]
  ] as Array<readonly [number, number, number]>) {
    addCylinder(treatment, radius, 0.52, [x, 0.38, z], palette.concrete, 32);
    addCylinder(treatment, radius * 0.8, 0.1, [x, 0.7, z], palette.water, 32);
  }
  addBox(treatment, [7.6, 3.2, 6], [10, 1.75, 2.5], palette.white);
  addFacadeBands(treatment, 7.65, 6.05, 3.2, 2, palette.glassCyan, 10, 2.5);
  scene.add(treatment);

  const testing = new THREE.Group();
  testing.position.set(-32, 0.2, 124);
  addBox(testing, [27, 0.22, 18], [0, 0.1, 0], palette.concrete, false);
  addBox(testing, [8, 3, 5], [-7, 1.65, -4], palette.white);
  addBox(testing, [7, 2.4, 5], [6, 1.35, 3.5], palette.light);
  addCylinder(testing, 0.16, 8.5, [3, 4.4, -4], palette.light, 12);
  addCylinder(testing, 0.55, 0.22, [3, 8.72, -4], palette.cyanEmissive, 16);
  scene.add(testing);

  const dataCenter = new THREE.Group();
  dataCenter.position.set(30, 0.2, 119);
  addBox(dataCenter, [30, 0.2, 18], [0, 0.1, 0], palette.concrete, false);
  for (const [x, z] of [
    [-8, -4.8],
    [0, -4.8],
    [8, -4.8],
    [-8, 4.8],
    [0, 4.8],
    [8, 4.8]
  ] as Point2[]) {
    addBox(dataCenter, [6.5, 3.1, 6.2], [x, 1.75, z], palette.light);
    addBox(dataCenter, [6.8, 0.2, 6.5], [x, 3.42, z], palette.cyanEmissive, false);
    for (const unitX of [-1.6, 0, 1.6]) {
      addBox(dataCenter, [1, 0.55, 1.2], [x + unitX, 3.8, z], palette.roof, false);
    }
  }
  scene.add(dataCenter);

  const supply = new THREE.Group();
  supply.position.set(62, 0.2, 86);
  addBox(supply, [31, 0.22, 20], [0, 0.1, 0], palette.concrete, false);
  addBox(supply, [20, 4.5, 11], [0, 2.35, 0], palette.white);
  addBox(supply, [20.5, 0.34, 11.5], [0, 4.77, 0], palette.orangeEmissive);
  for (let bay = 0; bay < 7; bay += 1) {
    addBox(supply, [1.8, 2.1, 0.12], [-7.8 + bay * 2.6, 1.45, 5.56], palette.glassOrange, false);
  }
  scene.add(supply);

  const maintenance = new THREE.Group();
  maintenance.position.set(131, 0.2, 52);
  addBox(maintenance, [29, 0.2, 20], [0, 0.1, 0], palette.concrete, false);
  for (let row = 0; row < 3; row += 1) {
    addBox(maintenance, [22, 2.4, 4.2], [0, 1.3, -6 + row * 6], palette.light);
    for (let bay = 0; bay < 6; bay += 1) {
      addBox(
        maintenance,
        [2.4, 1.45, 0.1],
        [-8.3 + bay * 3.3, 0.95, -3.86 + row * 6],
        palette.glassOrange,
        false
      );
    }
  }
  scene.add(maintenance);

  const containerOrange: THREE.Matrix4[] = [];
  const containerCyan: THREE.Matrix4[] = [];
  const containerLight: THREE.Matrix4[] = [];
  for (let row = 0; row < 5; row += 1) {
    for (let column = 0; column < 11; column += 1) {
      const stack = 1 + ((row * 3 + column * 5) % 3);
      for (let level = 0; level < stack; level += 1) {
        const matrix = boxMatrix(70 + column * 5, 0.72 + level * 1.35, 121 + row * 2.7, 4.2, 1.2, 2.1);
        [containerOrange, containerCyan, containerLight][(row + column) % 3].push(matrix);
      }
    }
  }
  addInstancedBoxes(scene, new THREE.BoxGeometry(1, 1, 1), containerOrange, palette.orangeEmissive, false);
  addInstancedBoxes(scene, new THREE.BoxGeometry(1, 1, 1), containerCyan, palette.cyanEmissive, false);
  addInstancedBoxes(scene, new THREE.BoxGeometry(1, 1, 1), containerLight, palette.light, false);
  for (const x of [80, 101, 122]) {
    const crane = new THREE.Group();
    crane.position.set(x, 0.25, 116);
    addBox(crane, [0.7, 10, 0.7], [0, 5, 0], palette.light);
    addBox(crane, [10, 0.65, 0.65], [4.6, 9.8, 0], palette.orangeEmissive);
    addBox(crane, [0.28, 6.2, 0.28], [8.2, 6.8, 0], palette.blackSteel);
    scene.add(crane);
  }

  const greenStrips: Array<{ center: Point2; size: Point2; vertical?: boolean }> = [
    { center: [-18, -125.4], size: [388, 2.2] },
    { center: [-18, -138.6], size: [388, 2.2] },
    { center: [-18, -82.9], size: [388, 2.2] },
    { center: [-18, -95.1], size: [388, 2.2] },
    { center: [-12, -23], size: [376, 2.1] },
    { center: [-12, -33], size: [376, 2.1] },
    { center: [110.2, -73], size: [2.1, 170], vertical: true },
    { center: [121.8, -73], size: [2.1, 170], vertical: true },
    { center: [-148.7, 18], size: [2.1, 182], vertical: true },
    { center: [-159.3, 18], size: [2.1, 182], vertical: true }
  ];
  const shoreMaterial = new THREE.MeshStandardMaterial({
    color: 0x285c3f,
    emissive: 0x0a2f1d,
    emissiveIntensity: 0.2,
    roughness: 0.9
  });
  const shoreTrees: THREE.Matrix4[] = [];
  const shoreCanopies: THREE.Matrix4[] = [];
  greenStrips.forEach(({ center, size, vertical }, stripIndex) => {
    addBox(scene, [size[0], 0.08, size[1]], [center[0], 0.18, center[1]], shoreMaterial, false);
    const length = vertical ? size[1] : size[0];
    for (let offset = -length / 2 + 5; offset < length / 2 - 4; offset += 7.5) {
      if ((Math.round(offset) + stripIndex) % 3 === 0) continue;
      const x = vertical ? center[0] : center[0] + offset;
      const z = vertical ? center[1] + offset : center[1];
      shoreTrees.push(boxMatrix(x, 0.72, z, 0.14, 1.25, 0.14));
      shoreCanopies.push(boxMatrix(x, 1.78, z, 1.2, 1.45, 1.2, stripIndex * 0.4));
    }
  });
  addInstancedBoxes(scene, new THREE.BoxGeometry(1, 1, 1), shoreTrees, palette.blackSteel, false);
  addInstancedBoxes(scene, new THREE.BoxGeometry(1, 1, 1), shoreCanopies, palette.foliage, false);
}

function createDigitalPark(scene: THREE.Scene, palette: MaterialPalette) {
  const park = new THREE.Mesh(new THREE.CircleGeometry(14.6, 64), palette.park);
  park.rotation.x = -Math.PI / 2;
  park.position.set(PARK[0], 0.25, PARK[1]);
  park.receiveShadow = true;
  scene.add(park);

  for (const radius of [4.8, 9.5, 14.3]) {
    const path = new THREE.Mesh(
      new THREE.TorusGeometry(radius, 0.075, 7, 64),
      palette.greenGlow
    );
    path.rotation.x = -Math.PI / 2;
    path.position.set(PARK[0], 0.31, PARK[1]);
    scene.add(path);
  }

  const random = seededRandom(45451);
  const trunkMatrices: THREE.Matrix4[] = [];
  const canopyMatrices: THREE.Matrix4[][] = [[], [], []];
  for (let index = 0; index < 90; index += 1) {
    const angle = random() * Math.PI * 2;
    const radius = 1.8 + Math.sqrt(random()) * 11.7;
    const x = PARK[0] + Math.cos(angle) * radius;
    const z = PARK[1] + Math.sin(angle) * radius;
    const height = 0.9 + random() * 0.7;
    trunkMatrices.push(boxMatrix(x, 0.25 + height / 2, z, 0.12, height, 0.12));
    canopyMatrices[index % canopyMatrices.length].push(
      boxMatrix(x, 1 + height, z, 0.75, 1.15, 0.75, angle)
    );
  }
  const box = new THREE.BoxGeometry(1, 1, 1);
  const foliageLight = new THREE.MeshStandardMaterial({
    color: 0x87df62,
    emissive: 0x235b1a,
    emissiveIntensity: 0.24,
    roughness: 0.86
  });
  const foliageTeal = new THREE.MeshStandardMaterial({
    color: 0x39bfa2,
    emissive: 0x0b4e40,
    emissiveIntensity: 0.24,
    roughness: 0.84
  });
  addInstancedBoxes(scene, box, trunkMatrices, palette.blackSteel);
  addInstancedBoxes(scene, box, canopyMatrices[0], palette.foliage);
  addInstancedBoxes(scene, box, canopyMatrices[1], foliageLight);
  addInstancedBoxes(scene, box, canopyMatrices[2], foliageTeal);
  addCylinder(scene, 0.45, 2.2, [PARK[0], 1.35, PARK[1]], palette.greenGlow, 18);
}

function addAnchorLot(
  group: THREE.Group,
  spec: AnchorSpec,
  palette: MaterialPalette,
  selection: THREE.MeshBasicMaterial
) {
  const [width, depth] = spec.lot;
  addBox(group, [width, 0.2, depth], [0, 0.2, 0], palette.concrete, false);
  addBox(
    group,
    [width * 0.78, 0.025, depth * 0.72],
    [0.18, 0.312, 0.2],
    palette.contact,
    false
  );
  const selectionPad = new THREE.Mesh(
    new THREE.PlaneGeometry(width + 0.7, depth + 0.7),
    selection
  );
  selectionPad.rotation.x = -Math.PI / 2;
  selectionPad.position.y = 0.325;
  selectionPad.userData.ignoreRaycast = true;
  group.add(selectionPad);
}

function buildAnchorArchitecture(
  group: THREE.Group,
  id: BuildingId,
  palette: MaterialPalette,
  accent: THREE.MeshStandardMaterial
) {
  const windows = accentGlass(palette, anchorAccentFor(id));
  switch (id) {
    case "intake":
      addBox(group, [7.6, 3.2, 5.8], [0, 1.9, 0], palette.white);
      addFacadeBands(group, 7.65, 5.85, 3.2, 2, windows);
      addBox(group, [8.1, 0.24, 6.2], [0, 3.62, 0], palette.roof);
      addBox(group, [4.7, 0.22, 1.4], [0, 2.75, 3.5], accent);
      break;
    case "automation":
      addBox(group, [8.7, 1.3, 7.5], [0, 1, 0], palette.concrete);
      addBox(group, [6.2, 11.8, 5.4], [0, 7.55, 0], palette.white);
      addFacadeBands(group, 6.25, 5.45, 11.8, 7, windows);
      addBox(group, [6.55, 0.3, 5.75], [0, 13.58, 0], accent);
      addCylinder(group, 0.1, 3.2, [0, 15.3, 0], palette.light, 10);
      break;
    case "hmi":
      addBox(group, [7.5, 8.6, 5.8], [0, 4.7, 0], windows);
      for (const x of [-3.55, 3.55]) {
        addBox(group, [0.55, 8.9, 6.15], [x, 4.75, 0], palette.white);
      }
      for (let y = 1.5; y < 9; y += 1.5) {
        addBox(group, [7.25, 0.12, 6.05], [0, y, 0], palette.light, false);
      }
      addBox(group, [7.8, 0.24, 6.3], [0, 9.1, 0], accent);
      break;
    case "electrical":
      addBox(group, [8.6, 4.2, 6], [-1.5, 2.3, 0], palette.white);
      addFacadeBands(group, 8.65, 6.05, 4.2, 3, windows, -1.5, 0);
      addBox(group, [9, 0.24, 6.4], [-1.5, 4.52, 0], accent);
      for (const z of [-2.2, 0, 2.2]) {
        addCylinder(group, 0.85, 3.3, [4.2, 1.85, z], palette.light, 18);
        addCylinder(group, 0.18, 1.5, [4.2, 4.2, z], palette.blackSteel, 10);
      }
      break;
    case "qa":
      addBox(group, [9.8, 5.2, 6.8], [0, 2.8, 0], palette.white);
      addFacadeBands(group, 9.85, 6.85, 5.2, 3, windows);
      addBox(group, [10.2, 0.26, 7.2], [0, 5.54, 0], accent);
      for (const x of [-3.2, 0, 3.2]) {
        addBox(group, [1.8, 0.65, 1.5], [x, 6, -0.6], palette.roof);
      }
      break;
    case "documentation":
      addBox(group, [7.4, 1.2, 6.6], [0, 0.9, 0], palette.concrete);
      addBox(group, [5.9, 11.2, 5.2], [0, 7.1, 0], palette.light);
      addFacadeBands(group, 5.95, 5.25, 11.2, 7, windows);
      addBox(group, [6.2, 0.25, 5.5], [0, 12.82, 0], accent);
      break;
    case "approval":
      addBox(group, [8.6, 4.8, 5.8], [0, 2.7, -0.4], palette.white);
      addFacadeBands(group, 8.65, 5.85, 4.8, 3, windows, 0, -0.4);
      for (const x of [-3, -1, 1, 3]) {
        addCylinder(group, 0.2, 3.6, [x, 2, 3.1], palette.light, 16);
      }
      addBox(group, [8.4, 0.28, 1.3], [0, 4, 3.1], accent);
      break;
    case "release":
      addBox(group, [11.5, 4.1, 7.9], [0, 2.25, 0], palette.light);
      addBox(group, [12, 0.27, 8.4], [0, 4.43, 0], accent);
      for (let index = 0; index < 5; index += 1) {
        addBox(
          group,
          [1.7, 2.2, 0.14],
          [-4.4 + index * 2.2, 1.4, 4.02],
          palette.blackSteel,
          false
        );
      }
      addBox(group, [3.1, 6.8, 3.6], [-3.8, 3.65, -1.6], palette.white);
      addFacadeBands(group, 3.15, 3.65, 6.8, 4, windows, -3.8, -1.6);
      break;
  }

  const entranceByBuilding: Record<
    BuildingId,
    { position: Vec3; width: number; height: number }
  > = {
    intake: { position: [0, 1.16, 2.96], width: 1.16, height: 1.55 },
    automation: { position: [0, 0.94, 3.81], width: 1.08, height: 1.15 },
    hmi: { position: [0, 1.18, 2.96], width: 1.14, height: 1.55 },
    electrical: { position: [-1.5, 1.18, 3.06], width: 1.16, height: 1.55 },
    qa: { position: [0, 1.18, 3.46], width: 1.18, height: 1.55 },
    documentation: { position: [0, 0.86, 3.36], width: 1.08, height: 1.08 },
    approval: { position: [0, 1.18, 2.56], width: 1.16, height: 1.55 },
    release: { position: [4.4, 1.2, 4.1], width: 1.24, height: 1.62 }
  };
  const entrance = entranceByBuilding[id];
  addBox(
    group,
    [entrance.width, entrance.height, 0.13],
    entrance.position,
    windows,
    false
  );
  addBox(
    group,
    [entrance.width + 0.45, 0.12, 0.64],
    [
      entrance.position[0],
      entrance.position[1] + entrance.height / 2 + 0.16,
      entrance.position[2] + 0.25
    ],
    accent,
    false
  );
}

function createAnchorBuilding(
  scene: THREE.Scene,
  spec: AnchorSpec,
  palette: MaterialPalette,
  pickable: THREE.Mesh[]
): BuildingRuntime {
  const group = new THREE.Group();
  group.position.set(spec.position[0], 0, spec.position[1]);
  group.userData.buildingId = spec.id;

  const ambientAccent = accentHex(anchorAccentFor(spec.id));
  const accent = makeAccentMaterial(ambientAccent);
  const selection = new THREE.MeshBasicMaterial({
    color: 0xff6a00,
    transparent: true,
    opacity: 0,
    depthWrite: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    toneMapped: false
  });
  addAnchorLot(group, spec, palette, selection);
  buildAnchorArchitecture(group, spec.id, palette, accent);

  group.traverse((object) => {
    if (object instanceof THREE.Mesh && object.userData.ignoreRaycast !== true) {
      object.userData.buildingId = spec.id;
      pickable.push(object);
    }
  });
  scene.add(group);
  return { accent, selection, ambientAccent };
}

function agentStatusColor(status: WorldAgent["status"]) {
  switch (status) {
    case "blocked":
      return 0xff5f6d;
    case "waiting":
    case "awaiting_approval":
      return 0xffb84c;
    case "completed":
      return 0x4ee58a;
    case "offline":
      return 0x6f7f87;
    case "reviewing":
      return 0xb87aff;
    case "walking":
    case "working":
      return 0x54e6f2;
    default:
      return 0xeaf9fb;
  }
}

function packageStateColor(state: WorldPackage["state"]) {
  switch (state) {
    case "blocked":
      return 0xff5f6d;
    case "approval":
      return 0xffb84c;
    case "review":
    case "validation":
      return 0xb87aff;
    case "released":
      return 0x4ee58a;
    default:
      return 0x54e6f2;
  }
}

function desiredAgentLocation(agent: WorldAgent): LocationId | null {
  return agent.targetLocationId ?? agent.locationId;
}

function createAgentMarker(
  scene: THREE.Scene,
  palette: MaterialPalette,
  agent: WorldAgent,
  locationId: LocationId,
  position: WorldPoint,
  globalIndex: number,
  agentPickable: THREE.Mesh[]
): AgentRuntime {
  const root = new THREE.Group();
  const baseY = 0.34;
  root.position.set(position[0], baseY, position[1]);
  root.scale.setScalar(1.85);
  root.userData.agentId = agent.id;

  addCylinder(root, 0.09, 0.72, [0, 0.39, 0], palette.blackSteel, 10);
  const color = agentStatusColor(agent.status);
  const coreMaterial = new THREE.MeshStandardMaterial({
    color,
    emissive: color,
    emissiveIntensity: 0.72,
    roughness: 0.28,
    metalness: 0.34
  });
  const core = new THREE.Mesh(new THREE.SphereGeometry(0.28, 16, 12), coreMaterial);
  core.position.y = 0.88;
  core.castShadow = true;
  core.userData.agentId = agent.id;
  root.add(core);
  agentPickable.push(core);

  const haloMaterial = new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity: 0.68,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    toneMapped: false
  });
  const halo = new THREE.Mesh(new THREE.TorusGeometry(0.47, 0.04, 8, 28), haloMaterial);
  halo.rotation.x = Math.PI / 2;
  halo.position.y = 0.1;
  halo.userData.agentId = agent.id;
  root.add(halo);
  agentPickable.push(halo);

  const workMaterial = new THREE.MeshStandardMaterial({
    color: 0xe8fbff,
    emissive: 0x7b43c9,
    emissiveIntensity: 0.7,
    roughness: 0.32,
    metalness: 0.38
  });
  const workPackage = new THREE.Mesh(new THREE.BoxGeometry(0.33, 0.26, 0.29), workMaterial);
  workPackage.position.set(0.42, 0.67, 0);
  workPackage.rotation.y = Math.PI / 4;
  workPackage.userData.agentId = agent.id;
  workPackage.castShadow = true;
  root.add(workPackage);
  agentPickable.push(workPackage);

  scene.add(root);
  return {
    id: agent.id,
    root,
    halo,
    coreMaterial,
    workPackage,
    baseY,
    phase: globalIndex * 1.17,
    destinationLocationId: locationId,
    lastSequence: agent.lastEvent.sequence,
    route: [position],
    routeLength: 0,
    routeTravelled: 0,
    destination: position
  };
}

function createPackageMarker(
  scene: THREE.Scene,
  worldPackage: WorldPackage,
  locationId: LocationId,
  position: WorldPoint,
  index: number
): PackageRuntime {
  const root = new THREE.Group();
  const baseY = 0.42;
  root.position.set(position[0], baseY, position[1]);
  root.scale.setScalar(1.38);
  root.userData.packageId = worldPackage.id;
  const color = packageStateColor(worldPackage.state);
  const coreMaterial = new THREE.MeshStandardMaterial({
    color: 0xeefcff,
    emissive: color,
    emissiveIntensity: 0.78,
    roughness: 0.27,
    metalness: 0.42
  });
  const core = new THREE.Mesh(new THREE.BoxGeometry(1.08, 0.64, 0.82), coreMaterial);
  core.position.y = 0.48;
  core.rotation.y = Math.PI / 4;
  core.castShadow = true;
  root.add(core);
  const strapMaterial = new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity: 0.8,
    toneMapped: false
  });
  const strap = new THREE.Mesh(new THREE.BoxGeometry(1.16, 0.08, 0.9), strapMaterial);
  strap.position.y = 0.5;
  strap.rotation.y = Math.PI / 4;
  root.add(strap);
  const halo = new THREE.Mesh(
    new THREE.TorusGeometry(0.82, 0.045, 8, 32),
    new THREE.MeshBasicMaterial({
      color,
      transparent: true,
      opacity: 0.46,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      toneMapped: false
    })
  );
  halo.rotation.x = Math.PI / 2;
  halo.position.y = 0.08;
  root.add(halo);
  scene.add(root);
  return {
    id: worldPackage.id,
    root,
    coreMaterial,
    halo,
    baseY,
    phase: index * 1.41,
    destinationLocationId: locationId,
    lastSequence: worldPackage.lastEvent?.sequence ?? 0,
    route: [position],
    routeLength: 0,
    routeTravelled: 0,
    destination: position
  };
}

function beginMarkerRoute(
  runtime: AgentRuntime | PackageRuntime,
  locationId: LocationId,
  destination: WorldPoint,
  routeKey: string,
  instant: boolean
) {
  const current: WorldPoint = [runtime.root.position.x, runtime.root.position.z];
  const moved = Math.hypot(destination[0] - current[0], destination[1] - current[1]);
  if (moved < 0.03 && runtime.destinationLocationId === locationId) {
    runtime.destination = destination;
    return;
  }
  if (instant) {
    runtime.route = [destination];
    runtime.routeLength = 0;
    runtime.routeTravelled = 0;
    runtime.root.position.x = destination[0];
    runtime.root.position.z = destination[1];
  } else {
    const semanticRoute = planSemanticRoute(
      runtime.destinationLocationId,
      locationId,
      routeKey
    );
    const middle = semanticRoute.slice(1, -1);
    runtime.route = [current, ...middle, destination];
    runtime.routeLength = routeLength(runtime.route);
    runtime.routeTravelled = 0;
  }
  runtime.destinationLocationId = locationId;
  runtime.destination = destination;
}

const TERMINAL_WORLD_TASKS = new Set(["completed", "failed", "cancelled"]);

function syncAgentMarkers(
  scene: THREE.Scene,
  palette: MaterialPalette,
  runtimes: Map<AgentId, AgentRuntime>,
  agentPickable: THREE.Mesh[],
  worldAgents: readonly WorldAgent[],
  worldTasks: readonly WorldTask[],
  selectedAgentId: AgentId | null,
  instant: boolean
) {
  runtimes.forEach((runtime) => {
    runtime.root.visible = false;
  });
  const taskById = new Map(worldTasks.map((task) => [task.id, task]));
  const visible = worldAgents
    .filter((agent) => desiredAgentLocation(agent) !== null)
    .sort((left, right) => left.id.localeCompare(right.id));
  const byLocation = new Map<string, WorldAgent[]>();
  visible.forEach((agent) => {
    const locationId = desiredAgentLocation(agent)!;
    const group = byLocation.get(locationId) ?? [];
    group.push(agent);
    byLocation.set(locationId, group);
  });

  visible.forEach((agent, globalIndex) => {
    const locationId = desiredAgentLocation(agent)!;
    const siblings = byLocation.get(locationId) ?? [agent];
    const siblingIndex = siblings.findIndex((candidate) => candidate.id === agent.id);
    const node = getSceneLocationNode(locationId).position;
    const offset = markerStackOffset(siblingIndex, siblings.length);
    const destination: WorldPoint = [node[0] + offset[0], node[1] + offset[1]];
    let runtime = runtimes.get(agent.id);
    if (!runtime) {
      runtime = createAgentMarker(
        scene,
        palette,
        agent,
        locationId,
        destination,
        globalIndex,
        agentPickable
      );
      runtimes.set(agent.id, runtime);
    } else {
      const rewound = agent.lastEvent.sequence < runtime.lastSequence;
      beginMarkerRoute(runtime, locationId, destination, agent.id, instant || rewound);
      runtime.lastSequence = agent.lastEvent.sequence;
    }
    runtime.root.visible = true;
    const color = agentStatusColor(agent.status);
    runtime.coreMaterial.color.setHex(color);
    runtime.coreMaterial.emissive.setHex(color);
    runtime.halo.material.color.setHex(selectedAgentId === agent.id ? 0xffffff : color);
    const task = agent.taskId ? taskById.get(agent.taskId) : undefined;
    runtime.workPackage.visible = Boolean(
      task && !TERMINAL_WORLD_TASKS.has(task.status)
    );
    runtime.workPackage.material.emissive.setHex(
      task?.status === "blocked" ? 0xff5f6d : task?.status === "awaiting_approval" ? 0xffb84c : 0x7b43c9
    );
  });
}

function syncPackageMarkers(
  scene: THREE.Scene,
  runtimes: Map<PackageId, PackageRuntime>,
  worldPackages: readonly WorldPackage[],
  instant: boolean
) {
  runtimes.forEach((runtime) => {
    runtime.root.visible = false;
  });
  const visible = worldPackages
    .filter((worldPackage): worldPackage is WorldPackage & { locationId: LocationId } =>
      worldPackage.locationId !== null
    )
    .sort((left, right) => left.id.localeCompare(right.id));
  visible.forEach((worldPackage, index) => {
    const node = getSceneLocationNode(worldPackage.locationId).position;
    const offset = markerStackOffset(index + 3, visible.length + 3);
    const destination: WorldPoint = [node[0] + offset[0] * 1.25, node[1] + offset[1] * 1.25];
    let runtime = runtimes.get(worldPackage.id);
    if (!runtime) {
      runtime = createPackageMarker(scene, worldPackage, worldPackage.locationId, destination, index);
      runtimes.set(worldPackage.id, runtime);
    } else {
      const sequence = worldPackage.lastEvent?.sequence ?? 0;
      const rewound = sequence < runtime.lastSequence;
      beginMarkerRoute(
        runtime,
        worldPackage.locationId,
        destination,
        worldPackage.id,
        instant || rewound
      );
      runtime.lastSequence = sequence;
    }
    runtime.root.visible = true;
    const color = packageStateColor(worldPackage.state);
    runtime.coreMaterial.emissive.setHex(color);
    runtime.halo.material.color.setHex(color);
  });
}

function createLights(scene: THREE.Scene, qualityProfile: WorldQualityProfile) {
  scene.add(new THREE.HemisphereLight(0xf4fbff, 0x24475a, 1.62));
  scene.add(new THREE.AmbientLight(0xb8d7df, 0.5));

  const key = new THREE.DirectionalLight(0xfffbed, 4.4);
  key.position.set(-95, 165, 95);
  key.castShadow = qualityProfile.shadows !== "off";
  const shadowMapSize = qualityProfile.shadowMapSize || 1024;
  key.shadow.mapSize.set(shadowMapSize, shadowMapSize);
  key.shadow.camera.left = -240;
  key.shadow.camera.right = 240;
  key.shadow.camera.top = 205;
  key.shadow.camera.bottom = -205;
  key.shadow.camera.near = 15;
  key.shadow.camera.far = 520;
  key.shadow.bias = -0.0002;
  key.shadow.normalBias = 0.028;
  scene.add(key);

  const rim = new THREE.DirectionalLight(0x38c9ed, 1.28);
  rim.position.set(105, 70, -95);
  scene.add(rim);

  const centralFill = new THREE.PointLight(0x2adfff, 27, 95, 2);
  centralFill.position.set(-3, 38, -8);
  scene.add(centralFill);

  const eastMobility = new THREE.PointLight(0xff7028, 24, 62, 2);
  eastMobility.position.set(58, 14, -20);
  scene.add(eastMobility);

  const northTech = new THREE.PointLight(0xa55bff, 27, 64, 2);
  northTech.position.set(-50, 15, -43);
  scene.add(northTech);

  const parkLight = new THREE.PointLight(0x3dff7e, 21, 36, 2);
  parkLight.position.set(PARK[0], 7, PARK[1]);
  scene.add(parkLight);
}

function setSelected(
  runtimes: Map<BuildingId, BuildingRuntime>,
  selected: BuildingId | null
) {
  runtimes.forEach((runtime, id) => {
    const active = id === selected;
    runtime.selection.opacity = active ? 0.17 : 0;
    runtime.accent.color.setHex(active ? 0xff7a28 : runtime.ambientAccent);
    runtime.accent.emissive.setHex(active ? 0xff4d00 : runtime.ambientAccent);
    runtime.accent.emissiveIntensity = active ? 1.5 : 0.46;
  });
}

function disposeScene(scene: THREE.Scene) {
  const geometries = new Set<THREE.BufferGeometry>();
  const materials = new Set<THREE.Material>();
  const textures = new Set<THREE.Texture>();
  scene.traverse((object) => {
    const disposable = object as THREE.Object3D & {
      geometry?: THREE.BufferGeometry;
      material?: THREE.Material | THREE.Material[];
    };
    if (disposable.geometry) geometries.add(disposable.geometry);
    const objectMaterials = Array.isArray(disposable.material)
      ? disposable.material
      : disposable.material
        ? [disposable.material]
        : [];
    for (const material of objectMaterials) {
      materials.add(material);
      const withMaps = material as THREE.Material & {
        map?: THREE.Texture | null;
        normalMap?: THREE.Texture | null;
        roughnessMap?: THREE.Texture | null;
        metalnessMap?: THREE.Texture | null;
      };
      for (const texture of [
        withMaps.map,
        withMaps.normalMap,
        withMaps.roughnessMap,
        withMaps.metalnessMap
      ]) {
        if (texture) textures.add(texture);
      }
    }
  });
  textures.forEach((texture) => texture.dispose());
  geometries.forEach((geometry) => geometry.dispose());
  materials.forEach((material) => material.dispose());
}

function resetFirstPersonCamera(camera: THREE.PerspectiveCamera): FirstPersonLook {
  camera.position.set(...FIRST_PERSON_START);
  camera.rotation.order = "YXZ";
  const deltaX = FIRST_PERSON_TARGET[0] - FIRST_PERSON_START[0];
  const deltaY = FIRST_PERSON_TARGET[1] - FIRST_PERSON_START[1];
  const deltaZ = FIRST_PERSON_TARGET[2] - FIRST_PERSON_START[2];
  const horizontalDistance = Math.hypot(deltaX, deltaZ);
  const look = {
    yaw: Math.atan2(-deltaX, -deltaZ),
    pitch: Math.atan2(deltaY, horizontalDistance)
  };
  camera.rotation.set(look.pitch, look.yaw, 0, "YXZ");
  camera.updateProjectionMatrix();
  return look;
}

function createCameraFlight(
  camera: THREE.PerspectiveCamera,
  controls: OrbitControls,
  focus: SceneLocationNode,
  duration: number
): CameraFlight {
  const fromPosition = camera.position.clone();
  const fromTarget = controls.target.clone();
  const toTarget = new THREE.Vector3(
    focus.position[0],
    focus.targetY ?? 4.2,
    focus.position[1]
  );
  const viewDirection = fromPosition.clone().sub(fromTarget);
  if (viewDirection.lengthSq() < 0.001) {
    viewDirection
      .set(...OVERVIEW_POSITION)
      .sub(new THREE.Vector3(...OVERVIEW_TARGET));
  }
  const focusDistance = THREE.MathUtils.clamp(viewDirection.length() * 0.58, 146, 188);
  const toPosition = toTarget
    .clone()
    .add(viewDirection.normalize().multiplyScalar(focusDistance));

  return {
    startedAt: performance.now(),
    duration,
    fromPosition,
    toPosition,
    fromTarget,
    toTarget
  };
}

function isEditableTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT"
  );
}

const locationBuilding = new Map(
  CITY_LOCATIONS.map((location) => [location.id, location.focusBuildingId] as const)
);

const occupancyFor = (id: BuildingId, worldAgents: readonly WorldAgent[]) =>
  worldAgents.filter((agent) => {
    const locationId = desiredAgentLocation(agent);
    return locationId ? locationBuilding.get(locationId) === id : false;
  }).length;

export function ThreeCity({
  selectedBuilding,
  selectBuilding,
  paused,
  reducedMotion,
  speed,
  zoom,
  resetKey,
  viewMode,
  activeLayer = "overview",
  qualityProfile = WORLD_QUALITY_PROFILES.high,
  selectionRequestKey = 0,
  focusLocationId = null,
  focusRequestKey = 0,
  worldAgents = [],
  worldTasks = [],
  worldPackages = [],
  selectedAgentId = null,
  onSelectAgent,
  onLabelPositionsChange
}: ThreeCityProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const firstPersonCameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const publishLabelsRef = useRef<() => void>(() => undefined);
  const runtimesRef = useRef<Map<BuildingId, BuildingRuntime>>(new Map());
  const layerSceneRef = useRef<LayerSceneRuntime | null>(null);
  const cameraFlightRef = useRef<CameraFlight | null>(null);
  const overviewCameraSnapshotRef = useRef<OverviewCameraSnapshot | null>(null);
  const selectedRef = useRef<BuildingId | null>(selectedBuilding);
  const selectRef = useRef(selectBuilding);
  const animationRef = useRef({ paused, reducedMotion, speed });
  const viewModeRef = useRef(viewMode);
  const activeLayerRef = useRef(activeLayer);
  const worldAgentsRef = useRef(worldAgents);
  const worldTasksRef = useRef(worldTasks);
  const worldPackagesRef = useRef(worldPackages);
  const selectedAgentRef = useRef<AgentId | null>(selectedAgentId);
  const selectAgentRef = useRef(onSelectAgent);
  const labelPositionsChangeRef = useRef(onLabelPositionsChange);
  const pressedKeysRef = useRef(new Set<string>());
  const firstPersonLookRef = useRef<FirstPersonLook>({ yaw: 0, pitch: 0 });
  const [renderError, setRenderError] = useState<string | null>(null);
  const [pointerLocked, setPointerLocked] = useState(false);

  selectedRef.current = selectedBuilding;
  selectRef.current = selectBuilding;
  animationRef.current = { paused, reducedMotion, speed };
  viewModeRef.current = viewMode;
  activeLayerRef.current = activeLayer;
  worldAgentsRef.current = worldAgents;
  worldTasksRef.current = worldTasks;
  worldPackagesRef.current = worldPackages;
  selectedAgentRef.current = selectedAgentId;
  selectAgentRef.current = onSelectAgent;
  labelPositionsChangeRef.current = onLabelPositionsChange;

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;
    const pressedKeys = pressedKeysRef.current;

    let disposed = false;
    let frame = 0;
    let resizeObserver: ResizeObserver | null = null;
    let renderer: THREE.WebGLRenderer | null = null;
    let controls: OrbitControls | null = null;
    let scene: THREE.Scene | null = null;
    let detachInputListeners: (() => void) | null = null;
    let detachLabelListener: (() => void) | null = null;
    let pointerStart: Point2 | null = null;
    const pickable: THREE.Mesh[] = [];
    const agentPickable: THREE.Mesh[] = [];
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();

    try {
      setRenderError(null);
      scene = new THREE.Scene();
      scene.background = new THREE.Color(0x4d8398);
      scene.fog = new THREE.Fog(0x4d8398, 350, 760);
      const automatedRuntime = navigator.webdriver;

      renderer = new THREE.WebGLRenderer({
        antialias: !automatedRuntime && qualityProfile.mode !== "performance",
        alpha: false,
        powerPreference: qualityProfile.mode === "performance" ? "low-power" : "high-performance"
      });
      renderer.setPixelRatio(
        automatedRuntime
          ? 0.65
          : Math.min(window.devicePixelRatio || 1, qualityProfile.pixelRatioCap)
      );
      renderer.shadowMap.enabled = !automatedRuntime && qualityProfile.shadows !== "off";
      renderer.shadowMap.type = THREE.PCFShadowMap;
      renderer.shadowMap.autoUpdate = qualityProfile.shadows === "dynamic";
      renderer.shadowMap.needsUpdate = true;
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = qualityProfile.postprocessing === "off"
        ? THREE.NoToneMapping
        : THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 1.03;
      renderer.domElement.className = "three-city-canvas pixi-city-canvas";
      renderer.domElement.setAttribute("aria-hidden", "true");
      renderer.domElement.tabIndex = -1;
      renderer.domElement.style.position = "absolute";
      renderer.domElement.style.inset = "0";
      renderer.domElement.style.width = "100%";
      renderer.domElement.style.height = "100%";
      renderer.domElement.style.display = "block";
      renderer.domElement.style.touchAction = "none";
      mount.appendChild(renderer.domElement);
      rendererRef.current = renderer;

      const camera = new THREE.PerspectiveCamera(OVERVIEW_FOV, 1, 0.1, 1200);
      camera.position.set(...OVERVIEW_POSITION);
      camera.zoom = 1;
      camera.lookAt(...OVERVIEW_TARGET);
      cameraRef.current = camera;

      const firstPersonCamera = new THREE.PerspectiveCamera(58, 1, 0.08, 800);
      firstPersonLookRef.current = resetFirstPersonCamera(firstPersonCamera);
      firstPersonCameraRef.current = firstPersonCamera;

      controls = new OrbitControls(camera, renderer.domElement);
      controls.target.set(...OVERVIEW_TARGET);
      controls.enableDamping = !animationRef.current.reducedMotion;
      controls.dampingFactor = 0.075;
      controls.enablePan = true;
      controls.enableRotate = true;
      controls.enableZoom = true;
      controls.panSpeed = 0.55;
      controls.rotateSpeed = 0.62;
      controls.zoomSpeed = 0.86;
      controls.screenSpacePanning = false;
      controls.minPolarAngle = THREE.MathUtils.degToRad(34);
      controls.maxPolarAngle = THREE.MathUtils.degToRad(69);
      controls.minAzimuthAngle = -Infinity;
      controls.maxAzimuthAngle = Infinity;
      controls.minDistance = 132;
      controls.maxDistance = 520;
      controls.enabled = viewModeRef.current === "overview";
      controls.update();
      controls.saveState();
      const savedCamera = overviewCameraSnapshotRef.current;
      if (savedCamera) {
        camera.position.copy(savedCamera.position);
        camera.zoom = savedCamera.zoom;
        camera.updateProjectionMatrix();
        controls.target.copy(savedCamera.target);
        controls.update();
        overviewCameraSnapshotRef.current = null;
      }
      controlsRef.current = controls;

      let lastLabelPublish = -Infinity;
      const publishLabelPositions = (force = false) => {
        const publish = labelPositionsChangeRef.current;
        if (!publish || viewModeRef.current !== "overview") return;
        const now = performance.now();
        if (!force && now - lastLabelPublish < 48) return;
        lastLabelPublish = now;
        camera.updateMatrixWorld(true);
        const baselineCamera = camera.clone();
        baselineCamera.position.set(...OVERVIEW_POSITION);
        baselineCamera.zoom = 1;
        baselineCamera.lookAt(...OVERVIEW_TARGET);
        baselineCamera.updateProjectionMatrix();
        baselineCamera.updateMatrixWorld(true);
        const positions: CityLabelPositions = {};
        CITY_LABEL_ANCHORS.forEach(({ key, position }) => {
          const current = new THREE.Vector3(...position).project(camera);
          const baseline = new THREE.Vector3(...position).project(baselineCamera);
          positions[key] = {
            deltaX: (current.x - baseline.x) * 50,
            deltaY: (baseline.y - current.y) * 50,
            visible:
              current.z >= -1 &&
              current.z <= 1 &&
              current.x >= -1.12 &&
              current.x <= 1.12 &&
              current.y >= -1.08 &&
              current.y <= 1.08
          };
        });
        publish(positions);
      };
      publishLabelsRef.current = () => publishLabelPositions(true);
      const publishLabelsOnControlsChange = () => publishLabelPositions();
      const cancelCameraFlightOnInteraction = () => {
        cameraFlightRef.current = null;
      };
      controls.addEventListener("change", publishLabelsOnControlsChange);
      controls.addEventListener("start", cancelCameraFlightOnInteraction);
      detachLabelListener = () => {
        controls?.removeEventListener("change", publishLabelsOnControlsChange);
        controls?.removeEventListener("start", cancelCameraFlightOnInteraction);
      };
      renderer.domElement.style.cursor =
        viewModeRef.current === "first-person" ? "crosshair" : "grab";

      const palette = createPalette();
      createWorldGround(scene, palette);
      createMetroFabric(scene, palette);
      createDistrictPads(scene, palette);
      createRoadNetwork(scene, palette);
      createProceduralBuildings(scene, palette);
      createCentralLandmark(scene, palette);
      createRoundabout(scene, palette);
      createIndustrialUtilities(scene, palette);
      createLogisticsDetails(scene, palette);
      createExpandedInfrastructure(scene, palette);
      createDigitalPark(scene, palette);
      createUrbanDetails(scene, palette);

      const runtimes = new Map<BuildingId, BuildingRuntime>();
      for (const spec of ANCHOR_SPECS) {
        runtimes.set(spec.id, createAnchorBuilding(scene, spec, palette, pickable));
      }
      runtimesRef.current = runtimes;
      setSelected(runtimes, selectedRef.current);
      const agentMarkers = new Map<AgentId, AgentRuntime>();
      const packageMarkers = new Map<PackageId, PackageRuntime>();
      const vehicleLife = createVehicleLife(
        scene,
        palette,
        qualityProfile.ambientLifeDensity
      );
      const pedestrianLife = createPedestrianLife(
        scene,
        qualityProfile.ambientLifeDensity
      );
      const waterPulses = createWaterPulses(scene, palette);
      const routePulses = createRoutePulses(scene, palette);
      const rooftopSignals = createRooftopSignals(scene);
      const layerOverlays = createLayerOverlays(scene);
      const layerSceneRuntime: LayerSceneRuntime = {
        scene,
        overlays: layerOverlays,
        vehicleLife,
        pedestrianLife,
        waterPulses,
        routePulses,
        rooftopSignals
      };
      layerSceneRef.current = layerSceneRuntime;
      applyCityLayer(layerSceneRuntime, activeLayerRef.current);
      createLights(scene, qualityProfile);

      const updatePointer = (event: PointerEvent) => {
        if (!renderer) return;
        const bounds = renderer.domElement.getBoundingClientRect();
        pointer.x = ((event.clientX - bounds.left) / Math.max(1, bounds.width)) * 2 - 1;
        pointer.y = -((event.clientY - bounds.top) / Math.max(1, bounds.height)) * 2 + 1;
      };
      const pickTarget = (event: PointerEvent):
        | { readonly agentId: AgentId }
        | { readonly buildingId: BuildingId }
        | undefined => {
        if (viewModeRef.current !== "overview") return undefined;
        updatePointer(event);
        raycaster.setFromCamera(pointer, camera);
        const agentHit = raycaster.intersectObjects(agentPickable, false).find((hit) => {
          const agentId = hit.object.userData.agentId as AgentId | undefined;
          return agentId ? agentMarkers.get(agentId)?.root.visible : false;
        });
        const agentId = agentHit?.object.userData.agentId as AgentId | undefined;
        if (agentId) return { agentId };
        const hit = raycaster.intersectObjects(pickable, false)[0];
        const buildingId = hit?.object.userData.buildingId as BuildingId | undefined;
        return buildingId ? { buildingId } : undefined;
      };
      const onPointerDown = (event: PointerEvent) => {
        if (viewModeRef.current === "first-person") {
          pointerStart = null;
          if (
            event.button === 0 &&
            renderer &&
            document.pointerLockElement !== renderer.domElement
          ) {
            void renderer.domElement.requestPointerLock().catch(() => {
              if (!disposed) setPointerLocked(false);
            });
          }
          return;
        }
        if (event.button !== 0) return;
        pointerStart = [event.clientX, event.clientY];
        renderer!.domElement.style.cursor = "grabbing";
      };
      const onPointerMove = (event: PointerEvent) => {
        if (viewModeRef.current === "first-person") return;
        if (pointerStart) {
          renderer!.domElement.style.cursor = "grabbing";
          return;
        }
        const target = pickTarget(event);
        renderer!.domElement.style.cursor = target
          ? "pointer"
          : "grab";
      };
      const onPointerUp = (event: PointerEvent) => {
        if (viewModeRef.current === "first-person") {
          pointerStart = null;
          return;
        }
        if (!pointerStart || event.button !== 0) {
          pointerStart = null;
          renderer!.domElement.style.cursor = "grab";
          return;
        }
        const target = pickTarget(event);
        renderer!.domElement.style.cursor = target ? "pointer" : "grab";
        const movement = Math.hypot(
          event.clientX - pointerStart[0],
          event.clientY - pointerStart[1]
        );
        pointerStart = null;
        if (movement > 7) return;
        if (target && "agentId" in target) selectAgentRef.current?.(target.agentId);
        else if (target && "buildingId" in target) selectRef.current(target.buildingId);
      };
      const onPointerLeave = () => {
        pointerStart = null;
        if (renderer) {
          renderer.domElement.style.cursor =
            viewModeRef.current === "first-person" ? "crosshair" : "grab";
        }
      };
      const onContextLost = (event: Event) => {
        event.preventDefault();
        if (!disposed) {
          setRenderError("The WebGL context was lost. Reload the page to restore the 3D city.");
        }
      };
      const onPointerLockChange = () => {
        const locked = document.pointerLockElement === renderer?.domElement;
        setPointerLocked(locked);
        if (renderer) {
          renderer.domElement.style.cursor =
            viewModeRef.current === "first-person"
              ? locked
                ? "none"
                : "crosshair"
              : "grab";
        }
      };
      const onMouseLook = (event: MouseEvent) => {
        if (
          viewModeRef.current !== "first-person" ||
          document.pointerLockElement !== renderer?.domElement
        ) {
          return;
        }
        const look = firstPersonLookRef.current;
        look.yaw -= event.movementX * 0.0019;
        look.pitch = THREE.MathUtils.clamp(
          look.pitch - event.movementY * 0.0019,
          THREE.MathUtils.degToRad(-78),
          THREE.MathUtils.degToRad(78)
        );
        firstPersonCamera.rotation.set(look.pitch, look.yaw, 0, "YXZ");
      };
      const onKeyDown = (event: KeyboardEvent) => {
        if (
          viewModeRef.current !== "first-person" ||
          isEditableTarget(event.target) ||
          !MOVEMENT_KEYS.has(event.code)
        ) {
          return;
        }
        pressedKeys.add(event.code);
        event.preventDefault();
      };
      const onKeyUp = (event: KeyboardEvent) => {
        pressedKeys.delete(event.code);
        if (
          viewModeRef.current === "first-person" &&
          MOVEMENT_KEYS.has(event.code) &&
          !isEditableTarget(event.target)
        ) {
          event.preventDefault();
        }
      };
      const onWindowBlur = () => {
        pressedKeys.clear();
      };
      renderer.domElement.addEventListener("pointerdown", onPointerDown);
      renderer.domElement.addEventListener("pointermove", onPointerMove);
      renderer.domElement.addEventListener("pointerup", onPointerUp);
      renderer.domElement.addEventListener("pointerleave", onPointerLeave);
      renderer.domElement.addEventListener("webglcontextlost", onContextLost);
      document.addEventListener("pointerlockchange", onPointerLockChange);
      document.addEventListener("mousemove", onMouseLook);
      window.addEventListener("keydown", onKeyDown);
      window.addEventListener("keyup", onKeyUp);
      window.addEventListener("blur", onWindowBlur);
      detachInputListeners = () => {
        renderer?.domElement.removeEventListener("pointerdown", onPointerDown);
        renderer?.domElement.removeEventListener("pointermove", onPointerMove);
        renderer?.domElement.removeEventListener("pointerup", onPointerUp);
        renderer?.domElement.removeEventListener("pointerleave", onPointerLeave);
        renderer?.domElement.removeEventListener("webglcontextlost", onContextLost);
        document.removeEventListener("pointerlockchange", onPointerLockChange);
        document.removeEventListener("mousemove", onMouseLook);
        window.removeEventListener("keydown", onKeyDown);
        window.removeEventListener("keyup", onKeyUp);
        window.removeEventListener("blur", onWindowBlur);
      };

      const resize = () => {
        if (!renderer) return;
        const bounds = mount.getBoundingClientRect();
        const width = Math.max(1, Math.round(bounds.width));
        const height = Math.max(1, Math.round(bounds.height));
        const aspect = width / height;
        camera.aspect = aspect;
        camera.fov = OVERVIEW_FOV + THREE.MathUtils.clamp((aspect - 1.34) * 10.5, 0, 6);
        camera.updateProjectionMatrix();
        firstPersonCamera.aspect = aspect;
        firstPersonCamera.updateProjectionMatrix();
        renderer.setSize(width, height, false);
        publishLabelPositions();
      };
      resize();
      resizeObserver = new ResizeObserver(resize);
      resizeObserver.observe(mount);

      const timer = new THREE.Timer();
      timer.connect(document);
      let animationElapsed = 0;
      let lastFrameTimestamp: number | null = null;
      const targetFrameRate = automatedRuntime ? 8 : qualityProfile.targetFps;
      const targetFrameInterval = 1000 / targetFrameRate;
      const firstPersonForward = new THREE.Vector3();
      const firstPersonRight = new THREE.Vector3();
      const firstPersonMovement = new THREE.Vector3();
      let workflowAgentSignature = "";
      let workflowPackageSignature = "";
      const render = (timestamp?: number) => {
        if (disposed || !renderer || !scene || !controls) return;
        frame = window.requestAnimationFrame(render);
        const frameTimestamp = timestamp ?? performance.now();
        if (lastFrameTimestamp !== null) {
          const elapsedSinceFrame = frameTimestamp - lastFrameTimestamp;
          if (elapsedSinceFrame + 0.05 < targetFrameInterval) return;
          lastFrameTimestamp = frameTimestamp - (elapsedSinceFrame % targetFrameInterval);
        } else {
          lastFrameTimestamp = frameTimestamp;
        }
        timer.update(timestamp);
        const delta = Math.min(timer.getDelta(), 0.05);
        const state = animationRef.current;
        const nextAgentSignature = [
          selectedAgentRef.current ?? "",
          ...worldAgentsRef.current.map((agent) =>
            [
              agent.id,
              agent.lastEvent.sequence,
              agent.locationId ?? "",
              agent.targetLocationId ?? "",
              agent.status,
              agent.taskId ?? ""
            ].join(":")
          ),
          ...worldTasksRef.current.map((task) =>
            `${task.id}:${task.lastEvent.sequence}:${task.status}`
          )
        ].join("|");
        if (nextAgentSignature !== workflowAgentSignature) {
          syncAgentMarkers(
            scene,
            palette,
            agentMarkers,
            agentPickable,
            worldAgentsRef.current,
            worldTasksRef.current,
            selectedAgentRef.current,
            state.reducedMotion || state.paused
          );
          workflowAgentSignature = nextAgentSignature;
        }
        const nextPackageSignature = worldPackagesRef.current
          .map((worldPackage) =>
            [
              worldPackage.id,
              worldPackage.lastEvent?.sequence ?? 0,
              worldPackage.locationId ?? "",
              worldPackage.state,
              worldPackage.artifactCount
            ].join(":")
          )
          .join("|");
        if (nextPackageSignature !== workflowPackageSignature) {
          syncPackageMarkers(
            scene,
            packageMarkers,
            worldPackagesRef.current,
            state.reducedMotion || state.paused
          );
          workflowPackageSignature = nextPackageSignature;
        }
        const firstPersonActive = viewModeRef.current === "first-person";
        controls.enabled = !firstPersonActive;
        controls.enableDamping = !state.reducedMotion;
        if (!firstPersonActive) {
          const cameraFlight = cameraFlightRef.current;
          if (cameraFlight) {
            const progress = THREE.MathUtils.clamp(
              (frameTimestamp - cameraFlight.startedAt) / cameraFlight.duration,
              0,
              1
            );
            const easedProgress = 1 - Math.pow(1 - progress, 3);
            camera.position.lerpVectors(
              cameraFlight.fromPosition,
              cameraFlight.toPosition,
              easedProgress
            );
            controls.target.lerpVectors(
              cameraFlight.fromTarget,
              cameraFlight.toTarget,
              easedProgress
            );
            camera.lookAt(controls.target);
            publishLabelPositions();
            if (progress >= 1) cameraFlightRef.current = null;
          }
          controls.update();
        } else {
          const keys = pressedKeys;
          const forwardAxis =
            (keys.has("KeyW") || keys.has("ArrowUp") ? 1 : 0) -
            (keys.has("KeyS") || keys.has("ArrowDown") ? 1 : 0);
          const rightAxis =
            (keys.has("KeyD") || keys.has("ArrowRight") ? 1 : 0) -
            (keys.has("KeyA") || keys.has("ArrowLeft") ? 1 : 0);
          if (forwardAxis !== 0 || rightAxis !== 0) {
            const yaw = firstPersonLookRef.current.yaw;
            firstPersonForward.set(-Math.sin(yaw), 0, -Math.cos(yaw));
            firstPersonRight.set(Math.cos(yaw), 0, -Math.sin(yaw));
            firstPersonMovement
              .copy(firstPersonForward)
              .multiplyScalar(forwardAxis)
              .addScaledVector(firstPersonRight, rightAxis)
              .normalize();
            const fast = keys.has("ShiftLeft") || keys.has("ShiftRight");
            firstPersonCamera.position.addScaledVector(
              firstPersonMovement,
              delta * (fast ? 17 : 7.5)
            );
            firstPersonCamera.position.x = THREE.MathUtils.clamp(
              firstPersonCamera.position.x,
              FIRST_PERSON_BOUNDS.minX,
              FIRST_PERSON_BOUNDS.maxX
            );
            firstPersonCamera.position.z = THREE.MathUtils.clamp(
              firstPersonCamera.position.z,
              FIRST_PERSON_BOUNDS.minZ,
              FIRST_PERSON_BOUNDS.maxZ
            );
            firstPersonCamera.position.y = FIRST_PERSON_START[1];
          }
        }
        const lifeDelta = delta * Math.max(0.1, state.speed);
        if (!state.paused && !state.reducedMotion) {
          animationElapsed += lifeDelta;
          updateVehicleLife(vehicleLife, lifeDelta);
          updatePedestrianLife(pedestrianLife, lifeDelta, animationElapsed);
          updatePulseRuntime(waterPulses, lifeDelta);
          updatePulseRuntime(routePulses, lifeDelta);
          updateRooftopSignals(rooftopSignals, animationElapsed);
          palette.glassCyan.emissiveIntensity =
            0.64 + Math.sin(animationElapsed * 0.72) * 0.055;
          palette.glassOrange.emissiveIntensity =
            0.56 + Math.sin(animationElapsed * 0.61 + 1.7) * 0.05;
          palette.glassPurple.emissiveIntensity =
            0.6 + Math.sin(animationElapsed * 0.67 + 3.1) * 0.05;
        } else {
          palette.glassCyan.emissiveIntensity = 0.64;
          palette.glassOrange.emissiveIntensity = 0.56;
          palette.glassPurple.emissiveIntensity = 0.6;
        }
        agentMarkers.forEach((marker) => {
          if (!marker.root.visible) return;
          if (state.reducedMotion && marker.routeTravelled < marker.routeLength) {
            marker.routeTravelled = marker.routeLength;
          } else if (!state.paused && marker.routeTravelled < marker.routeLength) {
            marker.routeTravelled = Math.min(
              marker.routeLength,
              marker.routeTravelled + lifeDelta * 32
            );
          }
          if (marker.routeLength > 0) {
            const point = pointAlongRoute(
              marker.route,
              marker.routeTravelled / marker.routeLength
            );
            marker.root.position.x = point[0];
            marker.root.position.z = point[1];
          }
          const wave = state.paused || state.reducedMotion
            ? 0
            : Math.sin(animationElapsed * 2.1 + marker.phase);
          marker.root.position.y = marker.baseY + wave * 0.075;
          const selected = selectedAgentRef.current === marker.id;
          const scale = selected ? 1.5 : 1 + (wave + 1) * 0.14;
          marker.halo.scale.setScalar(scale);
          marker.halo.material.opacity = selected ? 0.92 : 0.35 + (wave + 1) * 0.2;
          marker.workPackage.rotation.y += state.paused || state.reducedMotion ? 0 : lifeDelta * 1.8;
        });
        packageMarkers.forEach((marker) => {
          if (!marker.root.visible) return;
          if (state.reducedMotion && marker.routeTravelled < marker.routeLength) {
            marker.routeTravelled = marker.routeLength;
          } else if (!state.paused && marker.routeTravelled < marker.routeLength) {
            marker.routeTravelled = Math.min(
              marker.routeLength,
              marker.routeTravelled + lifeDelta * 25
            );
          }
          if (marker.routeLength > 0) {
            const point = pointAlongRoute(
              marker.route,
              marker.routeTravelled / marker.routeLength
            );
            marker.root.position.x = point[0];
            marker.root.position.z = point[1];
          }
          const wave = state.paused || state.reducedMotion
            ? 0
            : Math.sin(animationElapsed * 1.55 + marker.phase);
          marker.root.position.y = marker.baseY + wave * 0.055;
          marker.halo.scale.setScalar(1 + (wave + 1) * 0.1);
          marker.halo.material.opacity = 0.34 + (wave + 1) * 0.12;
          marker.root.rotation.y += state.paused || state.reducedMotion ? 0 : lifeDelta * 0.24;
        });
        renderer.render(scene, firstPersonActive ? firstPersonCamera : camera);
      };
      render();

      return () => {
        disposed = true;
        window.cancelAnimationFrame(frame);
        timer.dispose();
        resizeObserver?.disconnect();
        detachLabelListener?.();
        detachInputListeners?.();
        pressedKeys.clear();
        if (document.pointerLockElement === renderer?.domElement) {
          document.exitPointerLock();
        }
        if (controls && viewModeRef.current === "overview") {
          overviewCameraSnapshotRef.current = {
            position: camera.position.clone(),
            target: controls.target.clone(),
            zoom: camera.zoom
          };
        }
        controls?.dispose();
        controlsRef.current = null;
        cameraFlightRef.current = null;
        layerSceneRef.current = null;
        publishLabelsRef.current = () => undefined;
        cameraRef.current = null;
        firstPersonCameraRef.current = null;
        rendererRef.current = null;
        runtimesRef.current = new Map();
        if (scene) disposeScene(scene);
        renderer?.renderLists.dispose();
        renderer?.dispose();
        renderer?.forceContextLoss();
        renderer?.domElement.remove();
      };
    } catch (error: unknown) {
      disposed = true;
      detachInputListeners?.();
      detachLabelListener?.();
      pressedKeys.clear();
      if (document.pointerLockElement === renderer?.domElement) {
        document.exitPointerLock();
      }
      controls?.dispose();
      controlsRef.current = null;
      cameraFlightRef.current = null;
      layerSceneRef.current = null;
      publishLabelsRef.current = () => undefined;
      cameraRef.current = null;
      firstPersonCameraRef.current = null;
      rendererRef.current = null;
      if (scene) disposeScene(scene);
      renderer?.dispose();
      renderer?.domElement.remove();
      setPointerLocked(false);
      setRenderError(
        error instanceof Error
          ? error.message
          : "This device could not initialize the WebGL city renderer."
      );
    }
  }, [qualityProfile]);

  useEffect(() => {
    setSelected(runtimesRef.current, selectedBuilding);
    cameraFlightRef.current = null;
    if (!selectedBuilding || viewModeRef.current !== "overview") return;

    const camera = cameraRef.current;
    const controls = controlsRef.current;
    const anchor = ANCHOR_SPECS.find(({ id }) => id === selectedBuilding);
    if (!camera || !controls || !anchor) return;

    const flight = createCameraFlight(
      camera,
      controls,
      { position: anchor.position },
      animationRef.current.reducedMotion ? 1 : 720
    );
    if (animationRef.current.reducedMotion) {
      camera.position.copy(flight.toPosition);
      controls.target.copy(flight.toTarget);
      camera.lookAt(controls.target);
      controls.update();
      publishLabelsRef.current();
      return;
    }
    cameraFlightRef.current = flight;
  }, [selectedBuilding, selectionRequestKey]);

  useEffect(() => {
    if (!focusLocationId || focusRequestKey === 0 || viewModeRef.current !== "overview") return;
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    if (!camera || !controls) return;
    const focus = getSceneLocationNode(focusLocationId);

    const flight = createCameraFlight(
      camera,
      controls,
      focus,
      animationRef.current.reducedMotion ? 1 : 720
    );
    if (animationRef.current.reducedMotion) {
      camera.position.copy(flight.toPosition);
      controls.target.copy(flight.toTarget);
      camera.lookAt(controls.target);
      controls.update();
      publishLabelsRef.current();
      return;
    }
    cameraFlightRef.current = flight;
  }, [focusLocationId, focusRequestKey]);

  useEffect(() => {
    activeLayerRef.current = activeLayer;
    const runtime = layerSceneRef.current;
    if (runtime) applyCityLayer(runtime, activeLayer);
  }, [activeLayer]);

  useEffect(() => {
    viewModeRef.current = viewMode;
    cameraFlightRef.current = null;
    pressedKeysRef.current.clear();
    const controls = controlsRef.current;
    const renderer = rendererRef.current;
    if (controls) controls.enabled = viewMode === "overview";
    if (renderer) {
      if (
        viewMode === "overview" &&
        document.pointerLockElement === renderer.domElement
      ) {
        document.exitPointerLock();
      }
      renderer.domElement.style.cursor =
        viewMode === "first-person"
          ? document.pointerLockElement === renderer.domElement
            ? "none"
            : "crosshair"
          : "grab";
    }
    if (viewMode === "overview") setPointerLocked(false);
  }, [viewMode]);

  useEffect(() => {
    const camera = cameraRef.current;
    if (!camera) return;
    const nextZoom = THREE.MathUtils.clamp(zoom, 0.72, 1.55);
    if (Math.abs(camera.zoom - nextZoom) > 0.0001) {
      cameraFlightRef.current = null;
    }
    camera.zoom = nextZoom;
    camera.updateProjectionMatrix();
    controlsRef.current?.update();
    publishLabelsRef.current();
  }, [zoom]);

  useEffect(() => {
    const camera = cameraRef.current;
    const firstPersonCamera = firstPersonCameraRef.current;
    const controls = controlsRef.current;
    cameraFlightRef.current = null;
    if (camera && controls) {
      controls.reset();
      camera.position.set(...OVERVIEW_POSITION);
      camera.zoom = 1;
      controls.target.set(...OVERVIEW_TARGET);
      camera.lookAt(controls.target);
      camera.updateProjectionMatrix();
      controls.update();
      publishLabelsRef.current();
    }
    if (firstPersonCamera) {
      firstPersonLookRef.current = resetFirstPersonCamera(firstPersonCamera);
    }
    pressedKeysRef.current.clear();
  }, [resetKey]);

  useEffect(() => {
    if (controlsRef.current) controlsRef.current.enableDamping = !reducedMotion;
  }, [reducedMotion]);

  return (
    <div className="three-city-host pixi-city-host" style={ROOT_STYLE}>
      <div ref={mountRef} style={CANVAS_MOUNT_STYLE} aria-hidden="true" />
      <div className="three-city-mode-badge" style={MODE_BADGE_STYLE}>
        {viewMode === "first-person"
          ? "OPS CITY · FIRST PERSON · WASD TO MOVE"
          : "OPS CITY · FLAT WORLD · DRAG TO ORBIT 360° · SCROLL TO ZOOM"}
      </div>

      {viewMode === "first-person" && (
        <>
          <div
            className="three-city-first-person-hud"
            style={FIRST_PERSON_HUD_STYLE}
            role="group"
            aria-label="Walk inspection controls"
          >
            <strong
              style={{ color: pointerLocked ? "#62e5ff" : "#ffad73" }}
              aria-live="polite"
            >
              {pointerLocked ? "MOUSE LOOK ACTIVE" : "CLICK CITY TO LOOK"}
            </strong>
            <span>WASD / ARROWS TO WALK · HOLD SHIFT TO MOVE FASTER</span>
            <span>{pointerLocked ? "PRESS ESC TO RELEASE CURSOR" : "ESC KEEPS WALK MODE ACTIVE"}</span>
            {!pointerLocked && (
              <button
                type="button"
                onClick={() => {
                  const canvas = rendererRef.current?.domElement;
                  if (canvas) void canvas.requestPointerLock().catch(() => setPointerLocked(false));
                }}
                style={{
                  marginTop: 4,
                  padding: "4px 9px",
                  border: "1px solid rgba(98,229,255,.7)",
                  borderRadius: 2,
                  background: "rgba(14,57,71,.92)",
                  color: "#e9fbff",
                  fontSize: 8,
                  fontWeight: 800,
                  cursor: "pointer"
                }}
              >
                Enable mouse look
              </button>
            )}
          </div>
          <div
            className="three-city-crosshair"
            style={{
              ...CROSSHAIR_STYLE,
              opacity: pointerLocked ? 0.95 : 0.45
            }}
            aria-hidden="true"
          >
            +
          </div>
        </>
      )}

      {renderError && (
        <div className="city-render-error three-city-error" style={ERROR_STYLE} role="alert">
          <strong style={{ color: "#ff7a1a", fontSize: 14 }}>3D city renderer unavailable</strong>
          <div style={{ marginTop: 6 }}>{renderError}</div>
          <div style={{ marginTop: 6, color: "#9da5a8" }}>
            The building controls remain available below so project navigation still works.
          </div>
        </div>
      )}

      <div
        className="three-city-building-controls"
        style={BUILDING_NAV_STYLE}
        role="group"
        aria-label="Engineering city buildings"
      >
        {buildings.map((building) => {
          const selected = selectedBuilding === building.id;
          const occupancy = occupancyFor(building.id, worldAgents);
          return (
            <button
              key={building.id}
              type="button"
              aria-label={`${building.name}, ${occupancy} active agent${occupancy === 1 ? "" : "s"}`}
              aria-pressed={selected}
              onClick={() => selectBuilding(building.id)}
              style={{
                flex: "0 0 auto",
                height: 24,
                padding: "0 8px",
                border: selected ? "1px solid #ff6a00" : "1px solid rgba(255,255,255,.24)",
                borderRadius: 2,
                background: selected ? "rgba(255,106,0,.2)" : "rgba(3,4,4,.84)",
                color: selected ? "#ffad73" : "#d9dddc",
                fontSize: 8,
                fontWeight: 800,
                letterSpacing: ".65px",
                cursor: "pointer"
              }}
            >
              {building.short}
              {occupancy > 0 ? ` · ${occupancy}` : ""}
            </button>
          );
        })}
      </div>
    </div>
  );
}
