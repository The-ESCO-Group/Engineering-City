#[tauri::command]
fn backend_health_url(base_url: String) -> Result<String, String> {
    let url = base_url.trim().trim_end_matches('/');
    if !(url.starts_with("http://127.0.0.1") || url.starts_with("http://localhost")) {
        return Err("Only an explicitly configured local backend is allowed.".into());
    }
    Ok(format!("{url}/health"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![backend_health_url])
        .run(tauri::generate_context!())
        .expect("error while running ESCO Engineering City");
}

#[cfg(test)]
mod tests {
    use super::backend_health_url;
    #[test]
    fn rejects_non_local_backend() {
        assert!(backend_health_url("https://example.com".into()).is_err());
    }
}

