import { create } from "zustand";
import type { BuildingId } from "./domain";

interface UIState {
  selectedBuilding: BuildingId | null;
  paused: boolean;
  reducedMotion: boolean;
  speed: number;
  zoom: number;
  selectedFinding: string | null;
  selectBuilding: (id: BuildingId | null) => void;
  togglePause: () => void;
  setReducedMotion: (value: boolean) => void;
  setSpeed: (value: number) => void;
  setZoom: (value: number) => void;
  selectFinding: (id: string | null) => void;
}

const systemPrefersReducedMotion = typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export const useUI = create<UIState>((set) => ({
  selectedBuilding: null,
  paused: false,
  reducedMotion: systemPrefersReducedMotion,
  speed: 1,
  zoom: 1,
  selectedFinding: "F-104",
  selectBuilding: (id) => set({ selectedBuilding: id }),
  togglePause: () => set((state) => ({ paused: !state.paused })),
  setReducedMotion: (reducedMotion) => set({ reducedMotion }),
  setSpeed: (speed) => set({ speed }),
  setZoom: (zoom) => set({ zoom }),
  selectFinding: (selectedFinding) => set({ selectedFinding })
}));
