import { useEffect, useRef, useState, type CSSProperties } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { agents, buildings, type BuildingId } from "../domain";

export interface ThreeCityProps {
  selectedBuilding: BuildingId | null;
  selectBuilding: (id: BuildingId) => void;
  paused: boolean;
  reducedMotion: boolean;
  speed: number;
  zoom: number;
}

type Vec3 = readonly [number, number, number];

interface BuildingSpec {
  id: BuildingId;
  code: string;
  position: readonly [number, number];
  lot: readonly [number, number];
  height: number;
}

interface MaterialPalette {
  white: THREE.MeshStandardMaterial;
  light: THREE.MeshStandardMaterial;
  concrete: THREE.MeshStandardMaterial;
  roof: THREE.MeshStandardMaterial;
  glass: THREE.MeshPhysicalMaterial;
  blackSteel: THREE.MeshStandardMaterial;
  steel: THREE.MeshStandardMaterial;
  warning: THREE.MeshStandardMaterial;
  success: THREE.MeshStandardMaterial;
}

interface BuildingRuntime {
  accent: THREE.MeshStandardMaterial;
  selection: THREE.MeshBasicMaterial;
}

interface AgentRuntime {
  root: THREE.Group;
  halo: THREE.Mesh<THREE.TorusGeometry, THREE.MeshBasicMaterial>;
  baseY: number;
  phase: number;
}

const BUILDING_SPECS: BuildingSpec[] = [
  { id: "intake", code: "01", position: [-15, -6], lot: [7.4, 5.8], height: 2.9 },
  { id: "automation", code: "02", position: [-7, -7], lot: [8.3, 6.8], height: 6.2 },
  { id: "hmi", code: "03", position: [2.4, -6.6], lot: [6.8, 6.4], height: 5.1 },
  { id: "electrical", code: "04", position: [12, -3.2], lot: [9.2, 7.2], height: 3.2 },
  { id: "qa", code: "05", position: [6.2, 5.1], lot: [8.8, 7.4], height: 4 },
  { id: "documentation", code: "06", position: [-3.2, 6.5], lot: [7.2, 6.3], height: 5.7 },
  { id: "approval", code: "07", position: [-13.2, 5], lot: [7.1, 6.1], height: 4 },
  { id: "release", code: "08", position: [14.2, 7.2], lot: [9.2, 7.5], height: 5 }
];

const ROOT_STYLE: CSSProperties = {
  position: "absolute",
  inset: 0,
  overflow: "hidden",
  background: "#020303"
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
  border: "1px solid #ff7a1a",
  background: "rgba(8,8,8,.96)",
  boxShadow: "0 18px 60px #000",
  color: "#dfe3e5",
  fontSize: 11,
  lineHeight: 1.5
};

const makeStandard = (color: number, roughness: number, metalness: number) =>
  new THREE.MeshStandardMaterial({ color, roughness, metalness });

function createPalette(): MaterialPalette {
  return {
    white: makeStandard(0xf0f1ee, 0.52, 0.08),
    light: makeStandard(0xc9cdca, 0.58, 0.12),
    concrete: makeStandard(0x747a79, 0.9, 0.02),
    roof: makeStandard(0x8e9492, 0.68, 0.18),
    glass: new THREE.MeshPhysicalMaterial({
      color: 0x071014,
      roughness: 0.14,
      metalness: 0.48,
      clearcoat: 1,
      clearcoatRoughness: 0.12,
      reflectivity: 0.82
    }),
    blackSteel: makeStandard(0x080a0a, 0.3, 0.8),
    steel: makeStandard(0x858c8b, 0.32, 0.82),
    warning: makeStandard(0xe1a637, 0.4, 0.45),
    success: makeStandard(0x51b87b, 0.42, 0.32)
  };
}

function makeAccentMaterial() {
  return new THREE.MeshStandardMaterial({
    color: 0xff7618,
    roughness: 0.38,
    metalness: 0.34,
    emissive: 0x8b2500,
    emissiveIntensity: 0.18
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

function addFrontWindows(
  parent: THREE.Object3D,
  width: number,
  facadeHeight: number,
  depth: number,
  columns: number,
  rows: number,
  material: THREE.Material,
  x = 0,
  y = 0,
  z = 0
) {
  const gapX = 0.15;
  const gapY = 0.18;
  const paneWidth = (width - gapX * (columns + 1)) / columns;
  const paneHeight = (facadeHeight - gapY * (rows + 1)) / rows;
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      addBox(
        parent,
        [paneWidth, paneHeight, 0.07],
        [
          x - width / 2 + gapX + paneWidth / 2 + column * (paneWidth + gapX),
          y + gapY + paneHeight / 2 + row * (paneHeight + gapY),
          z + depth / 2 + 0.04
        ],
        material,
        false
      );
    }
  }
}

function addSideWindows(
  parent: THREE.Object3D,
  depth: number,
  facadeHeight: number,
  width: number,
  columns: number,
  rows: number,
  material: THREE.Material,
  side: 1 | -1
) {
  const paneDepth = Math.max(0.35, (depth - 0.22 * (columns + 1)) / columns);
  const paneHeight = Math.max(0.35, (facadeHeight - 0.2 * (rows + 1)) / rows);
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      addBox(
        parent,
        [0.07, paneHeight, paneDepth],
        [
          side * (width / 2 + 0.04),
          0.2 + paneHeight / 2 + row * (paneHeight + 0.2),
          -depth / 2 + 0.22 + paneDepth / 2 + column * (paneDepth + 0.22)
        ],
        material,
        false
      );
    }
  }
}

function addLoadingDoors(
  parent: THREE.Object3D,
  width: number,
  depth: number,
  count: number,
  material: THREE.Material,
  accent: THREE.Material,
  height = 1.55
) {
  const usableWidth = width - 0.7;
  const bay = usableWidth / count;
  for (let index = 0; index < count; index += 1) {
    const x = -usableWidth / 2 + bay * (index + 0.5);
    addBox(parent, [bay * 0.72, height, 0.09], [x, height / 2 + 0.12, depth / 2 + 0.05], material, false);
    for (let slat = 1; slat < 5; slat += 1) {
      addBox(
        parent,
        [bay * 0.68, 0.025, 0.035],
        [x, 0.18 + (height / 5) * slat, depth / 2 + 0.105],
        accent,
        false
      );
    }
    addCylinder(parent, 0.065, 0.48, [x - bay * 0.38, 0.24, depth / 2 + 0.22], accent, 10);
    addCylinder(parent, 0.065, 0.48, [x + bay * 0.38, 0.24, depth / 2 + 0.22], accent, 10);
  }
}

function addRoofUnit(
  parent: THREE.Object3D,
  position: Vec3,
  size: readonly [number, number, number],
  palette: MaterialPalette
) {
  addBox(parent, size, position, palette.roof);
  const grilleY = position[1] + 0.02;
  for (let index = -2; index <= 2; index += 1) {
    addBox(
      parent,
      [0.035, size[1] * 0.58, size[2] * 0.72],
      [position[0] + index * (size[0] / 7), grilleY, position[2] + size[2] / 2 + 0.025],
      palette.blackSteel,
      false
    );
  }
}

function addLot(
  parent: THREE.Group,
  spec: BuildingSpec,
  palette: MaterialPalette,
  selection: THREE.MeshBasicMaterial
) {
  const [width, depth] = spec.lot;
  addBox(parent, [width, 0.1, depth], [0, 0.01, 0], palette.concrete, false);
  const selectionPad = new THREE.Mesh(new THREE.PlaneGeometry(width + 0.38, depth + 0.38), selection);
  selectionPad.rotation.x = -Math.PI / 2;
  selectionPad.position.y = 0.072;
  selectionPad.userData.ignoreRaycast = true;
  parent.add(selectionPad);

  const outlineGeometry = new THREE.EdgesGeometry(new THREE.BoxGeometry(width + 0.12, 0.12, depth + 0.12));
  const outline = new THREE.LineSegments(
    outlineGeometry,
    new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.34 })
  );
  outline.position.y = 0.02;
  outline.userData.ignoreRaycast = true;
  parent.add(outline);
}

function buildIntake(
  group: THREE.Group,
  palette: MaterialPalette,
  accent: THREE.MeshStandardMaterial
) {
  addBox(group, [5.9, 2.55, 4.15], [0, 1.34, 0], palette.white);
  addBox(group, [6.12, 0.18, 4.36], [0, 2.68, 0], palette.roof);
  addLoadingDoors(group, 3.65, 4.15, 2, palette.blackSteel, accent, 1.48);
  addFrontWindows(group, 1.45, 1.05, 4.15, 2, 1, palette.glass, -2.02, 1.12);
  addBox(group, [1.8, 0.16, 1.02], [-1.92, 2.07, 2.52], accent);
  addBox(group, [0.09, 1.65, 0.09], [-2.68, 1.24, 2.91], palette.blackSteel);
  addBox(group, [0.09, 1.65, 0.09], [-1.16, 1.24, 2.91], palette.blackSteel);
  addRoofUnit(group, [1.2, 3.03, -0.45], [1.15, 0.52, 0.86], palette);
  addCylinder(group, 0.16, 0.5, [-1.65, 3.03, -0.7], palette.steel, 12);
}

function buildAutomation(
  group: THREE.Group,
  palette: MaterialPalette,
  accent: THREE.MeshStandardMaterial
) {
  addBox(group, [5.4, 5.7, 4.85], [-0.65, 2.9, -0.2], palette.white);
  addBox(group, [5.66, 0.2, 5.08], [-0.65, 5.83, -0.2], palette.roof);
  addBox(group, [2.05, 2.75, 4.4], [3.05, 1.43, 0.08], palette.light);
  addBox(group, [2.22, 0.18, 4.62], [3.05, 2.88, 0.08], palette.roof);
  addFrontWindows(group, 4.5, 3.82, 4.85, 5, 3, palette.glass, -0.65, 0.72, -0.2);
  addSideWindows(group, 4.85, 3.75, 5.4, 4, 3, palette.glass, -1);
  addLoadingDoors(group, 1.76, 4.4, 1, palette.blackSteel, accent, 1.72);
  addBox(group, [5.52, 0.13, 0.18], [-0.65, 4.95, 2.28], accent);
  addRoofUnit(group, [-1.7, 6.2, -0.75], [1.35, 0.55, 1.02], palette);
  addRoofUnit(group, [0.35, 6.15, 0.5], [1.12, 0.48, 0.9], palette);
  for (const x of [-2.35, 1.2]) {
    addCylinder(group, 0.16, 1.45, [x, 6.65, -1.5], palette.steel, 14);
    addCylinder(group, 0.23, 0.13, [x, 7.39, -1.5], palette.blackSteel, 14);
    addBox(group, [0.43, 0.12, 0.43], [x, 6.15, -1.5], accent);
  }
}

function buildHmi(
  group: THREE.Group,
  palette: MaterialPalette,
  accent: THREE.MeshStandardMaterial
) {
  addBox(group, [4.45, 4.72, 4.25], [0, 2.43, 0], palette.glass);
  addBox(group, [1.05, 4.92, 4.48], [-2.25, 2.53, 0], palette.white);
  addBox(group, [1.05, 4.92, 4.48], [2.25, 2.53, 0], palette.white);
  addBox(group, [5.6, 0.2, 4.58], [0, 5.04, 0], palette.roof);
  for (let x = -1.55; x <= 1.56; x += 0.78) {
    addBox(group, [0.075, 4.2, 0.11], [x, 2.42, 2.19], palette.white, false);
  }
  for (const y of [1.18, 2.37, 3.56, 4.64]) {
    addBox(group, [4.5, 0.075, 0.11], [0, y, 2.19], palette.white, false);
  }
  addBox(group, [4.72, 0.12, 0.18], [0, 4.71, 2.25], accent);
  addBox(group, [1.06, 0.15, 1.28], [0, 1.65, 2.74], accent);
  addBox(group, [0.07, 1.58, 0.07], [-0.46, 0.88, 2.94], palette.blackSteel);
  addBox(group, [0.07, 1.58, 0.07], [0.46, 0.88, 2.94], palette.blackSteel);
  addCylinder(group, 0.055, 1.45, [0, 5.8, 0], palette.steel, 10);
  const antenna = new THREE.Mesh(
    new THREE.SphereGeometry(0.18, 16, 10),
    accent
  );
  antenna.position.set(0, 6.55, 0);
  antenna.castShadow = true;
  group.add(antenna);
}

function buildElectrical(
  group: THREE.Group,
  palette: MaterialPalette,
  accent: THREE.MeshStandardMaterial
) {
  addBox(group, [4.15, 2.72, 4.25], [-2.15, 1.43, -0.2], palette.white);
  addBox(group, [4.38, 0.18, 4.48], [-2.15, 2.87, -0.2], palette.roof);
  addFrontWindows(group, 2.9, 1.25, 4.25, 3, 1, palette.glass, -2.15, 1.05, -0.2);
  addBox(group, [0.94, 1.85, 0.09], [-0.4, 1.02, 1.97], palette.blackSteel, false);
  addBox(group, [4.22, 0.13, 0.16], [-2.15, 2.35, 1.94], accent);
  addRoofUnit(group, [-2.65, 3.19, -0.55], [1.12, 0.48, 0.92], palette);

  for (let index = 0; index < 3; index += 1) {
    const x = 0.75 + index * 1.18;
    addBox(group, [0.82, 1.1, 1.35], [x, 0.62, -0.15], palette.steel);
    for (const offset of [-0.22, 0.22]) {
      addCylinder(group, 0.105, 0.62, [x + offset, 1.48, -0.15], palette.warning, 12);
    }
    addBox(group, [0.94, 0.1, 1.47], [x, 1.19, -0.15], palette.blackSteel);
  }
  for (const x of [0.15, 1.34, 2.52, 3.7]) {
    addCylinder(group, 0.045, 2.15, [x, 1.08, -2.25], palette.steel, 8);
    addCylinder(group, 0.045, 2.15, [x, 1.08, 2.25], palette.steel, 8);
  }
  for (const z of [-2.25, 2.25]) {
    addBox(group, [3.75, 0.055, 0.055], [1.92, 1.48, z], palette.steel);
    addBox(group, [3.75, 0.055, 0.055], [1.92, 0.85, z], palette.steel);
  }
  for (let index = 0; index < 3; index += 1) {
    addBox(group, [1.18, 0.06, 0.06], [0.75 + index * 1.18, 2.02, -0.15], accent);
  }
}

function buildQa(
  group: THREE.Group,
  palette: MaterialPalette,
  accent: THREE.MeshStandardMaterial
) {
  addBox(group, [7.25, 3.45, 5.45], [0, 1.8, -0.2], palette.white);
  addBox(group, [7.5, 0.2, 5.7], [0, 3.61, -0.2], palette.roof);
  addLoadingDoors(group, 5.3, 5.45, 3, palette.blackSteel, accent, 1.85);
  addFrontWindows(group, 1.2, 1.15, 5.45, 2, 1, palette.glass, -3, 2, -0.2);
  addBox(group, [7.35, 0.14, 0.17], [0, 3.08, 2.46], accent);
  for (const x of [-2.3, 0, 2.3]) {
    addRoofUnit(group, [x, 3.98, -0.65], [1.3, 0.5, 1.08], palette);
  }
  for (const x of [-3.15, 3.15]) {
    addBox(group, [0.16, 2.8, 0.16], [x, 1.46, 3.25], palette.blackSteel);
  }
  addBox(group, [6.45, 0.16, 0.18], [0, 2.78, 3.25], palette.blackSteel);
  addBox(group, [5.75, 0.12, 0.16], [0, 2.52, 3.25], accent);
  addCylinder(group, 0.2, 0.32, [0, 2.3, 3.25], palette.warning, 12);
}

function buildDocumentation(
  group: THREE.Group,
  palette: MaterialPalette,
  accent: THREE.MeshStandardMaterial
) {
  addBox(group, [4.55, 5.32, 3.8], [-0.72, 2.73, -0.45], palette.white);
  addBox(group, [4.78, 0.19, 4.04], [-0.72, 5.49, -0.45], palette.roof);
  for (let floor = 0; floor < 4; floor += 1) {
    addBox(group, [4.16, 0.68, 0.08], [-0.72, 0.75 + floor * 1.12, 1.49], palette.glass, false);
    addBox(group, [4.68, 0.12, 0.13], [-0.72, 1.15 + floor * 1.12, 1.54], floor === 3 ? accent : palette.light, false);
    for (const x of [-1.7, -0.65, 0.4]) {
      addBox(group, [0.07, 0.68, 0.12], [x, 0.75 + floor * 1.12, 1.55], palette.white, false);
    }
  }
  addBox(group, [2.12, 2.3, 3.55], [2.65, 1.23, 0.4], palette.light);
  addBox(group, [2.3, 0.18, 3.78], [2.65, 2.46, 0.4], palette.roof);
  addFrontWindows(group, 1.56, 1.18, 3.55, 2, 1, palette.glass, 2.65, 0.86, 0.4);
  addRoofUnit(group, [-0.72, 5.86, -0.55], [1.4, 0.48, 0.94], palette);
  for (const x of [-1.55, 0.1]) {
    addCylinder(group, 0.1, 0.48, [x, 5.92, 0.44], palette.steel, 12);
  }
}

function buildApproval(
  group: THREE.Group,
  palette: MaterialPalette,
  accent: THREE.MeshStandardMaterial
) {
  addBox(group, [5.25, 3.6, 3.95], [0, 1.88, -0.25], palette.white);
  addBox(group, [5.52, 0.2, 4.22], [0, 3.78, -0.25], palette.roof);
  addFrontWindows(group, 4.45, 1.65, 3.95, 5, 2, palette.glass, 0, 1.65, -0.25);
  addBox(group, [1.24, 2.55, 0.11], [0, 1.38, 1.79], palette.glass, false);
  addBox(group, [2.55, 0.2, 1.28], [0, 3.08, 2.32], accent);
  for (const x of [-1.06, 1.06]) {
    addCylinder(group, 0.12, 2.72, [x, 1.62, 2.61], palette.white, 18);
  }
  for (let step = 0; step < 3; step += 1) {
    addBox(
      group,
      [3.15 - step * 0.28, 0.12, 0.55],
      [0, 0.13 + step * 0.1, 2.72 + step * 0.36],
      palette.concrete,
      false
    );
  }
  addBox(group, [5.34, 0.11, 0.16], [0, 3.31, 1.75], accent);
  addCylinder(group, 0.05, 1.1, [2.2, 4.43, -0.5], palette.steel, 10);
  addBox(group, [0.72, 0.38, 0.04], [2.2, 4.76, -0.47], accent, false);
}

function buildRelease(
  group: THREE.Group,
  palette: MaterialPalette,
  accent: THREE.MeshStandardMaterial
) {
  addBox(group, [7.35, 3.22, 5.35], [0, 1.68, 0], palette.white);
  addBox(group, [7.6, 0.2, 5.62], [0, 3.39, 0], palette.roof);
  addLoadingDoors(group, 6.35, 5.35, 4, palette.blackSteel, accent, 1.76);
  addBox(group, [1.72, 4.48, 2.15], [-2.48, 2.33, -1.28], palette.light);
  addBox(group, [1.92, 0.18, 2.37], [-2.48, 4.65, -1.28], palette.roof);
  addFrontWindows(group, 1.28, 2.85, 2.15, 2, 3, palette.glass, -2.48, 1.18, -1.28);
  addBox(group, [7.45, 0.14, 0.17], [0, 2.84, 2.6], accent);
  for (const x of [-0.9, 1.1, 2.75]) {
    addRoofUnit(group, [x, 3.74, -0.55], [1.15, 0.5, 0.92], palette);
  }
  addBox(group, [6.5, 0.14, 1.08], [0.22, 2.45, 3.28], palette.blackSteel);
  for (const x of [-2.75, -0.95, 0.85, 2.65]) {
    addBox(group, [0.11, 2.05, 0.11], [x, 1.42, 3.62], palette.blackSteel);
  }
}

function buildArchitecture(
  group: THREE.Group,
  id: BuildingId,
  palette: MaterialPalette,
  accent: THREE.MeshStandardMaterial
) {
  switch (id) {
    case "intake":
      buildIntake(group, palette, accent);
      break;
    case "automation":
      buildAutomation(group, palette, accent);
      break;
    case "hmi":
      buildHmi(group, palette, accent);
      break;
    case "electrical":
      buildElectrical(group, palette, accent);
      break;
    case "qa":
      buildQa(group, palette, accent);
      break;
    case "documentation":
      buildDocumentation(group, palette, accent);
      break;
    case "approval":
      buildApproval(group, palette, accent);
      break;
    case "release":
      buildRelease(group, palette, accent);
      break;
  }
}

function createBuilding(
  scene: THREE.Scene,
  spec: BuildingSpec,
  palette: MaterialPalette,
  pickable: THREE.Mesh[]
): BuildingRuntime {
  const group = new THREE.Group();
  group.position.set(spec.position[0], 0, spec.position[1]);
  group.userData.buildingId = spec.id;

  const accent = makeAccentMaterial();
  const selection = new THREE.MeshBasicMaterial({
    color: 0xff7618,
    transparent: true,
    opacity: 0,
    depthWrite: false,
    side: THREE.DoubleSide
  });
  addLot(group, spec, palette, selection);
  buildArchitecture(group, spec.id, palette, accent);

  group.traverse((object) => {
    if (object instanceof THREE.Mesh && object.userData.ignoreRaycast !== true) {
      object.userData.buildingId = spec.id;
      pickable.push(object);
    }
  });
  scene.add(group);
  return { accent, selection };
}

function createAgentMarkers(
  scene: THREE.Scene,
  palette: MaterialPalette
): AgentRuntime[] {
  const markers: AgentRuntime[] = [];
  const sortedAgents = [...agents].sort((left, right) => left.id.localeCompare(right.id));
  sortedAgents.forEach((agent, globalIndex) => {
    const spec = BUILDING_SPECS.find((candidate) => candidate.id === agent.building);
    if (!spec) return;
    const siblings = sortedAgents.filter((candidate) => candidate.building === agent.building);
    const siblingIndex = siblings.findIndex((candidate) => candidate.id === agent.id);
    const statusMaterial =
      agent.state === "complete"
        ? palette.success
        : agent.state === "waiting"
          ? palette.warning
          : makeAccentMaterial();
    const statusColor =
      agent.state === "complete" ? 0x51b87b : agent.state === "waiting" ? 0xe1a637 : 0xff7618;

    const root = new THREE.Group();
    const baseY = 0.18;
    root.position.set(
      spec.position[0] - spec.lot[0] / 2 + 0.65 + siblingIndex * 0.72,
      baseY,
      spec.position[1] + spec.lot[1] / 2 + 0.5
    );
    root.userData.agentId = agent.id;

    addCylinder(root, 0.075, 0.55, [0, 0.31, 0], palette.blackSteel, 10);
    const core = new THREE.Mesh(new THREE.SphereGeometry(0.19, 16, 12), statusMaterial);
    core.position.y = 0.68;
    core.castShadow = true;
    root.add(core);
    const haloMaterial = new THREE.MeshBasicMaterial({
      color: statusColor,
      transparent: true,
      opacity: 0.7,
      depthWrite: false
    });
    const halo = new THREE.Mesh(new THREE.TorusGeometry(0.32, 0.025, 8, 28), haloMaterial);
    halo.rotation.x = Math.PI / 2;
    halo.position.y = 0.08;
    root.add(halo);
    scene.add(root);
    markers.push({ root, halo, baseY, phase: globalIndex * 1.17 });
  });
  return markers;
}

function createGround(scene: THREE.Scene) {
  const groundMaterial = new THREE.MeshStandardMaterial({
    color: 0x050606,
    roughness: 0.96,
    metalness: 0.04
  });
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(54, 42), groundMaterial);
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  const fineGrid = new THREE.GridHelper(54, 54, 0xffffff, 0xffffff);
  fineGrid.position.y = 0.061;
  const fineMaterial = fineGrid.material as THREE.LineBasicMaterial;
  fineMaterial.transparent = true;
  fineMaterial.opacity = 0.29;
  fineMaterial.depthWrite = false;
  scene.add(fineGrid);

  const majorGrid = new THREE.GridHelper(54, 18, 0xffffff, 0xffffff);
  majorGrid.position.y = 0.065;
  const majorMaterial = majorGrid.material as THREE.LineBasicMaterial;
  majorMaterial.transparent = true;
  majorMaterial.opacity = 0.5;
  majorMaterial.depthWrite = false;
  scene.add(majorGrid);

  const border = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(54.04, 0.03, 42.04)),
    new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.86 })
  );
  border.position.y = 0.03;
  scene.add(border);

  const routePoints = BUILDING_SPECS.map(
    (spec) => new THREE.Vector3(spec.position[0], 0.085, spec.position[1])
  );
  const route = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(routePoints),
    new THREE.LineDashedMaterial({
      color: 0xff7618,
      transparent: true,
      opacity: 0.58,
      dashSize: 0.38,
      gapSize: 0.3
    })
  );
  route.computeLineDistances();
  scene.add(route);

  for (const spec of BUILDING_SPECS) {
    const node = new THREE.Mesh(
      new THREE.CylinderGeometry(0.11, 0.11, 0.07, 16),
      new THREE.MeshBasicMaterial({ color: 0xff7618 })
    );
    node.position.set(spec.position[0], 0.11, spec.position[1]);
    scene.add(node);
  }
}

function createLights(scene: THREE.Scene) {
  const hemisphere = new THREE.HemisphereLight(0xffffff, 0x080a0a, 1.65);
  scene.add(hemisphere);

  const key = new THREE.DirectionalLight(0xffffff, 4.1);
  key.position.set(-13, 25, 12);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.left = -29;
  key.shadow.camera.right = 29;
  key.shadow.camera.top = 24;
  key.shadow.camera.bottom = -24;
  key.shadow.camera.near = 1;
  key.shadow.camera.far = 70;
  key.shadow.bias = -0.00035;
  key.shadow.normalBias = 0.025;
  scene.add(key);

  const rim = new THREE.DirectionalLight(0xa9d8ea, 1.25);
  rim.position.set(22, 15, -20);
  scene.add(rim);

  const warmFill = new THREE.PointLight(0xff8a3c, 18, 28, 2);
  warmFill.position.set(-4, 8, 2);
  scene.add(warmFill);
}

function setSelected(
  runtimes: Map<BuildingId, BuildingRuntime>,
  selected: BuildingId | null
) {
  runtimes.forEach((runtime, id) => {
    const active = id === selected;
    runtime.selection.opacity = active ? 0.22 : 0;
    runtime.accent.emissiveIntensity = active ? 1.15 : 0.18;
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

const occupancyFor = (id: BuildingId) =>
  agents.filter((agent) => agent.building === id).length;

export function ThreeCity({
  selectedBuilding,
  selectBuilding,
  paused,
  reducedMotion,
  speed,
  zoom
}: ThreeCityProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const cameraRef = useRef<THREE.OrthographicCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const runtimesRef = useRef<Map<BuildingId, BuildingRuntime>>(new Map());
  const selectedRef = useRef<BuildingId | null>(selectedBuilding);
  const selectRef = useRef(selectBuilding);
  const animationRef = useRef({ paused, reducedMotion, speed });
  const [renderError, setRenderError] = useState<string | null>(null);

  selectedRef.current = selectedBuilding;
  selectRef.current = selectBuilding;
  animationRef.current = { paused, reducedMotion, speed };

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    let disposed = false;
    let frame = 0;
    let resizeObserver: ResizeObserver | null = null;
    let renderer: THREE.WebGLRenderer | null = null;
    let controls: OrbitControls | null = null;
    let scene: THREE.Scene | null = null;
    let pointerStart: readonly [number, number] | null = null;
    const pickable: THREE.Mesh[] = [];
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();

    try {
      setRenderError(null);
      scene = new THREE.Scene();
      scene.background = new THREE.Color(0x020303);
      scene.fog = new THREE.Fog(0x020303, 37, 72);

      renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: false,
        powerPreference: "high-performance"
      });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.shadowMap.enabled = true;
      renderer.shadowMap.type = THREE.PCFShadowMap;
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 1.16;
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

      const camera = new THREE.OrthographicCamera(-20, 20, 14, -14, 0.1, 110);
      camera.position.set(27, 24, 29);
      camera.zoom = THREE.MathUtils.clamp(zoom, 0.75, 1.55);
      camera.lookAt(0, 1.2, 0);
      cameraRef.current = camera;

      controls = new OrbitControls(camera, renderer.domElement);
      controls.target.set(0, 1.35, 0);
      controls.enableDamping = !animationRef.current.reducedMotion;
      controls.dampingFactor = 0.075;
      controls.enablePan = false;
      controls.screenSpacePanning = false;
      controls.minPolarAngle = THREE.MathUtils.degToRad(38);
      controls.maxPolarAngle = THREE.MathUtils.degToRad(66);
      controls.minAzimuthAngle = THREE.MathUtils.degToRad(14);
      controls.maxAzimuthAngle = THREE.MathUtils.degToRad(78);
      controls.minZoom = 0.72;
      controls.maxZoom = 1.75;
      controls.update();
      controlsRef.current = controls;

      createGround(scene);
      createLights(scene);
      const palette = createPalette();
      const runtimes = new Map<BuildingId, BuildingRuntime>();
      for (const spec of BUILDING_SPECS) {
        runtimes.set(spec.id, createBuilding(scene, spec, palette, pickable));
      }
      runtimesRef.current = runtimes;
      setSelected(runtimes, selectedRef.current);
      const agentMarkers = createAgentMarkers(scene, palette);

      const updatePointer = (event: PointerEvent) => {
        if (!renderer) return;
        const bounds = renderer.domElement.getBoundingClientRect();
        pointer.x = ((event.clientX - bounds.left) / Math.max(1, bounds.width)) * 2 - 1;
        pointer.y = -((event.clientY - bounds.top) / Math.max(1, bounds.height)) * 2 + 1;
      };
      const pickBuilding = (event: PointerEvent) => {
        updatePointer(event);
        raycaster.setFromCamera(pointer, camera);
        const hit = raycaster.intersectObjects(pickable, false)[0];
        return hit?.object.userData.buildingId as BuildingId | undefined;
      };
      const onPointerDown = (event: PointerEvent) => {
        pointerStart = [event.clientX, event.clientY];
      };
      const onPointerMove = (event: PointerEvent) => {
        const buildingId = pickBuilding(event);
        renderer!.domElement.style.cursor = buildingId ? "pointer" : event.buttons ? "grabbing" : "grab";
      };
      const onPointerUp = (event: PointerEvent) => {
        if (!pointerStart || event.button !== 0) {
          pointerStart = null;
          return;
        }
        const movement = Math.hypot(
          event.clientX - pointerStart[0],
          event.clientY - pointerStart[1]
        );
        pointerStart = null;
        if (movement > 7) return;
        const buildingId = pickBuilding(event);
        if (buildingId) selectRef.current(buildingId);
      };
      const onPointerLeave = () => {
        pointerStart = null;
        if (renderer) renderer.domElement.style.cursor = "grab";
      };
      const onContextLost = (event: Event) => {
        event.preventDefault();
        if (!disposed) {
          setRenderError("The WebGL context was lost. Reload the page to restore the 3D city.");
        }
      };
      renderer.domElement.addEventListener("pointerdown", onPointerDown);
      renderer.domElement.addEventListener("pointermove", onPointerMove);
      renderer.domElement.addEventListener("pointerup", onPointerUp);
      renderer.domElement.addEventListener("pointerleave", onPointerLeave);
      renderer.domElement.addEventListener("webglcontextlost", onContextLost);

      const resize = () => {
        if (!renderer) return;
        const bounds = mount.getBoundingClientRect();
        const width = Math.max(1, Math.round(bounds.width));
        const height = Math.max(1, Math.round(bounds.height));
        const aspect = width / height;
        const verticalSize = 29;
        camera.left = (-verticalSize * aspect) / 2;
        camera.right = (verticalSize * aspect) / 2;
        camera.top = verticalSize / 2;
        camera.bottom = -verticalSize / 2;
        camera.updateProjectionMatrix();
        renderer.setSize(width, height, false);
      };
      resize();
      resizeObserver = new ResizeObserver(resize);
      resizeObserver.observe(mount);

      const timer = new THREE.Timer();
      timer.connect(document);
      let animationElapsed = 0;
      const render = (timestamp?: number) => {
        if (disposed || !renderer || !scene || !controls) return;
        frame = window.requestAnimationFrame(render);
        timer.update(timestamp);
        const delta = Math.min(timer.getDelta(), 0.05);
        const state = animationRef.current;
        controls.enableDamping = !state.reducedMotion;
        controls.update();
        if (!state.paused && !state.reducedMotion) {
          animationElapsed += delta * Math.max(0.1, state.speed);
          agentMarkers.forEach((marker) => {
            const wave = Math.sin(animationElapsed * 2.1 + marker.phase);
            marker.root.position.y = marker.baseY + wave * 0.055;
            const scale = 1 + (wave + 1) * 0.13;
            marker.halo.scale.setScalar(scale);
            marker.halo.material.opacity = 0.34 + (wave + 1) * 0.18;
          });
        } else {
          agentMarkers.forEach((marker) => {
            marker.root.position.y = marker.baseY;
            marker.halo.scale.setScalar(1);
            marker.halo.material.opacity = 0.56;
          });
        }
        renderer.render(scene, camera);
      };
      render();

      return () => {
        disposed = true;
        window.cancelAnimationFrame(frame);
        timer.dispose();
        resizeObserver?.disconnect();
        renderer?.domElement.removeEventListener("pointerdown", onPointerDown);
        renderer?.domElement.removeEventListener("pointermove", onPointerMove);
        renderer?.domElement.removeEventListener("pointerup", onPointerUp);
        renderer?.domElement.removeEventListener("pointerleave", onPointerLeave);
        renderer?.domElement.removeEventListener("webglcontextlost", onContextLost);
        controls?.dispose();
        controlsRef.current = null;
        cameraRef.current = null;
        runtimesRef.current = new Map();
        if (scene) disposeScene(scene);
        renderer?.renderLists.dispose();
        renderer?.dispose();
        renderer?.forceContextLoss();
        renderer?.domElement.remove();
      };
    } catch (error: unknown) {
      disposed = true;
      controls?.dispose();
      if (scene) disposeScene(scene);
      renderer?.dispose();
      renderer?.domElement.remove();
      setRenderError(
        error instanceof Error
          ? error.message
          : "This device could not initialize the WebGL city renderer."
      );
    }
  }, []);

  useEffect(() => {
    setSelected(runtimesRef.current, selectedBuilding);
  }, [selectedBuilding]);

  useEffect(() => {
    const camera = cameraRef.current;
    if (!camera) return;
    camera.zoom = THREE.MathUtils.clamp(zoom, 0.75, 1.55);
    camera.updateProjectionMatrix();
    controlsRef.current?.update();
  }, [zoom]);

  useEffect(() => {
    if (controlsRef.current) controlsRef.current.enableDamping = !reducedMotion;
  }, [reducedMotion]);

  return (
    <div className="three-city-host pixi-city-host" style={ROOT_STYLE}>
      <div ref={mountRef} style={CANVAS_MOUNT_STYLE} aria-hidden="true" />
      <div style={MODE_BADGE_STYLE}>ORTHOGRAPHIC 3D · DRAG TO ORBIT · CLICK TO SELECT</div>

      {renderError && (
        <div className="city-render-error three-city-error" style={ERROR_STYLE} role="alert">
          <strong style={{ color: "#ff8a3c", fontSize: 14 }}>3D city renderer unavailable</strong>
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
          const occupancy = occupancyFor(building.id);
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
                border: selected ? "1px solid #ff8a3c" : "1px solid rgba(255,255,255,.24)",
                borderRadius: 2,
                background: selected ? "rgba(255,118,24,.2)" : "rgba(3,4,4,.84)",
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
