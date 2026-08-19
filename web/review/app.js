const SUPPORTED_SCHEMA = "weftmark.kanban-projection.v0";
const LANES = ["backlog", "active", "review", "ready", "done"];
const ATTENTION_TONES = {
  failed_evidence: "danger",
  blocked: "danger",
  scope_collision: "warn",
  unavailable_evidence: "warn",
  evidence_incomplete: "warn",
  stale_review: "warn",
  stale_handoff: "warn",
  obsolete_evidence: "warn",
  dirty_worktree: "warn",
};

const elements = {
  board: document.querySelector("#board"),
  message: document.querySelector("#message"),
  sourceStatus: document.querySelector("#source-status"),
  refresh: document.querySelector("#refresh-button"),
  file: document.querySelector("#file-input"),
  countCards: document.querySelector("#count-cards"),
  countClaims: document.querySelector("#count-claims"),
  countAttention: document.querySelector("#count-attention"),
  generatedAt: document.querySelector("#generated-at"),
  dialog: document.querySelector("#detail-dialog"),
  detailLane: document.querySelector("#detail-lane"),
  detailTitle: document.querySelector("#detail-title"),
  detailContent: document.querySelector("#detail-content"),
  closeDetail: document.querySelector("#close-detail"),
};

const defaultSource = new URLSearchParams(location.search).get("source") || "/v0/kanban";
let source = { kind: "url", value: defaultSource };
let projection = null;

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(options)) {
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("data-")) node.setAttribute(key, value);
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
}

function humanize(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

function shortSha(value) {
  if (!value) return "—";
  return value.length > 12 ? value.slice(0, 12) : value;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function validateProjection(value) {
  if (!value || typeof value !== "object") throw new Error("Projection must be a JSON object.");
  if (value.schema !== SUPPORTED_SCHEMA) {
    throw new Error(`Unsupported projection schema: ${value.schema || "missing"}`);
  }
  if (!Array.isArray(value.cards)) throw new Error("Projection cards must be an array.");
  if (!value.counts || typeof value.counts !== "object") throw new Error("Projection counts are missing.");
  for (const card of value.cards) {
    if (!card || typeof card !== "object" || typeof card.id !== "string" || typeof card.title !== "string") {
      throw new Error("Projection contains a malformed card.");
    }
    if (!LANES.includes(card.lane)) throw new Error(`Unsupported board lane: ${card.lane}`);
  }
  return value;
}

function setMessage(message = "") {
  elements.message.hidden = !message;
  elements.message.textContent = message;
}

function attentionBadge(name) {
  return el("span", {
    className: "badge",
    text: humanize(name),
    "data-tone": ATTENTION_TONES[name] || "neutral",
  });
}

function statusBadge(readiness) {
  const tone = readiness === "ready" || readiness === "ready_with_follow_up" ? "ok"
    : readiness === "blocked" ? "danger"
      : readiness === "stale" || readiness === "evidence_incomplete" ? "warn" : "neutral";
  return el("span", { className: "badge", text: humanize(readiness), "data-tone": tone });
}

function renderCard(card) {
  const button = el("button", { className: "card", type: "button" });
  button.append(el("span", { className: "card-title", text: card.title }));

  const badges = el("span", { className: "badges" });
  badges.append(statusBadge(card.readiness));
  for (const attention of card.attention || []) badges.append(attentionBadge(attention));
  button.append(badges);

  const evidence = card.evidence || {};
  button.append(
    el("span", { className: "evidence-mini" }, [
      el("span", { text: `${evidence.current ?? 0} current` }),
      el("span", { text: `${evidence.failed ?? 0} failed` }),
      el("span", { text: `${evidence.obsolete ?? 0} stale` }),
    ]),
  );

  button.append(
    el("span", { className: "card-meta" }, [
      el("span", { text: card.git?.branch || "no branch" }),
      el("span", { className: "mono", text: shortSha(card.git?.head_sha) }),
    ]),
  );
  button.addEventListener("click", () => openDetail(card));
  return button;
}

function renderBoard() {
  elements.board.replaceChildren();
  const cards = projection.cards;
  const byLane = Object.fromEntries(LANES.map((lane) => [lane, []]));
  for (const card of cards) byLane[card.lane].push(card);

  for (const lane of LANES) {
    const laneCards = byLane[lane];
    const section = el("section", { className: "lane", "aria-labelledby": `lane-${lane}` });
    section.append(
      el("header", { className: "lane-header" }, [
        el("h2", { id: `lane-${lane}`, className: "lane-title", text: lane }),
        el("span", { className: "lane-count", text: String(laneCards.length) }),
      ]),
    );
    const list = el("div", { className: "cards" });
    if (laneCards.length === 0) list.append(el("p", { className: "empty-lane", text: "No Change Sets in this lane." }));
    for (const card of laneCards) list.append(renderCard(card));
    section.append(list);
    elements.board.append(section);
  }
}

function renderSummary() {
  const attentionCount = projection.cards.filter((card) => (card.attention || []).length > 0).length;
  elements.countCards.textContent = String(projection.counts.cards ?? projection.cards.length);
  elements.countClaims.textContent = String(projection.counts.active_claims ?? 0);
  elements.countAttention.textContent = String(attentionCount);
  elements.generatedAt.textContent = formatTime(projection.generated_at);
}

function definitionRows(rows) {
  const dl = el("dl", { className: "kv" });
  for (const [label, value, mono = false] of rows) {
    dl.append(el("dt", { text: label }));
    dl.append(el("dd", { className: mono ? "mono" : "", text: value == null || value === "" ? "—" : String(value) }));
  }
  return dl;
}

function detailSection(title, content) {
  return el("section", { className: "detail-section" }, [el("h3", { text: title }), content]);
}

function renderCollisions(card) {
  const collisions = card.scope_collisions || [];
  if (collisions.length === 0) return el("p", { text: "No active semantic-scope blockers." });
  const container = el("div");
  for (const collision of collisions) {
    const requested = collision.requested_scope || {};
    const owned = collision.owned_scope || {};
    container.append(
      el("div", { className: "collision" }, [
        definitionRows([
          ["Blocked by", collision.competing_change_set_id, true],
          ["Claim", collision.claim_id, true],
          ["Requested", `${requested.kind || "scope"}:${requested.key || "?"}`, true],
          ["Owned", `${owned.kind || "scope"}:${owned.key || "?"}`, true],
        ]),
      ]),
    );
  }
  return container;
}

function openDetail(card) {
  elements.detailLane.textContent = `${card.lane} · ${humanize(card.readiness)}`;
  elements.detailTitle.textContent = card.title;
  elements.detailContent.replaceChildren();

  elements.detailContent.append(
    detailSection("State", definitionRows([
      ["Change Set", card.id, true],
      ["Lifecycle", humanize(card.lifecycle_state)],
      ["Readiness", humanize(card.readiness)],
      ["Active claims", (card.claims?.active_ids || []).join(", ") || "none", true],
    ])),
  );
  elements.detailContent.append(detailSection("Scope blockers", renderCollisions(card)));

  const evidence = card.evidence || {};
  elements.detailContent.append(
    detailSection("Evidence", definitionRows([
      ["Total", evidence.total ?? 0],
      ["Current", evidence.current ?? 0],
      ["Failed", evidence.failed ?? 0],
      ["Unavailable", evidence.unavailable ?? 0],
      ["Obsolete", evidence.obsolete ?? 0],
    ])),
  );

  const review = card.review;
  elements.detailContent.append(
    detailSection("Review", review
      ? definitionRows([
        ["Decision", review.id, true],
        ["Outcome", humanize(review.outcome)],
        ["Head", shortSha(review.head_sha), true],
        ["Current", review.is_current ? "yes" : "no"],
      ])
      : el("p", { text: "No review decision recorded." })),
  );

  const handoff = card.handoff;
  elements.detailContent.append(
    detailSection("Handoff", handoff
      ? definitionRows([
        ["Handoff", handoff.id, true],
        ["Head", shortSha(handoff.head_sha), true],
        ["Current", handoff.is_current ? "yes" : "no"],
      ])
      : el("p", { text: "No handoff recorded." })),
  );

  elements.detailContent.append(
    detailSection("Git observation", definitionRows([
      ["Branch", card.git?.branch, true],
      ["Head", card.git?.head_sha, true],
      ["Observed", formatTime(card.git?.observed_at)],
      ["Dirty paths", (card.git?.dirty_paths || []).join(", ") || "none", true],
    ])),
  );

  if (!elements.dialog.open) elements.dialog.showModal();
}

function render() {
  renderSummary();
  renderBoard();
  setMessage();
}

async function loadFromUrl(url) {
  elements.sourceStatus.textContent = "Loading projection…";
  const response = await fetch(url, {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`Projection request failed (${response.status}).`);
  projection = validateProjection(await response.json());
  source = { kind: "url", value: url };
  elements.refresh.disabled = false;
  elements.sourceStatus.textContent = `Live · ${url}`;
  render();
}

async function loadFromFile(file) {
  const text = await file.text();
  projection = validateProjection(JSON.parse(text));
  source = { kind: "file", value: file.name };
  elements.refresh.disabled = true;
  elements.sourceStatus.textContent = `File · ${file.name}`;
  render();
}

async function loadCurrentSource() {
  try {
    if (source.kind === "url") await loadFromUrl(source.value);
  } catch (error) {
    elements.sourceStatus.textContent = "No live projection";
    setMessage(`${error.message} Use “Open projection” to inspect an exported JSON snapshot, or proxy /v0/kanban under this origin.`);
  }
}

elements.refresh.addEventListener("click", loadCurrentSource);
elements.file.addEventListener("change", async () => {
  const [file] = elements.file.files;
  if (!file) return;
  try {
    await loadFromFile(file);
  } catch (error) {
    setMessage(`Could not open ${file.name}: ${error.message}`);
  } finally {
    elements.file.value = "";
  }
});
elements.closeDetail.addEventListener("click", () => elements.dialog.close());
elements.dialog.addEventListener("click", (event) => {
  if (event.target === elements.dialog) elements.dialog.close();
});

loadCurrentSource();
