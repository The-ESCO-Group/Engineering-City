import { create } from "zustand";
import type { BuildingId } from "./domain";

interface UIState {
  selectedBuilding: BuildingId | null;
  paused: boolean;
  reducedMotion: boolean;
  speed: number;
  selectedFinding: string | null;
  selectBuilding: (id: BuildingId | null) => void;
  togglePause: () => void;
  setReducedMotion: (value: boolean) => void;
  setSpeed: (value: number) => void;
  selectFinding: (id: string | null) => void;
}
export const useUI = create<UIState>((set) => ({
  selectedBuilding: null, paused: false, reducedMotion: false, speed: 1, selectedFinding: "F-104",
  selectBuilding: (id) => set({ selectedBuilding: id }),
  togglePause: () => set((state) => ({ paused: !state.paused })),
  setReducedMotion: (reducedMotion) => set({ reducedMotion }),
  setSpeed: (speed) => set({ speed }),
  selectFinding: (selectedFinding) => set({ selectedFinding })
}));

