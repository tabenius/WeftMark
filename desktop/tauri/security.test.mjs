import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const cfg = JSON.parse(readFileSync("desktop/tauri/src-tauri/tauri.conf.json", "utf8"));
const cap = JSON.parse(readFileSync("desktop/tauri/src-tauri/capabilities/default.json", "utf8"));
test("desktop reviewer is not a privileged host", () => {
  assert.equal(cfg.bundle.active, false);
  assert.deepEqual(cap.permissions, []);
  assert.equal(cfg.app.security.csp.includes("object-src 'none'"), true);
  assert.equal(cfg.app.security.csp.includes("script-src 'self'"), true);
  assert.equal(cfg.build.frontendDist, "../../../web/review");
});
