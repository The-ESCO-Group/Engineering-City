use serde::{Deserialize, Serialize};
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};
use tauri::{AppHandle, Manager};
use tauri_plugin_notification::NotificationExt;
use url::{Host, Url};

#[cfg(target_os = "windows")]
use std::path::{Component, Prefix};

const ALLOWED_ARTIFACT_EXTENSIONS: &[&str] = &[
    "csv", "docx", "html", "json", "l5x", "pdf", "txt", "xlsx", "xml", "zip",
];

#[derive(Debug, Serialize)]
struct DesktopCapabilities {
    local_backend_only: bool,
    backend_base_url: String,
    project_directory_policy: &'static str,
    allowed_artifact_extensions: &'static [&'static str],
    live_equipment_control: bool,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum OperatorEventKind {
    ApprovalRequired,
    WorkflowFailure,
}

fn parse_loopback_url(raw: &str) -> Result<Url, String> {
    let trimmed = raw.trim();
    let url = Url::parse(trimmed).map_err(|_| "The backend URL is not a valid absolute URL.")?;
    if url.scheme() != "http" {
        return Err("The local backend URL must use http://.".into());
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err("Credentials are not allowed in the backend URL.".into());
    }
    if url.query().is_some() || url.fragment().is_some() {
        return Err("Query strings and fragments are not allowed in the backend base URL.".into());
    }

    let local = match url.host() {
        Some(Host::Domain(name)) => name.eq_ignore_ascii_case("localhost"),
        Some(Host::Ipv4(address)) => address.is_loopback(),
        Some(Host::Ipv6(address)) => address.is_loopback(),
        None => false,
    };
    if !local {
        return Err("Only a structurally valid loopback backend is allowed.".into());
    }
    Ok(url)
}

fn normalize_backend_base_url(raw: &str) -> Result<String, String> {
    let mut url = parse_loopback_url(raw)?;
    let trimmed_path = url.path().trim_end_matches('/').to_owned();
    url.set_path(&trimmed_path);
    Ok(url.to_string().trim_end_matches('/').to_owned())
}

#[tauri::command]
fn backend_health_url(base_url: String) -> Result<String, String> {
    let mut url = parse_loopback_url(&base_url)?;
    let current = url.path().trim_end_matches('/');
    let base_path = if current.is_empty() || current == "/" {
        "/api"
    } else {
        current
    };
    if !base_path.ends_with("/health") {
        url.set_path(&format!("{base_path}/health"));
    }
    Ok(url.to_string())
}

fn environment_project_roots() -> Result<Vec<PathBuf>, String> {
    if let Some(value) = env::var_os("ESCO_PROJECTS_DIR") {
        let roots = env::split_paths(&value).collect::<Vec<_>>();
        if roots.is_empty() {
            return Err("ESCO_PROJECTS_DIR is present but empty.".into());
        }
        if roots.iter().any(|path| !is_supported_local_absolute(path)) {
            return Err("Every ESCO_PROJECTS_DIR entry must be an absolute local path; UNC and device namespaces are not allowed.".into());
        }
        return Ok(roots);
    }

    if let Some(value) = env::var_os("ESCO_DATA_DIR") {
        let data_dir = PathBuf::from(value);
        if !is_supported_local_absolute(&data_dir) {
            return Err(
                "ESCO_DATA_DIR must be an absolute local path for desktop directory authorization."
                    .into(),
            );
        }
        return Ok(vec![data_dir.join("projects")]);
    }
    Ok(Vec::new())
}

fn is_supported_local_absolute(path: &Path) -> bool {
    if !path.is_absolute() {
        return false;
    }
    #[cfg(target_os = "windows")]
    {
        matches!(
            path.components().next(),
            Some(Component::Prefix(prefix))
                if matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_))
        )
    }
    #[cfg(not(target_os = "windows"))]
    {
        true
    }
}

fn configured_project_roots(app: &AppHandle) -> Result<Vec<PathBuf>, String> {
    let configured = environment_project_roots()?;
    if !configured.is_empty() {
        return Ok(configured);
    }
    Ok(vec![app
        .path()
        .app_data_dir()
        .map_err(|error| format!("Unable to locate the app data directory: {error}"))?
        .join("projects")])
}

fn canonical_directory(path: &Path, label: &str) -> Result<PathBuf, String> {
    if !is_supported_local_absolute(path) {
        return Err(format!("{label} must be an absolute local path."));
    }
    let canonical = path
        .canonicalize()
        .map_err(|_| format!("{label} does not exist or cannot be accessed."))?;
    if !is_supported_local_absolute(&canonical) {
        return Err(format!(
            "{label} resolved outside the local filesystem boundary."
        ));
    }
    if !canonical.is_dir() {
        return Err(format!("{label} is not a directory."));
    }
    Ok(canonical)
}

fn validate_project_directory(path: &Path, roots: &[PathBuf]) -> Result<PathBuf, String> {
    let project = canonical_directory(path, "Project directory")?;
    for root in roots {
        let root = canonical_directory(root, "Authorized projects root")?;
        if project.parent() == Some(root.as_path()) {
            return Ok(project);
        }
    }
    Err("The selected directory is not a direct child of an authorized projects root.".into())
}

fn validate_artifact_file(path: &Path, roots: &[PathBuf]) -> Result<PathBuf, String> {
    if !is_supported_local_absolute(path) {
        return Err("The artifact must use an absolute local path.".into());
    }
    let artifact = path
        .canonicalize()
        .map_err(|_| "The artifact does not exist or cannot be accessed.".to_string())?;
    if !artifact.is_file() {
        return Err("The artifact path is not a file.".into());
    }
    let extension = artifact
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .ok_or_else(|| "The artifact has no supported file extension.".to_string())?;
    if !ALLOWED_ARTIFACT_EXTENSIONS.contains(&extension.as_str()) {
        return Err(format!(
            "Artifacts with .{extension} are not allowed to open from the desktop shell."
        ));
    }

    for root in roots {
        let root = canonical_directory(root, "Authorized projects root")?;
        if let Ok(relative) = artifact.strip_prefix(&root) {
            if relative.components().count() >= 2 {
                return Ok(artifact);
            }
        }
    }
    Err("The artifact is outside every authorized project directory.".into())
}

fn open_with_operating_system(path: &Path) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    let mut command = Command::new("explorer.exe");
    #[cfg(target_os = "macos")]
    let mut command = Command::new("open");
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut command = Command::new("xdg-open");

    command.arg(path).spawn().map_err(|error| {
        format!("The operating system could not open the authorized path: {error}")
    })?;
    Ok(())
}

#[tauri::command]
fn open_authorized_project_directory(app: AppHandle, path: String) -> Result<(), String> {
    let roots = configured_project_roots(&app)?;
    let project = validate_project_directory(Path::new(path.trim()), &roots)?;
    log::info!("opening an authorized project directory");
    open_with_operating_system(&project)
}

#[tauri::command]
fn open_authorized_artifact(app: AppHandle, path: String) -> Result<(), String> {
    let roots = configured_project_roots(&app)?;
    let artifact = validate_artifact_file(Path::new(path.trim()), &roots)?;
    log::info!("opening an authorized generated artifact");
    open_with_operating_system(&artifact)
}

#[tauri::command]
fn support_log_directory(app: AppHandle) -> Result<String, String> {
    let directory = app
        .path()
        .app_log_dir()
        .map_err(|error| format!("Unable to locate the support-log directory: {error}"))?;
    fs::create_dir_all(&directory)
        .map_err(|error| format!("Unable to create the support-log directory: {error}"))?;
    Ok(directory.to_string_lossy().into_owned())
}

fn validate_notification_text(title: &str, body: &str) -> Result<(), String> {
    if title.trim().is_empty() || title.chars().count() > 100 {
        return Err("Notification titles must contain 1 to 100 characters.".into());
    }
    if body.trim().is_empty() || body.chars().count() > 500 {
        return Err("Notification bodies must contain 1 to 500 characters.".into());
    }
    Ok(())
}

#[tauri::command]
fn notify_operator_event(
    app: AppHandle,
    kind: OperatorEventKind,
    title: String,
    body: String,
) -> Result<(), String> {
    validate_notification_text(&title, &body)?;
    let event_label = match kind {
        OperatorEventKind::ApprovalRequired => "Approval required",
        OperatorEventKind::WorkflowFailure => "Workflow failure",
    };
    app.notification()
        .builder()
        .title(format!("ESCO Engineering City · {event_label}"))
        .body(format!("{} — {}", title.trim(), body.trim()))
        .show()
        .map_err(|error| format!("Unable to show the local desktop notification: {error}"))
}

#[tauri::command]
fn desktop_capabilities() -> Result<DesktopCapabilities, String> {
    let configured =
        env::var("ESCO_BACKEND_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".to_string());
    Ok(DesktopCapabilities {
        local_backend_only: true,
        backend_base_url: normalize_backend_base_url(&configured)?,
        project_directory_policy: "direct_child_of_configured_projects_root",
        allowed_artifact_extensions: ALLOWED_ARTIFACT_EXTENSIONS,
        live_equipment_control: false,
    })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_log::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![
            backend_health_url,
            desktop_capabilities,
            notify_operator_event,
            open_authorized_artifact,
            open_authorized_project_directory,
            support_log_directory
        ])
        .run(tauri::generate_context!())
        .expect("error while running ESCO Engineering City");
}

#[cfg(test)]
mod tests {
    use super::{
        backend_health_url, is_supported_local_absolute, normalize_backend_base_url,
        validate_artifact_file, validate_notification_text, validate_project_directory,
    };
    use std::{
        fs,
        path::PathBuf,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn accepts_structural_loopback_backends() {
        assert_eq!(
            backend_health_url("http://127.0.0.1:8000".into()).unwrap(),
            "http://127.0.0.1:8000/api/health"
        );
        assert_eq!(
            backend_health_url("http://localhost:9000/api/".into()).unwrap(),
            "http://localhost:9000/api/health"
        );
        assert_eq!(
            backend_health_url("http://[::1]:8000/api".into()).unwrap(),
            "http://[::1]:8000/api/health"
        );
        assert_eq!(
            normalize_backend_base_url("http://127.0.0.2:8000/").unwrap(),
            "http://127.0.0.2:8000"
        );
    }

    #[test]
    fn rejects_url_lookalikes_credentials_and_remote_hosts() {
        for value in [
            "https://127.0.0.1:8000",
            "http://example.com",
            "http://localhost.example.com:8000",
            "http://127.0.0.1.example.com:8000",
            "http://127.0.0.1@evil.example:8000",
            "http://localhost:8000?next=https://example.com",
            "http://localhost:8000/#fragment",
        ] {
            assert!(
                backend_health_url(value.into()).is_err(),
                "accepted {value}"
            );
        }
    }

    #[test]
    fn authorizes_only_direct_project_children_and_safe_artifacts() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let sandbox = std::env::temp_dir().join(format!("esco-desktop-test-{unique}"));
        let root = sandbox.join("projects");
        let project = root.join("project-001");
        let report_dir = project.join("reports");
        let outside = sandbox.join("outside");
        fs::create_dir_all(&report_dir).unwrap();
        fs::create_dir_all(&outside).unwrap();
        let report = report_dir.join("summary.pdf");
        let executable = report_dir.join("unsafe.exe");
        fs::write(&report, b"synthetic pdf fixture").unwrap();
        fs::write(&executable, b"not executable").unwrap();

        let roots = vec![PathBuf::from(&root)];
        let expected_project = project.canonicalize().unwrap();
        let expected_report = report.canonicalize().unwrap();
        assert_eq!(
            validate_project_directory(&project, &roots).unwrap(),
            expected_project
        );
        assert!(validate_project_directory(&report_dir, &roots).is_err());
        assert!(validate_project_directory(&outside, &roots).is_err());
        assert_eq!(
            validate_artifact_file(&report, &roots).unwrap(),
            expected_report
        );
        assert!(validate_artifact_file(&executable, &roots).is_err());

        fs::remove_dir_all(&sandbox).unwrap();
    }

    #[test]
    fn bounds_operator_notification_content() {
        assert!(validate_notification_text("Approval", "A named reviewer is required.").is_ok());
        assert!(validate_notification_text("", "body").is_err());
        assert!(validate_notification_text("title", &"x".repeat(501)).is_err());
    }

    #[test]
    fn rejects_relative_and_windows_network_namespaces() {
        assert!(!is_supported_local_absolute(std::path::Path::new(
            "relative/project"
        )));
        #[cfg(target_os = "windows")]
        {
            assert!(!is_supported_local_absolute(std::path::Path::new(
                r"\\server\share\project"
            )));
            assert!(!is_supported_local_absolute(std::path::Path::new(
                r"\\.\PhysicalDrive0"
            )));
        }
    }
}
