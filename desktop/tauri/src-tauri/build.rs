fn main() {
    use std::path::Path;
    let icon = Path::new("icons/icon.png");
    std::fs::create_dir_all("icons").expect("create Tauri icons directory");
    let source = image::open("../../../assets/generated/weftmark-512.png")
        .expect("read tracked WeftMark logo");
    source.to_rgba8().save(icon).expect("write RGBA Tauri icon");
    tauri_build::build();
}
