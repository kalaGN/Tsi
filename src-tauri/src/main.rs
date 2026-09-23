#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const READY_PREFIX: &str = "TSI_READY:";

struct BackendProcess(Mutex<Option<CommandChild>>);

fn show_startup_error(window: &tauri::WebviewWindow) {
    let _ = window.eval(
        "document.getElementById('status').textContent = '本地服务启动失败，请重新打开应用。';",
    );
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let token = uuid::Uuid::new_v4().simple().to_string();
            let allowed_port = Arc::new(Mutex::new(None::<u16>));
            let navigation_port = Arc::clone(&allowed_port);
            // 只在本地服务的主文档初始化时提供令牌，避免出现在 URL 或持久化状态。
            let init_script = format!(
                "if (location.protocol === 'http:' && location.hostname === '127.0.0.1') {{ Object.defineProperty(window, '__TSI_DESKTOP_TOKEN__', {{ value: {:?}, writable: false }}); }}",
                token,
            );
            let window = WebviewWindowBuilder::new(
                app,
                "main",
                WebviewUrl::App("index.html".into()),
            )
            .title("Tsi 助手")
            .inner_size(1200.0, 800.0)
            .min_inner_size(760.0, 560.0)
            .initialization_script(init_script)
            .on_navigation(move |url| {
                if url.scheme() == "tauri" {
                    return true;
                }
                url.scheme() == "http"
                    && url.host_str() == Some("127.0.0.1")
                    && url.port() == *navigation_port.lock().unwrap()
            })
            .build()?;

            let command = app
                .shell()
                .sidecar("tsi-backend")?
                .env("TSI_DESKTOP_TOKEN", token);
            let (mut events, child) = command.spawn()?;
            app.manage(BackendProcess(Mutex::new(Some(child))));
            tauri::async_runtime::spawn(async move {
                let startup = tokio::time::timeout(Duration::from_secs(30), async {
                    let mut stdout_buffer = Vec::new();
                    while let Some(event) = events.recv().await {
                        match event {
                            CommandEvent::Stdout(bytes) => {
                                stdout_buffer.extend_from_slice(&bytes);
                                if stdout_buffer.len() > 4096 {
                                    return None;
                                }
                                while let Some(end) = stdout_buffer.iter().position(|byte| *byte == b'\n') {
                                    let line: Vec<u8> = stdout_buffer.drain(..=end).collect();
                                    if let Ok(line) = std::str::from_utf8(&line) {
                                        if let Some(value) = line.trim().strip_prefix(READY_PREFIX) {
                                            if let Ok(port) = value.parse::<u16>() {
                                                if port != 0 {
                                                    return Some(port);
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            CommandEvent::Terminated(_) | CommandEvent::Error(_) => return None,
                            _ => {}
                        }
                    }
                    None
                })
                .await;
                let Ok(Some(port)) = startup else {
                    show_startup_error(&window);
                    return;
                };
                *allowed_port.lock().unwrap() = Some(port);
                let url = format!("http://127.0.0.1:{port}/ui#/chat");
                if let Ok(url) = tauri::Url::parse(&url) {
                    if window.navigate(url).is_err() {
                        show_startup_error(&window);
                    }
                } else {
                    show_startup_error(&window);
                }
                // 持续消费子进程输出，避免输出管道填满后阻塞 FastAPI。
                while let Some(event) = events.recv().await {
                    if matches!(event, CommandEvent::Terminated(_) | CommandEvent::Error(_)) {
                        let _ = window.eval(
                            "document.body.textContent = '本地服务已停止，请重新打开应用。';",
                        );
                        break;
                    }
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "main" && matches!(event, tauri::WindowEvent::Destroyed) {
                window.app_handle().exit(0);
            }
        })
        .build(tauri::generate_context!())
        .expect("无法创建 Tsi 桌面应用")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                if let Ok(mut child) = app.state::<BackendProcess>().0.lock() {
                    if let Some(mut process) = child.take() {
                        // PyInstaller onefile 会另起工作进程；通知它正常收尾。
                        // 宿主退出也会关闭 stdin，后端将据此停止服务。
                        let _ = process.write(b"shutdown\n");
                    }
                }
            }
        });
}
