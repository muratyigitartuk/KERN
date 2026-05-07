#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    env,
    fs::{self, OpenOptions},
    io::Write,
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use url::Url;

#[derive(Clone)]
struct KernDesktopState {
    child: Arc<Mutex<Option<Child>>>,
}

fn main() {
    let state = KernDesktopState {
        child: Arc::new(Mutex::new(None)),
    };
    let shutdown_state = state.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .manage(state.clone())
        .setup(move |app| {
            let handle = app.handle().clone();
            bootstrap_log("setup started");
            let port = free_local_port().map_err(|err| err.to_string())?;
            let runtime_root = resolve_runtime_root(&handle);
            let logs_root = desktop_logs_root(&handle);
            let data_root = desktop_data_root(&handle);
            fs::create_dir_all(&logs_root)?;
            fs::create_dir_all(&data_root)?;
            log_line(&logs_root, &format!("runtime_root={}", runtime_root.display()));
            log_line(&logs_root, &format!("data_root={}", data_root.display()));

            let child = start_kern_runtime(&runtime_root, &logs_root, &data_root, port)
                .map_err(|err| format!("failed to start KERN runtime: {err}"))?;
            log_line(&logs_root, &format!("runtime process started on port {port}"));
            *state.child.lock().expect("sidecar mutex poisoned") = Some(child);

            let runtime_url = format!("http://127.0.0.1:{port}");
            wait_until_ready(&runtime_url, Duration::from_secs(60))
                .map_err(|err| format!("KERN runtime did not become ready: {err}"))?;
            log_line(&logs_root, "runtime ready");

            let dashboard_url = Url::parse(&format!("{runtime_url}/dashboard"))?;
            let allowed_origin = format!("127.0.0.1:{port}");
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(dashboard_url))
                .title("KERN")
                .inner_size(1440.0, 960.0)
                .min_inner_size(1100.0, 720.0)
                .resizable(true)
                .maximizable(true)
                .on_navigation(move |url| {
                    url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(port)
                        || url.scheme() == "tauri"
                        || url.as_str().starts_with(&format!("http://{allowed_origin}/"))
                })
                .build()?;
            log_line(&logs_root, "main window opened");
            Ok(())
        })
        .on_window_event(move |_window, event| {
            if matches!(event, tauri::WindowEvent::Destroyed) {
                stop_kern_runtime(&shutdown_state);
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to run KERN desktop shell");
}

fn free_local_port() -> std::io::Result<u16> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    Ok(listener.local_addr()?.port())
}

fn resolve_runtime_root(app: &tauri::AppHandle) -> PathBuf {
    if let Some(path) = env::var_os("KERN_DESKTOP_RUNTIME_ROOT") {
        return PathBuf::from(path);
    }
    if cfg!(debug_assertions) {
        return PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("src-tauri has a parent")
            .to_path_buf();
    }
    app.path()
        .resource_dir()
        .map(|path| {
            for candidate in [
                path.join("desktop-runtime"),
                path.join("kern-runtime"),
                path.join("_up_").join("desktop-runtime"),
                path.join("_up_").join("kern-runtime"),
            ] {
                if candidate.exists() {
                    return candidate;
                }
            }
            path.join("desktop-runtime")
        })
        .unwrap_or_else(|_| PathBuf::from("desktop-runtime"))
}

fn bootstrap_log(message: &str) {
    if let Some(path) = env::var_os("KERN_DESKTOP_LOG_ROOT").map(PathBuf::from) {
        let _ = fs::create_dir_all(&path);
        log_line(&path, message);
    }
}

fn log_line(logs_root: &Path, message: &str) {
    let path = logs_root.join("kern-desktop-bootstrap.log");
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(file, "{message}");
    }
}

fn desktop_data_root(app: &tauri::AppHandle) -> PathBuf {
    env::var_os("KERN_DESKTOP_DATA_ROOT")
        .map(PathBuf::from)
        .or_else(|| app.path().app_data_dir().ok().map(|path| path.join("data")))
        .unwrap_or_else(|| PathBuf::from(".kern-desktop").join("data"))
}

fn desktop_logs_root(app: &tauri::AppHandle) -> PathBuf {
    env::var_os("KERN_DESKTOP_LOG_ROOT")
        .map(PathBuf::from)
        .or_else(|| app.path().app_log_dir().ok())
        .unwrap_or_else(|| PathBuf::from(".kern-desktop").join("logs"))
}

fn start_kern_runtime(
    runtime_root: &Path,
    logs_root: &Path,
    data_root: &Path,
    port: u16,
) -> std::io::Result<Child> {
    let python = resolve_python(runtime_root);
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs_root.join("kern-runtime.out.log"))?;
    let stderr = OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs_root.join("kern-runtime.err.log"))?;

    let profile_root = data_root.join("profiles");
    let backup_root = data_root.join("backups");
    let documents_root = data_root.join("documents");
    let attachments_root = data_root.join("attachments");
    let archives_root = data_root.join("archives");
    let meetings_root = data_root.join("meetings");
    let license_root = data_root.join("licenses");

    let mut command = Command::new(python);
    command
        .current_dir(runtime_root)
        .arg("-m")
        .arg("uvicorn")
        .arg("app.main:app")
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));

    for key in [
        "KERN_DB_PATH",
        "KERN_SYSTEM_DB_PATH",
        "KERN_ROOT_PATH",
        "KERN_PROFILE_ROOT",
        "KERN_BACKUP_ROOT",
        "KERN_DOCUMENT_ROOT",
        "KERN_ATTACHMENT_ROOT",
        "KERN_ARCHIVE_ROOT",
        "KERN_MEETING_ROOT",
        "KERN_LICENSE_ROOT",
        "KERN_LICENSE_PUBLIC_KEY",
        "KERN_LICENSE_PUBLIC_KEY_PATH",
        "KERN_SERVER_MODE",
        "KERN_POSTGRES_DSN",
        "KERN_REDIS_URL",
        "KERN_OBJECT_STORAGE_ROOT",
        "KERN_DISABLE_DOTENV",
    ] {
        command.env_remove(key);
    }

    command
        .env("KERN_DISABLE_DOTENV", "true")
        .env("KERN_DESKTOP_MODE", "true")
        .env("KERN_BIND_HOST", "127.0.0.1")
        .env("KERN_PORT", port.to_string())
        .env("KERN_PRODUCT_POSTURE", "production")
        .env("KERN_DISABLE_AUTH_FOR_LOOPBACK", "true")
        .env("KERN_ROOT_PATH", data_root)
        .env("KERN_SYSTEM_DB_PATH", data_root.join("kern-system.db"))
        .env("KERN_PROFILE_ROOT", profile_root)
        .env("KERN_BACKUP_ROOT", backup_root)
        .env("KERN_DOCUMENT_ROOT", documents_root)
        .env("KERN_ATTACHMENT_ROOT", attachments_root)
        .env("KERN_ARCHIVE_ROOT", archives_root)
        .env("KERN_MEETING_ROOT", meetings_root)
        .env("KERN_LICENSE_ROOT", license_root);

    for key in [
        "KERN_LLM_ENABLED",
        "KERN_LLM_LOCAL_ONLY",
        "KERN_LLAMA_SERVER_URL",
        "KERN_LLAMA_SERVER_MODEL_PATH",
        "KERN_LLAMA_SERVER_BINARY",
        "KERN_LLAMA_GPU_LAYERS",
        "KERN_LLM_MODEL",
    ] {
        if let Some(value) = env::var_os(key) {
            command.env(key, value);
        }
    }

    command.spawn()
}

fn resolve_python(runtime_root: &Path) -> PathBuf {
    if let Some(path) = env::var_os("KERN_DESKTOP_PYTHON") {
        return PathBuf::from(path);
    }
    let bundled = runtime_root.join(".venv").join("Scripts").join("python.exe");
    if bundled.exists() {
        return bundled;
    }
    PathBuf::from("python")
}

fn wait_until_ready(base_url: &str, timeout: Duration) -> Result<(), String> {
    let ready_url = format!("{base_url}/health/ready");
    let deadline = Instant::now() + timeout;
    loop {
        match ureq::get(&ready_url).timeout(Duration::from_secs(2)).call() {
            Ok(response) if response.status() == 200 => return Ok(()),
            Ok(response) => {
                if Instant::now() >= deadline {
                    return Err(format!("last readiness status was {}", response.status()));
                }
            }
            Err(err) => {
                if Instant::now() >= deadline {
                    return Err(err.to_string());
                }
            }
        }
        thread::sleep(Duration::from_millis(500));
    }
}

fn stop_kern_runtime(state: &KernDesktopState) {
    if let Some(mut child) = state.child.lock().expect("sidecar mutex poisoned").take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}
