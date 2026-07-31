import {
  AlertTriangle,
  Check,
  CircleDashed,
  Clock3,
  Minus,
  ShieldCheck,
  X
} from "lucide-react";
import type {
  ReadinessState,
  ReleaseReadinessGate,
  ReleaseValidationSummary,
  ReleaseVerificationSummary
} from "./types";

export interface ReleaseReadinessProps {
  gates: ReleaseReadinessGate[];
  validation: ReleaseValidationSummary | null;
  verification: ReleaseVerificationSummary;
}

function StateIcon({ state }: { state: ReadinessState }) {
  if (state === "passed" || state === "ready") return <Check aria-hidden="true" />;
  if (state === "blocked") return <X aria-hidden="true" />;
  if (state === "pending") return <Clock3 aria-hidden="true" />;
  if (state === "unavailable") return <Minus aria-hidden="true" />;
  return <CircleDashed aria-hidden="true" />;
}

function stateLabel(state: ReadinessState): string {
  if (state === "passed") return "Passed";
  if (state === "ready") return "Ready";
  if (state === "pending") return "Decision pending";
  if (state === "blocked") return "Blocked";
  if (state === "unavailable") return "Unavailable";
  return "Not reported";
}

export function ReleaseReadiness({ gates, validation, verification }: ReleaseReadinessProps) {
  const passed = gates.filter((gate) => gate.state === "passed" || gate.state === "ready").length;
  const blocked = gates.filter((gate) => gate.state === "blocked").length;
  const unknown = gates.filter((gate) => gate.state === "unknown" || gate.state === "unavailable").length;
  return (
    <section className="release-panel release-readiness" aria-labelledby="release-readiness-title">
      <header className="release-section-heading">
        <div className="release-heading-icon"><ShieldCheck aria-hidden="true" /></div>
        <div>
          <span>GATE CONTROL</span>
          <h2 id="release-readiness-title">Release readiness</h2>
          <p>Only event-backed gate facts are marked passed. Missing facts remain visible.</p>
        </div>
        <dl className="release-readiness-counts">
          <div><dt>Passed</dt><dd>{passed}</dd></div>
          <div className={blocked ? "danger" : ""}><dt>Blocked</dt><dd>{blocked}</dd></div>
          <div><dt>Unreported</dt><dd>{unknown}</dd></div>
        </dl>
      </header>

      <div className="release-gate-grid">
        {gates.map((gate) => (
          <article key={gate.id} className={`release-gate release-state-${gate.state}`}>
            <span className="release-gate-state"><StateIcon state={gate.state} />{stateLabel(gate.state)}</span>
            <h3>{gate.label}</h3>
            <p>{gate.detail}</p>
            <footer>
              <span>{gate.source === "event" ? "Event stream" : gate.source === "derived" ? "Conservative derivation" : "Unavailable"}</span>
              {gate.unmetCriteria.length > 0 && <details><summary>{gate.unmetCriteria.length} unmet</summary><ul>{gate.unmetCriteria.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul></details>}
            </footer>
          </article>
        ))}
      </div>

      <div className="release-validation-strip">
        <article>
          <AlertTriangle aria-hidden="true" />
          <div><span>LATEST VALIDATION</span><b>{validation ? `${validation.checksRun} checks · revision ${validation.revision}` : "No completed validation event"}</b><small>{validation?.summary || "Run status cannot be inferred from an absent event."}</small></div>
          {validation && <strong className={validation.isBlocking ? "danger" : "ok"}>{validation.isBlocking ? "BLOCKING" : `${validation.errorCount} ERRORS`}</strong>}
        </article>
        <article>
          <ShieldCheck aria-hidden="true" />
          <div><span>IMPORT VERIFICATION</span><b>{verification.status.replaceAll("_", " ")}</b><small>{verification.detail}</small></div>
          <strong className={verification.status === "passed" ? "ok" : "neutral"}>{verification.candidateOnly ? "CANDIDATE ONLY" : "VERIFIED"}</strong>
        </article>
      </div>
    </section>
  );
}
