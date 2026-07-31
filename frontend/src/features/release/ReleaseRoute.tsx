import { Building2, MapPin, PackageCheck, Play, RotateCcw, ShieldCheck, Warehouse } from "lucide-react";
import { useEffect, useState } from "react";
import type { ReleaseCandidateSummary } from "./types";

export interface ReleaseRouteProps {
  candidate: ReleaseCandidateSummary | null;
  released: boolean;
  onOpenCityLocation?: (semanticLocation: "release_logistics") => void;
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return reduced;
}

export function ReleaseRoute({ candidate, released, onOpenCityLocation }: ReleaseRouteProps) {
  const reducedMotion = useReducedMotion();
  const [replay, setReplay] = useState(0);
  const stage = released ? 3 : candidate ? 2 : 1;
  return (
    <section className="release-panel release-route" aria-labelledby="release-route-title">
      <header>
        <div><span>EVENT-DRIVEN HANDOFF</span><h2 id="release-route-title">Project package route</h2><p>{reducedMotion ? "Reduced motion is active; replay uses a direct state highlight." : "A restrained local replay mirrors the recorded candidate handoff."}</p></div>
        <div>
          <button type="button" disabled={!candidate} onClick={() => setReplay((value) => value + 1)} title={!candidate ? "No release candidate event is available to replay." : undefined}>{replay ? <RotateCcw aria-hidden="true" /> : <Play aria-hidden="true" />}Replay release route</button>
          <button type="button" disabled={!onOpenCityLocation} onClick={() => onOpenCityLocation?.("release_logistics")} title={!onOpenCityLocation ? "City navigation is not connected in this host." : undefined}><MapPin aria-hidden="true" />Open Release Logistics</button>
        </div>
      </header>
      <div className={`release-route-track stage-${stage} ${replay ? "is-replaying" : ""} ${reducedMotion ? "reduced-motion" : ""}`} key={`${replay}-${stage}`}>
        <article className="complete"><Building2 aria-hidden="true" /><span><b>Project Core</b><small>Revision baseline</small></span></article>
        <i aria-hidden="true" />
        <article className={candidate ? "complete" : "current"}><ShieldCheck aria-hidden="true" /><span><b>QA & approvals</b><small>Event-backed gates</small></span></article>
        <i aria-hidden="true" />
        <article className={released ? "complete current" : candidate ? "current" : ""}><Warehouse aria-hidden="true" /><span><b>Release Logistics</b><small>{released ? "Ready for handoff" : candidate ? `Candidate ${candidate.number}` : "Awaiting candidate"}</small></span></article>
        {candidate && <span className="release-route-package" aria-label={`Release candidate ${candidate.number} package`}><PackageCheck aria-hidden="true" /></span>}
      </div>
    </section>
  );
}
