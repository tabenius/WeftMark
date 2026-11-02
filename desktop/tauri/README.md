# WeftMark read-only Tauri shell (experimental)

This packages the existing static `web/review` client without granting
filesystem, shell, HTTP plugin, process-spawning or other privileged Tauri
commands. The renderer may import local JSON snapshots using its existing
browser file-input controls. No WeftMark ledger write or approval authority is
exposed.

The Tauri configuration intentionally has `bundle.active=false` until a
verified desktop build pipeline and signing policy are established. This is a
scaffold, not an installer or verified application build. Before enabling a
sidecar, introduce authenticated length-bounded IPC, a documented process
sandbox for each OS, and adversarial tests. Do not put unrestricted capabilities
in the default permission set.

The shared review source currently defaults to `/v0/kanban` in browser mode;
for offline desktop review use **Open projection** to load a saved JSON snapshot.
