import { useEffect, useRef, useState } from "react";
import { Application, Container, Graphics, Text } from "pixi.js";
import { agents, buildings, type BuildingId } from "../domain";

const WORLD_WIDTH = 1_000;
const WORLD_HEIGHT = 520;

type Point = [number, number];
type BuildingKind = "intake" | "automation" | "hmi" | "electrical" | "qa" | "documentation" | "approval" | "release";

interface BuildingVisual {
  id: BuildingId;
  kind: BuildingKind;
  x: number;
  y: number;
  width: number;
  depth: number;
  height: number;
  accent: number;
  label: string;
  code: string;
}

const visuals: BuildingVisual[] = [
  { id: "intake", kind: "intake", x: 150, y: 198, width: 132, depth: 70, height: 58, accent: 0xff791a, label: "PROJECT INTAKE", code: "01" },
  { id: "automation", kind: "automation", x: 390, y: 170, width: 174, depth: 88, height: 98, accent: 0xff791a, label: "AUTOMATION", code: "02" },
  { id: "hmi", kind: "hmi", x: 650, y: 192, width: 138, depth: 78, height: 88, accent: 0xe36536, label: "HMI DEVELOPMENT", code: "03" },
  { id: "electrical", kind: "electrical", x: 838, y: 318, width: 142, depth: 76, height: 68, accent: 0xf1a83b, label: "ELECTRICAL", code: "04" },
  { id: "qa", kind: "qa", x: 646, y: 408, width: 172, depth: 88, height: 64, accent: 0xf0a735, label: "QA + TESTING", code: "05" },
  { id: "documentation", kind: "documentation", x: 385, y: 430, width: 144, depth: 76, height: 82, accent: 0xff791a, label: "DOCUMENTATION", code: "06" },
  { id: "approval", kind: "approval", x: 154, y: 366, width: 120, depth: 68, height: 64, accent: 0xf1a83b, label: "APPROVAL OFFICE", code: "07" },
  { id: "release", kind: "release", x: 850, y: 458, width: 158, depth: 82, height: 60, accent: 0x58b77d, label: "RELEASE CENTER", code: "08" }
];

const occupancyFor = (id: BuildingId) => agents.filter((agent) => agent.building === id).length;
const points = (values: Point[]) => values.flatMap(([x, y]) => [x, y]);

function polygon(graphics: Graphics, values: Point[], color: number, alpha = 1, stroke?: { color: number; width: number; alpha?: number }) {
  graphics.poly(points(values), true).fill({ color, alpha });
  if (stroke) graphics.stroke({ color: stroke.color, width: stroke.width, alpha: stroke.alpha ?? 1 });
  return graphics;
}

function prism(
  parent: Container,
  x: number,
  y: number,
  width: number,
  depth: number,
  height: number,
  colors: { roof: number; left: number; right: number },
  outline = 0x101216
) {
  const shape = new Graphics();
  const front: Point = [x, y];
  const right: Point = [x + width / 2, y - depth / 2];
  const back: Point = [x, y - depth];
  const left: Point = [x - width / 2, y - depth / 2];
  const topFront: Point = [x, y - height];
  const topRight: Point = [right[0], right[1] - height];
  const topBack: Point = [back[0], back[1] - height];
  const topLeft: Point = [left[0], left[1] - height];
  polygon(shape, [topLeft, topFront, front, left], colors.left, 1, { color: outline, width: 1 });
  polygon(shape, [topFront, topRight, right, front], colors.right, 1, { color: outline, width: 1 });
  polygon(shape, [topBack, topRight, topFront, topLeft], colors.roof, 1, { color: 0x6a7077, width: 1, alpha: 0.72 });
  parent.addChild(shape);
  return { shape, front, right, back, left, topFront, topRight, topBack, topLeft };
}

function facePanel(parent: Container, x: number, y: number, side: "left" | "right", width = 16, height = 12, color = 0x9fb6c6, alpha = 0.82) {
  const panel = new Graphics();
  const direction = side === "right" ? 1 : -1;
  polygon(panel, [[x, y], [x + direction * width, y - width / 2], [x + direction * width, y - width / 2 + height], [x, y + height]], color, alpha, { color: 0xd8e1e7, width: 0.6, alpha: 0.5 });
  parent.addChild(panel);
}

function roofBox(parent: Container, x: number, y: number, accent = 0x697079, width = 36) {
  prism(parent, x, y, width, 22, 12, { roof: 0x858b90, left: 0x484e55, right: accent }, 0x24272b);
  const grille = new Graphics().rect(x - 9, y - 24, 18, 2).fill({ color: 0x1a1d20, alpha: 0.75 });
  parent.addChild(grille);
}

function drawLot(parent: Container, visual: BuildingVisual, selected: boolean) {
  const { x, y, width, depth, accent } = visual;
  const lot = new Graphics();
  polygon(lot, [[x, y + 22], [x + width * 0.72, y - depth * 0.42 + 22], [x, y - depth * 0.92 + 22], [x - width * 0.72, y - depth * 0.42 + 22]], selected ? accent : 0x34383c, selected ? 0.16 : 0.62, { color: selected ? accent : 0x555b61, width: selected ? 2 : 1, alpha: selected ? 0.95 : 0.5 });
  polygon(lot, [[x + 8, y + 26], [x + width * 0.7 + 8, y - depth * 0.4 + 26], [x + 11, y - depth * 0.88 + 27], [x - width * 0.68 + 10, y - depth * 0.38 + 27]], 0x050607, 0.42);
  parent.addChild(lot);
}

function drawWindows(parent: Container, visual: BuildingVisual) {
  const { x, y, width, depth, height, kind } = visual;
  const glass = kind === "hmi" ? 0x73a6b7 : 0x7590a0;
  const rightCount = kind === "automation" ? 5 : kind === "qa" || kind === "release" ? 4 : 3;
  const leftCount = kind === "documentation" ? 4 : 3;
  for (let index = 0; index < rightCount; index += 1) {
    const ratio = (index + 0.7) / (rightCount + 0.4);
    facePanel(parent, x + ratio * width * 0.42, y - height + 18 - ratio * depth * 0.42, "right", kind === "hmi" ? 19 : 13, kind === "hmi" ? 18 : 11, glass, kind === "hmi" ? 0.9 : 0.7);
  }
  for (let index = 0; index < leftCount; index += 1) {
    const ratio = (index + 0.8) / (leftCount + 0.6);
    facePanel(parent, x - ratio * width * 0.4, y - height + 20 - ratio * depth * 0.4, "left", 13, 10, glass, 0.64);
  }
}

function drawLoadingDoors(parent: Container, visual: BuildingVisual) {
  const { x, y, width, depth, height, kind } = visual;
  const count = kind === "release" ? 4 : 2;
  for (let index = 0; index < count; index += 1) {
    const ratio = (index + 0.75) / (count + 0.25);
    const doorX = x + ratio * width * 0.42;
    const doorY = y - 4 - ratio * depth * 0.42;
    facePanel(parent, doorX, doorY - Math.min(28, height * 0.42), "right", kind === "release" ? 17 : 20, kind === "release" ? 25 : 28, 0x25292d, 1);
    const stripe = new Graphics();
    stripe.moveTo(doorX, doorY - 5).lineTo(doorX + 17, doorY - 13).stroke({ color: 0xff8a2a, width: 2, alpha: 0.9 });
    parent.addChild(stripe);
  }
}

function drawBuildingDetail(parent: Container, visual: BuildingVisual) {
  const { x, y, width, depth, height, kind, accent } = visual;
  if (kind === "automation") {
    roofBox(parent, x - 28, y - height - 21, 0x555c64, 42);
    roofBox(parent, x + 25, y - height - depth * 0.34, 0x555c64, 34);
    const stacks = new Graphics();
    for (const offset of [-56, 58]) {
      stacks.rect(x + offset - 4, y - height - depth * 0.55 - 34, 8, 35).fill({ color: 0x454a4f });
      stacks.ellipse(x + offset, y - height - depth * 0.55 - 35, 5, 2.5).fill({ color: 0x899096 });
      stacks.rect(x + offset - 4, y - height - depth * 0.55 - 16, 8, 3).fill({ color: accent });
    }
    parent.addChild(stacks);
  } else if (kind === "hmi") {
    const glassCrown = new Graphics();
    polygon(glassCrown, [[x, y - height - 4], [x + width * 0.34, y - height - depth * 0.34 - 4], [x, y - height - depth * 0.68 - 4], [x - width * 0.34, y - height - depth * 0.34 - 4]], 0x6d99a8, 0.72, { color: 0xe17a43, width: 2 });
    glassCrown.moveTo(x, y - height - depth * 0.66 - 4).lineTo(x, y - height - depth * 0.66 - 27).stroke({ color: 0xcbd5da, width: 2 });
    glassCrown.circle(x, y - height - depth * 0.66 - 30, 3).fill({ color: accent });
    parent.addChild(glassCrown);
  } else if (kind === "electrical") {
    roofBox(parent, x - 18, y - height - 17, 0x5b6066, 34);
    const yard = new Graphics();
    for (let index = 0; index < 4; index += 1) {
      const px = x + width * 0.55 + index * 10;
      const py = y - depth * 0.32 + 17 - index * 5;
      yard.rect(px - 1, py - 24, 3, 23).fill({ color: 0x777d83 });
      yard.circle(px, py - 25, 4).fill({ color: 0xd8a449 });
      yard.circle(px, py - 17, 3).fill({ color: 0xa67732 });
    }
    yard.moveTo(x + width * 0.48, y - depth * 0.2 + 21).lineTo(x + width * 0.88, y - depth * 0.58 + 20).stroke({ color: 0x858b91, width: 1 });
    parent.addChild(yard);
  } else if (kind === "qa") {
    roofBox(parent, x - 30, y - height - 19, 0x50565d, 34);
    roofBox(parent, x + 24, y - height - depth * 0.34, 0x50565d, 30);
    drawLoadingDoors(parent, visual);
  } else if (kind === "documentation") {
    const clerestory = new Graphics();
    polygon(clerestory, [[x - 36, y - height - 17], [x, y - height - 35], [x + 36, y - height - 17], [x, y - height + 1]], 0x688797, 0.88, { color: 0xaab8c0, width: 1 });
    clerestory.moveTo(x, y - height - 35).lineTo(x, y - height + 1).stroke({ color: 0x1f2529, width: 2 });
    parent.addChild(clerestory);
  } else if (kind === "approval") {
    const entrance = new Graphics();
    polygon(entrance, [[x + 14, y - 31], [x + 38, y - 43], [x + 38, y - 10], [x + 14, y + 2]], 0x181b1e, 1, { color: accent, width: 1.3 });
    entrance.circle(x + 31, y - 24, 2).fill({ color: 0xffb069 });
    parent.addChild(entrance);
  } else if (kind === "release") {
    roofBox(parent, x - 35, y - height - 18, 0x4d545a, 42);
    drawLoadingDoors(parent, visual);
  } else if (kind === "intake") {
    drawLoadingDoors(parent, visual);
    const canopy = new Graphics();
    polygon(canopy, [[x + 7, y - height + 22], [x + 48, y - height + 1], [x + 48, y - height + 8], [x + 7, y - height + 29]], accent, 0.88);
    parent.addChild(canopy);
  }
}

function drawStatusLabel(parent: Container, visual: BuildingVisual, selected: boolean) {
  const occupancy = occupancyFor(visual.id);
  const label = new Container();
  label.position.set(visual.x - visual.width * 0.53, visual.y + 26);
  const background = new Graphics().roundRect(0, 0, Math.max(108, visual.label.length * 6.3), 31, 3).fill({ color: 0x111317, alpha: 0.94 });
  background.stroke({ color: selected ? visual.accent : 0x454a50, width: selected ? 1.5 : 1, alpha: 0.9 });
  background.rect(0, 0, 3, 31).fill({ color: visual.accent });
  const code = new Text({ text: visual.code, style: { fontFamily: "Segoe UI", fontSize: 8, fontWeight: "700", fill: 0x7f878f } });
  code.position.set(9, 5);
  const title = new Text({ text: visual.label, style: { fontFamily: "Segoe UI", fontSize: 9, fontWeight: "700", fill: 0xe1e5e8, letterSpacing: 0.5 } });
  title.position.set(27, 4);
  const status = new Text({ text: occupancy ? `${occupancy} AGENT${occupancy === 1 ? "" : "S"} ACTIVE` : "STANDBY", style: { fontFamily: "Segoe UI", fontSize: 7, fill: occupancy ? visual.accent : 0x788089, letterSpacing: 0.4 } });
  status.position.set(27, 17);
  label.addChild(background, code, title, status);
  parent.addChild(label);
}

function drawBuilding(parent: Container, visual: BuildingVisual, selected: boolean, select: (id: BuildingId) => void) {
  const building = new Container();
  building.eventMode = "static";
  building.cursor = "pointer";
  drawLot(building, visual, selected);
  const shadow = new Graphics();
  polygon(shadow, [[visual.x + 12, visual.y + 9], [visual.x + visual.width * 0.62 + 20, visual.y - visual.depth * 0.38 + 12], [visual.x + 20, visual.y - visual.depth * 0.8 + 12], [visual.x - visual.width * 0.52 + 12, visual.y - visual.depth * 0.3 + 14]], 0x000000, 0.38);
  building.addChild(shadow);
  const glass = visual.kind === "hmi";
  prism(building, visual.x, visual.y, visual.width, visual.depth, visual.height, {
    roof: glass ? 0x657985 : visual.kind === "approval" ? 0x69655e : 0x646a70,
    left: glass ? 0x334c58 : visual.kind === "approval" ? 0x49443d : 0x353a40,
    right: glass ? 0x3e5f6d : visual.kind === "documentation" ? 0x424b51 : 0x444a50
  });
  const accentLine = new Graphics()
    .moveTo(visual.x - visual.width / 2, visual.y - visual.depth / 2 - visual.height + 2)
    .lineTo(visual.x, visual.y - visual.height + 2)
    .lineTo(visual.x + visual.width / 2, visual.y - visual.depth / 2 - visual.height + 2)
    .stroke({ color: visual.accent, width: selected ? 4 : 2.5, alpha: 0.98 });
  building.addChild(accentLine);
  drawWindows(building, visual);
  drawBuildingDetail(building, visual);
  drawStatusLabel(building, visual, selected);
  building.on("pointertap", () => select(visual.id));
  building.on("pointerover", () => { building.alpha = 0.88; });
  building.on("pointerout", () => { building.alpha = 1; });
  parent.addChild(building);
}

function dashedLine(graphics: Graphics, from: Point, to: Point, color: number) {
  const dx = to[0] - from[0];
  const dy = to[1] - from[1];
  const distance = Math.hypot(dx, dy);
  const dash = 9;
  const gap = 7;
  for (let position = 0; position < distance; position += dash + gap) {
    const start = position / distance;
    const end = Math.min(position + dash, distance) / distance;
    graphics.moveTo(from[0] + dx * start, from[1] + dy * start).lineTo(from[0] + dx * end, from[1] + dy * end);
  }
  graphics.stroke({ color, width: 1.4, alpha: 0.38 });
}

function drawCampusGround(parent: Container) {
  const ground = new Graphics();
  polygon(ground, [[500, 22], [966, 255], [500, 498], [34, 255]], 0x171b1e, 1, { color: 0x343a3e, width: 1 });
  polygon(ground, [[500, 36], [946, 257], [500, 484], [54, 257]], 0x1d2225, 1);
  for (let row = 0; row < 21; row += 1) {
    const y = 54 + row * 20;
    ground.moveTo(66 + row * 20, 257).lineTo(500, 40 + row * 10).stroke({ color: 0x667078, width: 0.5, alpha: 0.1 });
    ground.moveTo(934 - row * 20, 257).lineTo(500, 40 + row * 10).stroke({ color: 0x667078, width: 0.5, alpha: 0.1 });
    if (y > 0) ground.moveTo(500, y).lineTo(500, y + 0.1).stroke({ color: 0x000000, width: 0, alpha: 0 });
  }
  parent.addChild(ground);

  const roads = new Graphics();
  const roadColor = 0x30363a;
  polygon(roads, [[85, 263], [117, 247], [910, 426], [882, 443]], roadColor, 0.9, { color: 0x4d555a, width: 1, alpha: 0.45 });
  polygon(roads, [[480, 63], [511, 79], [515, 474], [482, 457]], roadColor, 0.86, { color: 0x4d555a, width: 1, alpha: 0.45 });
  polygon(roads, [[162, 163], [184, 150], [846, 398], [824, 412]], roadColor, 0.82);
  dashedLine(roads, [103, 259], [895, 435], 0xc0a16f);
  dashedLine(roads, [496, 71], [499, 465], 0xc0a16f);
  parent.addChild(roads);

  const landscape = new Graphics();
  const trees: Point[] = [[82, 228], [99, 216], [242, 119], [731, 116], [902, 254], [746, 463], [272, 441], [104, 400], [933, 403]];
  for (const [x, y] of trees) {
    landscape.ellipse(x + 4, y + 5, 10, 5).fill({ color: 0x050607, alpha: 0.45 });
    landscape.rect(x - 1.5, y - 8, 3, 12).fill({ color: 0x59432f });
    landscape.circle(x, y - 11, 7).fill({ color: 0x354c3c });
    landscape.circle(x + 5, y - 13, 5).fill({ color: 0x3e5945 });
  }
  parent.addChild(landscape);
}

function drawAgent(parent: Container, id: string, buildingId: BuildingId, initials: string, state: "active" | "waiting" | "complete") {
  const visual = visuals.find((item) => item.id === buildingId)!;
  const indexAtBuilding = agents
    .filter((agent) => agent.building === buildingId)
    .sort((left, right) => left.id.localeCompare(right.id))
    .findIndex((agent) => agent.id === id);
  const x = visual.x + visual.width * 0.44 + indexAtBuilding * 24;
  const y = visual.y - visual.height - visual.depth * 0.58 - 12 - indexAtBuilding * 5;
  const marker = new Container();
  marker.position.set(x, y);
  const stateColor = state === "complete" ? 0x58b77d : state === "waiting" ? 0xf1a83b : 0xff791a;
  const shadow = new Graphics().ellipse(3, 18, 13, 5).fill({ color: 0x000000, alpha: 0.46 });
  const ring = new Graphics().circle(0, 0, 17).stroke({ color: stateColor, width: 2, alpha: 0.45 });
  const body = new Graphics().circle(0, 0, 12).fill({ color: 0xe6eaed }).stroke({ color: 0x111317, width: 3 });
  body.arc(0, -3, 8, Math.PI, Math.PI * 2).fill({ color: stateColor });
  body.rect(-8, -4, 16, 3).fill({ color: stateColor });
  const text = new Text({ text: initials, style: { fontFamily: "Segoe UI", fontSize: 7, fontWeight: "800", fill: 0x111317 } });
  text.anchor.set(0.5);
  text.position.set(0, 4);
  marker.addChild(shadow, ring, body, text);
  marker.label = `${id}-agent`;
  parent.addChild(marker);
  return { marker, ring, baseY: y };
}

interface PixiCityProps {
  selectedBuilding: BuildingId | null;
  selectBuilding: (id: BuildingId) => void;
  paused: boolean;
  reducedMotion: boolean;
  speed: number;
  zoom: number;
}

export function PixiCity({ selectedBuilding, selectBuilding, paused, reducedMotion, speed, zoom }: PixiCityProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [renderError, setRenderError] = useState<string | null>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;
    let application: Application | undefined;
    let resizeObserver: ResizeObserver | undefined;

    const start = async () => {
      setRenderError(null);
      const app = new Application();
      await app.init({
        resizeTo: host,
        backgroundAlpha: 0,
        antialias: true,
        autoDensity: true,
        resolution: Math.min(window.devicePixelRatio || 1, 2)
      });
      if (disposed) {
        app.destroy({ removeView: true }, { children: true });
        return;
      }
      application = app;
      app.canvas.className = "pixi-city-canvas";
      app.canvas.setAttribute("aria-hidden", "true");
      host.appendChild(app.canvas);
      const world = new Container();
      app.stage.addChild(world);
      drawCampusGround(world);

      const routeLayer = new Graphics();
      const routeOrder: BuildingId[] = ["intake", "automation", "hmi", "electrical", "qa", "documentation", "approval", "release"];
      for (let index = 0; index < routeOrder.length - 1; index += 1) {
        const from = visuals.find((item) => item.id === routeOrder[index])!;
        const to = visuals.find((item) => item.id === routeOrder[index + 1])!;
        dashedLine(routeLayer, [from.x, from.y + 8], [to.x, to.y + 8], 0xff8c39);
      }
      world.addChild(routeLayer);
      visuals.forEach((visual) => drawBuilding(world, visual, selectedBuilding === visual.id, selectBuilding));

      const pulsing: Array<{ marker: Container; ring: Graphics; baseY: number }> = [];
      agents.forEach((agent) => {
        pulsing.push(drawAgent(world, agent.id, agent.building, agent.name.slice(0, 2).toUpperCase(), agent.state));
      });

      let elapsed = 0;
      app.ticker.add((ticker) => {
        if (paused || reducedMotion) return;
        elapsed += ticker.deltaTime * 0.045 * speed;
        pulsing.forEach(({ marker, ring, baseY }, index) => {
          const phase = elapsed + index * 0.8;
          marker.y = baseY + Math.sin(phase) * 1.2;
          ring.alpha = 0.22 + (Math.sin(phase * 1.3) + 1) * 0.16;
          ring.scale.set(0.92 + (Math.sin(phase * 1.3) + 1) * 0.08);
        });
      });

      const fit = () => {
        const scale = Math.min(app.renderer.width / WORLD_WIDTH, app.renderer.height / WORLD_HEIGHT) * zoom;
        world.scale.set(scale);
        world.position.set((app.renderer.width - WORLD_WIDTH * scale) / 2, (app.renderer.height - WORLD_HEIGHT * scale) / 2);
      };
      fit();
      resizeObserver = new ResizeObserver(fit);
      resizeObserver.observe(host);
    };
    void start().catch((error: unknown) => {
      if (!disposed) setRenderError(error instanceof Error ? error.message : "The city renderer could not initialize.");
    });

    return () => {
      disposed = true;
      resizeObserver?.disconnect();
      application?.destroy({ removeView: true }, { children: true });
    };
  }, [paused, reducedMotion, selectedBuilding, selectBuilding, speed, zoom]);

  return (
    <div className="pixi-city-host" ref={hostRef}>
      {renderError && (
        <div className="city-render-error" role="alert">
          <strong>3D city renderer unavailable</strong>
          <span>{renderError}</span>
          <span>Use the keyboard building list below to continue working.</span>
        </div>
      )}
      <div className="city-accessibility">
        {buildings.map((building) => (
          <button
            key={building.id}
            aria-label={`${building.name}, ${occupancyFor(building.id)} active agents`}
            aria-pressed={selectedBuilding === building.id}
            onClick={() => selectBuilding(building.id)}
            style={{ left: `${building.x}%`, top: `${building.y}%` }}
          >
            {building.name}
          </button>
        ))}
      </div>
    </div>
  );
}
