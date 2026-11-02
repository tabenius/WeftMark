fn main() {
    use std::path::Path;
    let icon = Path::new("icons/icon.png");
    if !icon.exists() {
        std::fs::create_dir_all("icons").expect("create Tauri icons directory");
        std::fs::copy("../../../assets/generated/weftmark-512.png", icon)
            .expect("copy tracked WeftMark logo for Tauri build");
    }
    tauri_build::build();
}
