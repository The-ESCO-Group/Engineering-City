import {
  Bot,
  Check,
  Clipboard,
  KeyRound,
  Send,
  ShieldCheck,
  Sparkles,
  X
} from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import type {
  ExternalAgentDeployRequest,
  ExternalAgentDeployResponse
} from "../../api/types";

export interface ExternalAgentSetupProps {
  projectName: string;
  serverBaseUrl: string;
  onClose: () => void;
  onDeploy: (payload: ExternalAgentDeployRequest) => Promise<ExternalAgentDeployResponse>;
}

const PROVIDER_DEFAULTS = {
  openai: { name: "Orbit", model: "gpt-5.6-sol", script: "openai_worker.py" },
  anthropic: { name: "Beacon", model: "", script: "claude_worker.py" }
} as const;

function quotePowerShell(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

export function ExternalAgentSetup({
  projectName,
  serverBaseUrl,
  onClose,
  onDeploy
}: ExternalAgentSetupProps) {
  const [provider, setProvider] = useState<ExternalAgentDeployRequest["provider"]>("openai");
  const [displayName, setDisplayName] = useState<string>(PROVIDER_DEFAULTS.openai.name);
  const [model, setModel] = useState<string>(PROVIDER_DEFAULTS.openai.model);
  const [specialty, setSpecialty] = useState("External engineering reviewer");
  const [mission, setMission] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState<"token" | "command" | null>(null);
  const [deployment, setDeployment] = useState<ExternalAgentDeployResponse | null>(null);

  const command = useMemo(() => {
    if (!deployment) return "";
    const deployedProvider = deployment.provider === "anthropic" ? "anthropic" : "openai";
    const providerKey = deployedProvider === "openai" ? "OPENAI_API_KEY" : "ANTHROPIC_API_KEY";
    const modelKey = deployedProvider === "openai" ? "OPENAI_MODEL" : "ANTHROPIC_MODEL";
    const keyPlaceholder = deployedProvider === "openai"
      ? "<PASTE_OPENAI_API_KEY_IN_THIS_TERMINAL>"
      : "<PASTE_ANTHROPIC_API_KEY_IN_THIS_TERMINAL>";
    return [
      `$env:ESCO_BASE_URL = ${quotePowerShell(serverBaseUrl)}`,
      `$env:ESCO_AGENT_TOKEN = ${quotePowerShell(deployment.agent_token)}`,
      `$env:${providerKey} = ${quotePowerShell(keyPlaceholder)}`,
      `$env:${modelKey} = ${quotePowerShell(deployment.model)}`,
      `python .\\sdk\\python\\${PROVIDER_DEFAULTS[deployedProvider].script}`
    ].join("\n");
  }, [deployment, serverBaseUrl]);

  function chooseProvider(nextProvider: ExternalAgentDeployRequest["provider"]) {
    const currentDefault = PROVIDER_DEFAULTS[provider];
    const nextDefault = PROVIDER_DEFAULTS[nextProvider];
    setProvider(nextProvider);
    if (displayName === currentDefault.name) setDisplayName(nextDefault.name);
    setModel(nextDefault.model);
    setError("");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!displayName.trim() || !model.trim() || !mission.trim() || !specialty.trim()) {
      setError("Agent name, model, role, and mission are required.");
      return;
    }
    if (!acknowledged) {
      setError("Confirm external-provider processing before sending the agent.");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const response = await onDeploy({
        provider,
        display_name: displayName.trim(),
        specialty: specialty.trim(),
        mission: mission.trim(),
        model: model.trim(),
        initial_location: "project_intake",
        ttl_seconds: 3600,
        acknowledge_external_processing: true
      });
      setDeployment(response);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The agent could not be deployed.");
    } finally {
      setSubmitting(false);
    }
  }

  async function copyValue(value: string, kind: "token" | "command") {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(kind);
      window.setTimeout(() => setCopied(null), 1800);
    } catch {
      setError("Clipboard access is unavailable. Select and copy the value manually.");
    }
  }

  return (
    <div className="ops-agent-setup-backdrop">
      <section
        className="ops-agent-setup"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ops-agent-setup-title"
      >
        <header>
          <div className="ops-agent-setup__mark"><Bot aria-hidden="true" /></div>
          <div>
            <span>EXTERNAL SDK AGENT</span>
            <h3 id="ops-agent-setup-title">
              {deployment ? "Agent ready to launch" : "Send an agent into the city"}
            </h3>
            <p>{projectName}</p>
          </div>
          <button type="button" className="ops-agent-setup__close" onClick={onClose} aria-label="Close agent setup">
            <X aria-hidden="true" />
          </button>
        </header>

        {deployment ? (
          <div className="ops-agent-launch">
            <div className="ops-agent-launch__success" role="status">
              <Check aria-hidden="true" />
              <div>
                <strong>{deployment.instance.display_name} is now visible in the city.</strong>
                <p>Its one-hour mission credential expires {new Date(deployment.token_expires_at).toLocaleString()}.</p>
              </div>
            </div>

            <div className="ops-agent-launch__warning">
              <KeyRound aria-hidden="true" />
              <p><strong>Copy this token now.</strong> ESCO does not store it for later display. Closing this setup clears it from the screen.</p>
            </div>

            <label className="ops-agent-token">
              <span>One-time agent token</span>
              <textarea value={deployment.agent_token} readOnly rows={3} />
            </label>
            <button type="button" className="ops-copy-button" onClick={() => void copyValue(deployment.agent_token, "token")}>
              {copied === "token" ? <Check aria-hidden="true" /> : <Clipboard aria-hidden="true" />}
              {copied === "token" ? "Token copied" : "Copy token"}
            </button>

            <div className="ops-agent-launch__command">
              <div>
                <strong>Starter command</strong>
                <span>Run from the ESCO repository root. Replace only the provider-key placeholder.</span>
              </div>
              <pre>{command}</pre>
              <button type="button" className="ops-copy-button" onClick={() => void copyValue(command, "command")}>
                {copied === "command" ? <Check aria-hidden="true" /> : <Clipboard aria-hidden="true" />}
                {copied === "command" ? "Command copied" : "Copy starter command"}
              </button>
            </div>

            <p className="ops-agent-launch__boundary">
              Your provider key stays in that worker terminal. The SDK receives bounded project context and can only submit evidence-gated proposals; it cannot approve or release work.
            </p>
            {error && <p className="ops-agent-setup__error" role="alert">{error}</p>}
            <footer><button type="button" className="ops-primary-action" onClick={onClose}>Done</button></footer>
          </div>
        ) : (
          <form onSubmit={(event) => void submit(event)}>
            <fieldset className="ops-provider-picker">
              <legend>1. Choose the SDK provider</legend>
              <button type="button" aria-pressed={provider === "openai"} onClick={() => chooseProvider("openai")}>
                <Sparkles aria-hidden="true" /><span><strong>OpenAI</strong><small>Agents SDK worker</small></span>
              </button>
              <button type="button" aria-pressed={provider === "anthropic"} onClick={() => chooseProvider("anthropic")}>
                <Bot aria-hidden="true" /><span><strong>Claude</strong><small>Anthropic SDK worker</small></span>
              </button>
            </fieldset>

            <div className="ops-agent-form-grid">
              <label>
                <span>2. Agent name</span>
                <input
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  maxLength={80}
                  autoFocus
                  required
                />
                <small>This unique callsign appears above the moving city agent.</small>
              </label>
              <label>
                <span>Model</span>
                <input
                  value={model}
                  onChange={(event) => setModel(event.target.value)}
                  maxLength={120}
                  placeholder={provider === "anthropic" ? "Model ID available to your Anthropic account" : "OpenAI model ID"}
                  required
                />
                <small>The provider worker must use this same model.</small>
              </label>
            </div>

            <label className="ops-agent-mission">
              <span>3. What should this agent do?</span>
              <textarea
                value={mission}
                onChange={(event) => setMission(event.target.value)}
                maxLength={4000}
                rows={5}
                placeholder="Example: Review the uploaded requirements and PLC evidence. Report cited conflicts, missing inputs, and questions that require a human answer."
                required
              />
              <small>{mission.length.toLocaleString()} / 4,000 characters</small>
            </label>

            <details className="ops-agent-advanced">
              <summary>Role settings</summary>
              <label>
                <span>Specialty</span>
                <input value={specialty} onChange={(event) => setSpecialty(event.target.value)} maxLength={120} required />
              </label>
              <p>The agent starts at Project Intake and receives a one-hour, single-mission credential.</p>
            </details>

            <label className="ops-agent-acknowledgement">
              <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
              <ShieldCheck aria-hidden="true" />
              <span>I understand this worker may send the granted project context to {provider === "openai" ? "OpenAI" : "Anthropic"}. Engineering approval remains human-controlled.</span>
            </label>

            <div className="ops-agent-setup__notice">
              <KeyRound aria-hidden="true" />
              <p>No provider API key is entered here. After deployment, ESCO shows one private launch command for your local SDK worker.</p>
            </div>
            {error && <p className="ops-agent-setup__error" role="alert">{error}</p>}
            <footer>
              <button type="button" onClick={onClose}>Cancel</button>
              <button type="submit" className="ops-primary-action" disabled={submitting || !acknowledged}>
                <Send aria-hidden="true" />{submitting ? "Sending agent…" : "Send agent"}
              </button>
            </footer>
          </form>
        )}
      </section>
    </div>
  );
}
