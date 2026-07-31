import {
  CircleAlert,
  CloudOff,
  FolderKanban,
  LogIn,
  LogOut,
  RefreshCw,
  Server,
  ShieldCheck,
  UserRound,
  Wifi,
  X
} from "lucide-react";
import { useState, type FormEvent } from "react";
import { CONTRACT_VERSION, PRODUCT_VERSION } from "../config";
import {
  useEngineeringData,
  type ConnectionState
} from "../api/live";
import "./ConnectionCenter.css";

interface ConnectionCenterProps {
  onClose?: () => void;
}

interface StatePresentation {
  label: string;
  summary: string;
  tone: "ok" | "attention" | "muted" | "danger";
}

const STATE_PRESENTATION: Record<ConnectionState, StatePresentation> = {
  demo: {
    label: "Demo mode",
    summary: "Synthetic product data is active. No local backend session is being used.",
    tone: "muted"
  },
  connecting: {
    label: "Connecting",
    summary: "Checking the configured local backend and existing session.",
    tone: "attention"
  },
  authentication_required: {
    label: "Authentication required",
    summary: "The local backend is available. Sign in to access authorized projects.",
    tone: "attention"
  },
  live: {
    label: "Connected",
    summary: "The local backend and authenticated project data are available.",
    tone: "ok"
  },
  reconnecting: {
    label: "Reconnecting",
    summary: "The live event link was interrupted. REST data remains visible while it reconnects.",
    tone: "attention"
  },
  offline: {
    label: "Backend offline",
    summary: "The configured local backend cannot be reached. No cloud fallback was attempted.",
    tone: "danger"
  },
  incompatible: {
    label: "Contract mismatch",
    summary: "The backend is reachable, but its contract is not compatible with this client.",
    tone: "danger"
  }
};

function humanize(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function ConnectionCenter({ onClose }: ConnectionCenterProps) {
  const engineering = useEngineeringData();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [actionError, setActionError] = useState("");
  const state = STATE_PRESENTATION[engineering.connection];
  const user = engineering.currentUser?.user ?? null;
  const selectedProject = engineering.projects.find(
    (project) => project.id === engineering.projectId
  );
  const loginAvailable =
    engineering.connection === "authentication_required" &&
    engineering.backendAvailable &&
    engineering.contractCompatible;
  const refreshAvailable = engineering.connection !== "demo";
  const stateDescription =
    engineering.connection === "demo" ||
    engineering.connection === "authentication_required" ||
    engineering.connection === "offline" ||
    engineering.connection === "incompatible" ||
    (engineering.connection === "live" && !engineering.projectId)
      ? engineering.fallbackReason || state.summary
      : state.summary;

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setActionError("");
    try {
      await engineering.login(email.trim(), password);
      setPassword("");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Local sign-in failed.");
    }
  }

  async function handleLogout() {
    setActionError("");
    try {
      await engineering.logout();
      setPassword("");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Local sign-out failed.");
    }
  }

  async function handleRefresh() {
    if (!refreshAvailable) return;
    setActionError("");
    setRefreshing(true);
    try {
      await engineering.refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Local status refresh failed.");
    } finally {
      setRefreshing(false);
    }
  }

  const visibleError = actionError || engineering.authError;

  return (
    <section className="connection-center" aria-labelledby="connection-center-title">
      <header className="connection-center__header">
        <div>
          <span className="connection-center__eyebrow">LOCAL OPERATIONS</span>
          <h2 id="connection-center-title">Connection &amp; account</h2>
        </div>
        <div className="connection-center__header-actions">
          <button
            className="connection-center__icon-button"
            type="button"
            onClick={() => void handleRefresh()}
            disabled={!refreshAvailable || refreshing}
            aria-label={refreshing ? "Refreshing local backend status" : "Refresh local backend status"}
          >
            <RefreshCw className={refreshing ? "is-spinning" : ""} aria-hidden="true" />
          </button>
          {onClose ? (
            <button
              className="connection-center__icon-button"
              type="button"
              onClick={onClose}
              aria-label="Close connection and account center"
            >
              <X aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </header>

      <div
        className={`connection-center__state is-${state.tone}`}
        role="status"
        aria-live="polite"
      >
        <span className="connection-center__state-icon" aria-hidden="true">
          {engineering.connection === "live" ? (
            <Wifi />
          ) : engineering.connection === "offline" || engineering.connection === "demo" ? (
            <CloudOff />
          ) : engineering.connection === "incompatible" ? (
            <CircleAlert />
          ) : (
            <Server />
          )}
        </span>
        <div>
          <strong>{state.label}</strong>
          <p>{stateDescription}</p>
        </div>
      </div>

      {visibleError ? (
        <p className="connection-center__error" role="alert">
          <CircleAlert aria-hidden="true" />
          <span>{visibleError}</span>
        </p>
      ) : null}

      <div className="connection-center__section" aria-labelledby="connection-account-heading">
        <div className="connection-center__section-heading">
          <UserRound aria-hidden="true" />
          <h3 id="connection-account-heading">Local account</h3>
        </div>

        {user ? (
          <div className="connection-center__identity">
            <div className="connection-center__avatar" aria-hidden="true">
              {user.display_name
                .split(/\s+/)
                .map((part) => part[0])
                .join("")
                .slice(0, 2)
                .toUpperCase() || "EC"}
            </div>
            <div className="connection-center__identity-copy">
              <strong>{user.display_name}</strong>
              <span>{user.email}</span>
              <div className="connection-center__roles" aria-label="Account roles">
                {user.roles.map((role) => (
                  <span key={role}>{humanize(role)}</span>
                ))}
              </div>
            </div>
            <button
              className="connection-center__secondary-button"
              type="button"
              onClick={() => void handleLogout()}
              disabled={engineering.authBusy || !engineering.backendAvailable}
            >
              <LogOut aria-hidden="true" />
              Sign out
            </button>
          </div>
        ) : (
          <form className="connection-center__login" onSubmit={handleLogin}>
            <label htmlFor="connection-center-email">Email</label>
            <input
              id="connection-center-email"
              name="email"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="username"
              inputMode="email"
              required
              disabled={!loginAvailable || engineering.authBusy}
            />
            <label htmlFor="connection-center-password">Password</label>
            <input
              id="connection-center-password"
              name="password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              required
              disabled={!loginAvailable || engineering.authBusy}
            />
            <button
              className="connection-center__primary-button"
              type="submit"
              disabled={!loginAvailable || engineering.authBusy || !email.trim() || !password}
            >
              <LogIn aria-hidden="true" />
              {engineering.authBusy ? "Signing in…" : "Sign in to local backend"}
            </button>
            {!loginAvailable ? (
              <p className="connection-center__hint">
                {engineering.connection === "demo"
                  ? "Demo mode was explicitly configured; restart without demo mode to use a local account."
                  : engineering.connection === "incompatible"
                    ? "Sign-in is disabled until the client and backend contract versions match."
                    : engineering.connection === "offline"
                      ? "Start or reconnect the local backend before signing in."
                      : "Waiting for the local backend status check to finish."}
              </p>
            ) : null}
          </form>
        )}
      </div>

      <div className="connection-center__section" aria-labelledby="connection-project-heading">
        <div className="connection-center__section-heading">
          <FolderKanban aria-hidden="true" />
          <h3 id="connection-project-heading">Authorized project</h3>
        </div>
        <label className="connection-center__select-label" htmlFor="connection-center-project">
          Project workspace
        </label>
        <select
          id="connection-center-project"
          value={engineering.projectId ?? ""}
          onChange={(event) => engineering.selectProject(event.target.value)}
          disabled={!user || engineering.projects.length === 0}
        >
          <option value="">
            {user ? "Select a project" : "Sign in to view authorized projects"}
          </option>
          {engineering.projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}{project.client_reference ? ` · ${project.client_reference}` : ""}
            </option>
          ))}
        </select>
        <p className="connection-center__hint">
          {selectedProject
            ? `${humanize(selectedProject.status)} · Updated ${new Date(selectedProject.updated_at).toLocaleString()}`
            : user && engineering.projects.length === 0
              ? "This account has no accessible projects."
              : "Only projects authorized for the signed-in account are listed."}
        </p>
      </div>

      <div className="connection-center__section" aria-labelledby="connection-version-heading">
        <div className="connection-center__section-heading">
          <ShieldCheck aria-hidden="true" />
          <h3 id="connection-version-heading">Compatibility</h3>
        </div>
        <dl className="connection-center__versions">
          <div>
            <dt>Product</dt>
            <dd>{PRODUCT_VERSION}</dd>
          </div>
          <div>
            <dt>Client contract</dt>
            <dd>{CONTRACT_VERSION}</dd>
          </div>
          <div>
            <dt>Backend</dt>
            <dd>{engineering.health?.version ?? "Unavailable"}</dd>
          </div>
          <div>
            <dt>Backend contract</dt>
            <dd>{engineering.health?.contract_version ?? "Unavailable"}</dd>
          </div>
        </dl>
        <p className={`connection-center__compatibility ${engineering.contractCompatible ? "is-compatible" : "is-incompatible"}`}>
          {engineering.health
            ? engineering.contractCompatible
              ? "Client and backend contracts match."
              : "Client and backend contracts do not match. Engineering actions are unavailable."
            : "Backend compatibility has not been established."}
        </p>
      </div>

      <div className="connection-center__section" aria-labelledby="connection-capability-heading">
        <div className="connection-center__section-heading">
          <Server aria-hidden="true" />
          <h3 id="connection-capability-heading">Installed capabilities</h3>
        </div>
        {engineering.capabilities.length > 0 ? (
          <ul className="connection-center__capabilities">
            {engineering.capabilities.map((capability) => (
              <li key={capability.name}>
                <span
                  className={`connection-center__capability-state ${capability.available ? "is-available" : "is-unavailable"}`}
                >
                  {capability.available ? "Available" : "Unavailable"}
                </span>
                <div>
                  <strong>{humanize(capability.name)}</strong>
                  <span>
                    {[capability.provider, capability.version].filter(Boolean).join(" · ") ||
                      capability.detail ||
                      "No provider details were reported."}
                  </span>
                  {capability.detail && (capability.provider || capability.version) ? (
                    <small>{capability.detail}</small>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="connection-center__empty">
            {engineering.backendAvailable
              ? "The backend reported no optional capabilities."
              : "Capabilities are unavailable until the local backend responds."}
          </p>
        )}
      </div>
    </section>
  );
}

export default ConnectionCenter;
