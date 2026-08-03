export interface DesktopCapabilities {
  local_backend_only: boolean;
  backend_base_url: string;
  project_directory_policy: string;
  allowed_artifact_extensions: string[];
  live_equipment_control: boolean;
}

export type OperatorEventKind = "approval_required" | "workflow_failure";

type TauriCore = typeof import("@tauri-apps/api/core");

async function tauriCore(): Promise<TauriCore | null> {
  const core = await import("@tauri-apps/api/core");
  return core.isTauri() ? core : null;
}

function actionUnavailable(action: string): Error {
  return new Error(`${action} is available only in the installed ESCO desktop application.`);
}

export async function loadDesktopCapabilities(): Promise<DesktopCapabilities | null> {
  const core = await tauriCore();
  return core ? core.invoke<DesktopCapabilities>("desktop_capabilities") : null;
}

export async function openAuthorizedProjectDirectory(path: string): Promise<void> {
  const core = await tauriCore();
  if (!core) throw actionUnavailable("Opening an authorized project directory");
  await core.invoke("open_authorized_project_directory", { path });
}

export async function openAuthorizedArtifact(path: string): Promise<void> {
  const core = await tauriCore();
  if (!core) throw actionUnavailable("Opening an engineering artifact");
  await core.invoke("open_authorized_artifact", { path });
}

export async function loadSupportLogDirectory(): Promise<string> {
  const core = await tauriCore();
  if (!core) throw actionUnavailable("Locating desktop support logs");
  return core.invoke<string>("support_log_directory");
}

export async function notifyOperatorEvent(
  kind: OperatorEventKind,
  title: string,
  body: string
): Promise<boolean> {
  const core = await tauriCore();
  if (!core) return false;
  await core.invoke("notify_operator_event", { kind, title, body });
  return true;
}
