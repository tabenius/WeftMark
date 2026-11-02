#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // No shell, sidecar execution, filesystem API, or privileged IPC is
    // exposed to the renderer. Add capabilities only after threat-model tests.
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("WeftMark desktop startup failed");
}
