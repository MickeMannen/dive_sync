const $ = (id) => document.getElementById(id);

let currentSettings = null;
let fieldsInfo = null;           // GET /api/fields -> {pairs: [...]}
let credentialsAccounts = { garmin: [], divelogs: [] };  // usernames, for account-selector dropdowns

// ---------------------------------------------------------------- pages / sidebar nav

function selectPage(page) {
  document.querySelectorAll(".page").forEach((el) => { el.hidden = el.dataset.page !== page; });
  document.querySelectorAll(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.page === page));
  if (page === "mapping") requestAnimationFrame(drawLines);  // board was laid out while hidden (0-size rects)
  try { localStorage.setItem("dive_sync_page", page); } catch (e) { /* private mode etc. */ }
}

// ---------------------------------------------------------------- status, log, sync

async function loadVersion() {
  try {
    const res = await fetch("/api/version");
    const data = await res.json();
    $("version").textContent = `v${data.version}` + (data.update_available ? ` (update available: ${data.latest})` : "");
  } catch (e) {
    $("version").textContent = "";
  }
}

function summarizeJobResult(last) {
  if (!last) return "none yet";
  if (last.error) return `error: ${last.error}`;
  const parts = Object.entries(last)
    .filter(([k, v]) => (k.startsWith("uploaded_to_") || k.startsWith("updated_on_")) && Array.isArray(v))
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${v.length}`);
  if (last.conflicts && last.conflicts.length) parts.push(`conflicts: ${last.conflicts.length}`);
  return (last.dry_run ? "[dry run] " : "") + (parts.join(" · ") || "ok");
}

function renderJobResults(lastResults) {
  const body = $("job-results-body");
  const jobIds = Object.keys(lastResults || {});
  if (!jobIds.length) {
    body.innerHTML = `<tr><td colspan="2" class="muted">No sync has run yet.</td></tr>`;
    return;
  }
  body.innerHTML = jobIds
    .sort((a, b) => (a === "Manual" ? -1 : b === "Manual" ? 1 : a.localeCompare(b)))
    .map((id) => `<tr><td>${escapeHtml(id)}</td><td>${escapeHtml(summarizeJobResult(lastResults[id]))}</td></tr>`)
    .join("");
}

async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    $("status-running").textContent = data.is_running ? "yes" : "no";
    $("status-next").textContent = data.next_scheduled_run ? new Date(data.next_scheduled_run).toLocaleString() : "none";
    renderJobResults(data.last_results);
  } catch (e) {
    $("status-running").textContent = "?";
  }
}

async function triggerSync() {
  $("trigger-message").textContent = "Starting…";
  const payload = { dry_run: $("trigger-dry-run").checked };
  const garminAccount = $("trigger-garmin-account").value;
  const divelogsAccount = $("trigger-divelogs-account").value;
  if (garminAccount) payload.garmin_username = garminAccount;
  if (divelogsAccount) payload.divelogs_username = divelogsAccount;
  const res = await fetch("/api/sync/trigger", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  $("trigger-message").textContent = res.ok ? "Sync started." : (data.detail || "Failed to start.");
  setTimeout(loadStatus, 2000);
}

function appendLogLine(line) {
  const pre = $("log-tail");
  pre.textContent += line + "\n";
  const lines = pre.textContent.split("\n");
  if (lines.length > 400) pre.textContent = lines.slice(-400).join("\n");
  pre.scrollTop = pre.scrollHeight;
}

function connectLogStream() {
  const source = new EventSource("/api/logs/stream");
  source.onmessage = (event) => appendLogLine(event.data);
  source.onerror = () => {
    source.close();
    setTimeout(connectLogStream, 5000);
  };
}

// ---------------------------------------------------------------- credentials

function accountRowHtml(service, account) {
  const tokenDirField = service === "garmin"
    ? `<label>Token dir <input class="a-token-dir" type="text" value="${escapeHtml(account.token_dir || "tokens/garmin")}"></label>`
    : "";
  return `
    <label>Username <input class="a-username" type="text" value="${escapeHtml(account.username || "")}"></label>
    <label>Password <input class="a-password" type="password" placeholder="${account.username ? "unchanged" : ""}"></label>
    ${tokenDirField}
    <button type="button" class="secondary remove-row">✕</button>
  `;
}

function addAccountRow(service, account) {
  const row = document.createElement("div");
  row.className = "account-row";
  row.innerHTML = accountRowHtml(service, account || {});
  row.querySelector(".remove-row").addEventListener("click", () => row.remove());
  $(`${service}-accounts-rows`).appendChild(row);
}

function renderAccountRows(service, rows) {
  $(`${service}-accounts-rows`).innerHTML = "";
  (rows.length ? rows : [{}]).forEach((account) => addAccountRow(service, account));
}

function readAccountRows(service) {
  return Array.from($(`${service}-accounts-rows`).querySelectorAll(".account-row"))
    .map((row) => {
      const account = { username: row.querySelector(".a-username").value.trim(), password: row.querySelector(".a-password").value };
      const tokenDir = row.querySelector(".a-token-dir");
      if (tokenDir) account.token_dir = tokenDir.value.trim() || "tokens/garmin";
      return account;
    })
    .filter((account) => account.username);
}

function populateAccountSelect(id, usernames, includeBlank) {
  const select = $(id);
  const current = select.value;
  const options = [...(includeBlank ? [["", "Default account"]] : []), ...usernames.map((u) => [u, u])];
  select.innerHTML = optionList(options, current);
}

async function loadCredentialsStatus() {
  const res = await fetch("/api/credentials/status");
  const data = await res.json();
  setBadge("garmin-configured", data.garmin_configured);
  setBadge("divelogs-configured", data.divelogs_configured);
  setBadge("subsurface-configured", data.subsurface_configured);
  setBadge("submersion-configured", data.submersion_configured);
  renderAccountRows("garmin", data.garmin_account_rows || []);
  renderAccountRows("divelogs", data.divelogs_account_rows || []);
  credentialsAccounts = { garmin: data.garmin_accounts || [], divelogs: data.divelogs_accounts || [] };
  populateAccountSelect("trigger-garmin-account", credentialsAccounts.garmin, true);
  populateAccountSelect("trigger-divelogs-account", credentialsAccounts.divelogs, true);
  if (data.subsurface_email) $("subsurface-email").value = data.subsurface_email;
  const store = data.submersion_store || {};
  if (store.store_type) $("submersion-store-type").value = store.store_type;
  if (store.endpoint_url) $("submersion-endpoint-url").value = store.endpoint_url;
  if (store.region) $("submersion-region").value = store.region;
  if (store.bucket) $("submersion-bucket").value = store.bucket;
  if (store.prefix) $("submersion-prefix").value = store.prefix;
  $("submersion-path-style").checked = !!store.path_style;
  if (store.folder_path) $("submersion-folder-path").value = store.folder_path;
  updateSubmersionStoreFields();
}

function updateSubmersionStoreFields() {
  const isFolder = $("submersion-store-type").value === "folder";
  $("submersion-s3-fields").hidden = isFolder;
  $("submersion-folder-fields").hidden = !isFolder;
}

function setBadge(id, on) {
  const el = $(id);
  el.textContent = on ? "configured" : "not set";
  el.classList.toggle("on", !!on);
}

function credentialsPayload() {
  const payload = {
    garmin_accounts: readAccountRows("garmin"),
    divelogs_accounts: readAccountRows("divelogs"),
  };
  const email = $("subsurface-email").value.trim();
  const pw = $("subsurface-password").value;
  if (email && pw) payload.subsurface = { email, password: pw };

  const storeType = $("submersion-store-type").value;
  const submersion = {
    store_type: storeType,
    endpoint_url: $("submersion-endpoint-url").value.trim(),
    region: $("submersion-region").value.trim(),
    bucket: $("submersion-bucket").value.trim(),
    prefix: $("submersion-prefix").value.trim() || "submersion-sync/",
    access_key_id: $("submersion-access-key-id").value.trim(),
    secret_access_key: $("submersion-secret-access-key").value,
    path_style: $("submersion-path-style").checked,
    folder_path: $("submersion-folder-path").value.trim(),
    passphrase: $("submersion-passphrase").value,
  };
  const submersionSet = storeType === "folder"
    ? !!submersion.folder_path
    : !!(submersion.endpoint_url && submersion.bucket && submersion.access_key_id && submersion.secret_access_key);
  if (submersionSet) payload.submersion = submersion;
  return payload;
}

async function saveCredentials(event) {
  event.preventDefault();
  const res = await fetch("/api/credentials", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentialsPayload()),
  });
  const data = await res.json();
  $("credentials-message").textContent = res.ok ? "Credentials saved." : (data.detail || "Failed to save.");
  if (res.ok) loadCredentialsStatus();
}

async function testCredentials() {
  $("credentials-message").textContent = "Testing…";
  const res = await fetch("/api/credentials/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentialsPayload()),
  });
  const data = await res.json();
  const parts = [];
  const accountResults = (label, rows) => (rows || []).forEach((r) => parts.push(`${label} (${r.username}): ${r.ok ? "OK" : "failed"}`));
  accountResults("Garmin", data.garmin);
  accountResults("Divelogs", data.divelogs);
  if (data.subsurface !== undefined) parts.push(`Subsurface Cloud: ${data.subsurface ? "OK" : "failed"}`);
  if (data.submersion !== undefined) parts.push(`Submersion: ${data.submersion ? "OK" : (data.submersion_message || "failed")}`);
  $("credentials-message").textContent = parts.join(" · ") || "Enter a username and password to test.";
}

// ---------------------------------------------------------------- settings, pairs, schedule

function populateDefaults(settings) {
  $("default-directionality").value = settings.directionality;
  $("grace-window").value = settings.grace_window_minutes;
  $("api-cooldown").value = settings.api_cooldown_seconds;
  $("default-only-new").checked = settings.sync_filters.only_new;
  $("default-sync-gases").checked = settings.sync_filters.sync_gases;
  $("default-propagate-deletes").checked = !!settings.propagate_deletes;
  $("default-create-on-garmin").checked = !!settings.create_on_garmin;
  $("notify-url").value = settings.notify_url || "";
}

async function saveNotifyUrl() {
  $("notify-message").textContent = "Saving…";
  const { ok, message } = await postSettings(settingsPayload({ notify_url: $("notify-url").value.trim() }));
  $("notify-message").textContent = message;
  if (ok) currentSettings = await (await fetch("/api/settings")).json();
}

async function testNotifyUrl() {
  const url = $("notify-url").value.trim();
  if (!url) {
    $("notify-message").textContent = "Enter a webhook URL first.";
    return;
  }
  $("notify-message").textContent = "Sending test alert…";
  const res = await fetch("/api/notify/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notify_url: url }),
  });
  const data = await res.json();
  $("notify-message").textContent = data.detail || (res.ok ? "Sent." : "Failed to send.");
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function optionList(options, selected) {
  return options.map(([value, label]) => `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function pairIds() {
  return (currentSettings?.sync_pairs || []).map((p) => p.id);
}

function directionOptionsFor(sourceId, targetId) {
  return [["bidirectional", "Bidirectional"], [`to_${targetId}`, `To ${targetId}`], [`to_${sourceId}`, `To ${sourceId}`]];
}

function pairRowHtml(pair) {
  return `
    <td><input class="p-id" value="${escapeHtml(pair.id)}"></td>
    <td><input class="p-source" value="${escapeHtml(pair.source)}" placeholder="garmin"></td>
    <td><input class="p-target" value="${escapeHtml(pair.target)}" placeholder="uddf:export.uddf"></td>
    <td><input class="p-direction" value="${escapeHtml(pair.directionality)}" placeholder="bidirectional"></td>
    <td><input class="p-enabled" type="checkbox" ${pair.enabled ? "checked" : ""}></td>
    <td><button type="button" class="secondary remove-row">✕</button></td>
  `;
}

function addPairRow(pair) {
  const row = document.createElement("tr");
  const p = pair || { id: `pair-${Date.now()}`, source: "garmin", target: "uddf:export.uddf", directionality: "bidirectional", enabled: true };
  row.innerHTML = pairRowHtml(p);
  row.dataset.links = JSON.stringify(pair?.field_links ?? null);
  row.dataset.grace = pair?.grace_window_minutes ?? "";
  row.dataset.propagateDeletes = pair?.propagate_deletes ?? "";
  row.dataset.createOnGarmin = pair?.create_on_garmin ?? "";
  row.querySelector(".remove-row").addEventListener("click", () => row.remove());
  $("pairs-body").appendChild(row);
}

function readPairsTable() {
  return Array.from($("pairs-body").querySelectorAll("tr")).map((row) => {
    const links = JSON.parse(row.dataset.links || "null");
    const grace = row.dataset.grace;
    const propagateDeletes = row.dataset.propagateDeletes;
    const createOnGarmin = row.dataset.createOnGarmin;
    return {
      id: row.querySelector(".p-id").value.trim(),
      source: row.querySelector(".p-source").value.trim(),
      target: row.querySelector(".p-target").value.trim(),
      directionality: row.querySelector(".p-direction").value.trim() || "bidirectional",
      enabled: row.querySelector(".p-enabled").checked,
      grace_window_minutes: grace === "" || grace === "null" ? null : parseInt(grace, 10),
      field_links: links,
      propagate_deletes: propagateDeletes === "" || propagateDeletes === "null" ? null : propagateDeletes === "true",
      create_on_garmin: createOnGarmin === "" || createOnGarmin === "null" ? null : createOnGarmin === "true",
    };
  });
}

function cronRowHtml(job) {
  const pairOptions = [["", "Garmin ↔ Divelogs"], ...pairIds().map((id) => [id, id])];
  const garminOptions = [["", "Default account"], ...credentialsAccounts.garmin.map((u) => [u, u])];
  const divelogsOptions = [["", "Default account"], ...credentialsAccounts.divelogs.map((u) => [u, u])];
  return `
    <td><input class="f-id" value="${escapeHtml(job.id)}"></td>
    <td><select class="f-pair">${optionList(pairOptions, job.pair || "")}</select></td>
    <td><input class="f-direction" value="${escapeHtml(job.directionality)}"></td>
    <td>
      <select class="f-frequency">${optionList([["hourly", "Hourly"], ["daily", "Daily"], ["weekly", "Weekly"], ["custom_minutes", "Every N minutes"]], job.frequency)}</select>
    </td>
    <td><input class="f-hour" type="number" min="0" max="23" value="${job.hour}"></td>
    <td><input class="f-minute" type="number" min="0" max="59" value="${job.minute}"></td>
    <td><input class="f-day" type="number" min="0" max="6" value="${job.day_of_week}"></td>
    <td><input class="f-interval" type="number" min="1" value="${job.interval_minutes}"></td>
    <td><input class="f-only-new" type="checkbox" ${job.only_new ? "checked" : ""}></td>
    <td><input class="f-gases" type="checkbox" ${job.sync_gases ? "checked" : ""}></td>
    <td><input class="f-enabled" type="checkbox" ${job.enabled ? "checked" : ""}></td>
    <td><select class="f-garmin-account">${optionList(garminOptions, job.garmin_username || "")}</select></td>
    <td><select class="f-divelogs-account">${optionList(divelogsOptions, job.divelogs_username || "")}</select></td>
    <td><button type="button" class="secondary remove-row">✕</button></td>
  `;
}

function addCronRow(job) {
  const defaultJob = {
    id: `job-${Date.now()}`, pair: "", directionality: "bidirectional", frequency: "daily",
    hour: 0, minute: 0, day_of_week: 0, interval_minutes: 60, only_new: true, sync_gases: true, enabled: true,
    garmin_username: "", divelogs_username: "",
  };
  const row = document.createElement("tr");
  row.innerHTML = cronRowHtml(job || defaultJob);
  row.dataset.links = JSON.stringify(job?.field_links ?? null);
  row.querySelector(".remove-row").addEventListener("click", () => row.remove());
  $("cron-body").appendChild(row);
}

function populateCronTable(jobs) {
  $("cron-body").innerHTML = "";
  jobs.forEach(addCronRow);
}

function readCronTable() {
  return Array.from($("cron-body").querySelectorAll("tr")).map((row) => ({
    id: row.querySelector(".f-id").value,
    pair: row.querySelector(".f-pair").value || null,
    directionality: row.querySelector(".f-direction").value.trim() || "bidirectional",
    frequency: row.querySelector(".f-frequency").value,
    hour: parseInt(row.querySelector(".f-hour").value, 10),
    minute: parseInt(row.querySelector(".f-minute").value, 10),
    day_of_week: parseInt(row.querySelector(".f-day").value, 10),
    interval_minutes: parseInt(row.querySelector(".f-interval").value, 10),
    only_new: row.querySelector(".f-only-new").checked,
    sync_gases: row.querySelector(".f-gases").checked,
    enabled: row.querySelector(".f-enabled").checked,
    field_links: JSON.parse(row.dataset.links || "null"),
    garmin_username: row.querySelector(".f-garmin-account").value || null,
    divelogs_username: row.querySelector(".f-divelogs-account").value || null,
  }));
}

function settingsPayload(extra) {
  return Object.assign({
    directionality: $("default-directionality").value,
    sync_filters: {
      date_from: currentSettings?.sync_filters?.date_from ?? null,
      date_to: currentSettings?.sync_filters?.date_to ?? null,
      only_new: $("default-only-new").checked,
      sync_gases: $("default-sync-gases").checked,
      // No UI control any more (rework.md E6); pass the stored value
      // through unchanged so a hand-set --backup preference isn't clobbered.
      sync_fit: currentSettings?.sync_filters?.sync_fit ?? false,
    },
    grace_window_minutes: parseInt($("grace-window").value, 10),
    api_cooldown_seconds: parseFloat($("api-cooldown").value),
    propagate_deletes: $("default-propagate-deletes").checked,
    create_on_garmin: $("default-create-on-garmin").checked,
    schedule: (currentSettings?.schedule || []).map((s) => ({ hour: s.hour, minute: s.minute })),
    cron_jobs: readCronTable(),
    sync_pairs: readPairsTable(),
  }, extra || {});
}

async function postSettings(payload) {
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  let message = "Saved.";
  if (!res.ok) {
    const detail = data.detail;
    message = typeof detail === "string" ? detail : (detail?.errors || [detail?.message || "Failed to save."]).join("\n");
  }
  return { ok: res.ok, message };
}

async function loadSettings() {
  const res = await fetch("/api/settings");
  currentSettings = await res.json();
  populateDefaults(currentSettings);
  $("pairs-body").innerHTML = "";
  (currentSettings.sync_pairs || []).forEach(addPairRow);
  populateCronTable(currentSettings.cron_jobs || []);
}

async function saveSchedule() {
  const { ok, message } = await postSettings(settingsPayload());
  $("schedule-message").textContent = message;
  if (ok) {
    await loadSettings();
    await loadFields();
  }
}

// ---------------------------------------------------------------- mapping board

const TYPE_ICON = { text: "T", number: "#", datetime: "⏱", gps: "⌖", list: "≡", tanks: "🛢", samples: "📈" };
const board = { pairId: null, links: [], saved: [], catalog: {}, sourceId: null, targetId: null, selected: null };

function boardPairInfo() {
  return (fieldsInfo?.pairs || []).find((p) => p.id === board.pairId) || null;
}

function isDirty() {
  return JSON.stringify(board.links) !== JSON.stringify(board.saved);
}

function markDirty() {
  $("board-dirty").hidden = !isDirty();
}

function linkKey(link) {
  return link.id;
}

async function loadFields() {
  const res = await fetch("/api/fields");
  fieldsInfo = await res.json();
  const select = $("board-pair");
  const previous = board.pairId;
  select.innerHTML = optionList(fieldsInfo.pairs.map((p) => [p.id, p.id === "default" ? `${p.source_name} ↔ ${p.target_name}` : `${p.id} (${p.source} → ${p.target})`]), previous || "default");
  selectPair(select.value || "default");
}

function savedLinksFor(pairId) {
  if (pairId === "default") return currentSettings?.field_links || [];
  const pair = (currentSettings?.sync_pairs || []).find((p) => p.id === pairId);
  const info = boardPairInfo();
  return pair?.field_links || info?.default_links || [];
}

function selectPair(pairId) {
  if (isDirty() && !confirm("Discard unsaved changes on this board?")) {
    $("board-pair").value = board.pairId;
    return;
  }
  board.pairId = pairId;
  const info = boardPairInfo();
  if (!info) return;
  board.sourceId = info.source;
  board.targetId = info.target;
  board.catalog = {};
  Object.values(info.fields).flat().forEach((f) => { board.catalog[f.key] = f; });
  board.saved = JSON.parse(JSON.stringify(savedLinksFor(pairId)));
  board.links = JSON.parse(JSON.stringify(board.saved));
  board.selected = null;
  $("board-source-name").textContent = info.source_name;
  $("board-target-name").textContent = info.target_name;
  $("pair-direction").innerHTML = optionList(directionOptionsFor(info.source, info.target), pairDirection());
  $("pair-grace").value = pairGrace();
  $("pair-propagate-deletes").checked = pairPropagateDeletes();
  $("pair-create-on-garmin").checked = pairCreateOnGarmin();
  renderBoard();
  loadConflicts();
}

function pairDirection() {
  if (board.pairId === "default") return currentSettings?.directionality || "bidirectional";
  return (currentSettings?.sync_pairs || []).find((p) => p.id === board.pairId)?.directionality || "bidirectional";
}

function pairGrace() {
  if (board.pairId === "default") return currentSettings?.grace_window_minutes ?? 15;
  const pair = (currentSettings?.sync_pairs || []).find((p) => p.id === board.pairId);
  return pair?.grace_window_minutes ?? currentSettings?.grace_window_minutes ?? 15;
}

function pairPropagateDeletes() {
  if (board.pairId === "default") return !!currentSettings?.propagate_deletes;
  const pair = (currentSettings?.sync_pairs || []).find((p) => p.id === board.pairId);
  const value = pair?.propagate_deletes;
  return value === null || value === undefined ? !!currentSettings?.propagate_deletes : !!value;
}

function pairCreateOnGarmin() {
  if (board.pairId === "default") return !!currentSettings?.create_on_garmin;
  const pair = (currentSettings?.sync_pairs || []).find((p) => p.id === board.pairId);
  const value = pair?.create_on_garmin;
  return value === null || value === undefined ? !!currentSettings?.create_on_garmin : !!value;
}

function fieldLi(field) {
  const li = document.createElement("li");
  li.dataset.key = field.key;
  li.dataset.type = field.type;
  li.draggable = true;
  li.title = field.key;
  li.classList.toggle("locked", !field.writable);
  li.innerHTML = `<span class="type" title="${field.type}">${TYPE_ICON[field.type] || "?"}</span>
    <span class="name">${escapeHtml(field.label)}</span>
    ${field.unit ? `<span class="unit">${escapeHtml(field.unit)}</span>` : ""}
    ${field.writable ? "" : `<span class="lock" title="Cannot be written by its service">🔒</span>`}`;
  li.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", field.key);
    e.dataTransfer.effectAllowed = "link";
  });
  li.addEventListener("dragover", (e) => {
    const from = draggingKey;
    if (!from || from === field.key) return;
    const ok = canLink(from, field.key);
    li.classList.toggle("drop-ok", ok === true);
    li.classList.toggle("drop-no", ok !== true);
    e.preventDefault();
  });
  li.addEventListener("dragleave", () => li.classList.remove("drop-ok", "drop-no"));
  li.addEventListener("drop", (e) => {
    e.preventDefault();
    li.classList.remove("drop-ok", "drop-no");
    const from = e.dataTransfer.getData("text/plain");
    if (from && from !== field.key) createLink(from, field.key);
  });
  return li;
}

let draggingKey = null;
document.addEventListener("dragstart", (e) => { draggingKey = e.target?.dataset?.key || null; });
document.addEventListener("dragend", () => { draggingKey = null; });

function serviceOf(key) {
  return key.split(".")[0];
}

function canLink(fromKey, toKey) {
  const from = board.catalog[fromKey];
  const to = board.catalog[toKey];
  if (!from || !to) return "Unknown field.";
  if (serviceOf(fromKey) === serviceOf(toKey)) return "Link a field with one on the other side.";
  const existing = board.links.find((l) => l.target === toKey);
  if (existing) {
    if (["tanks", "samples"].includes(from.type) || !["text", "number", "datetime", "list"].includes(from.type)) return "Only text, number, date and list fields can be combined into a composite.";
    if (to.type !== "text") return "A composite can only target a text field.";
    if (!to.writable) return "This field is locked (cannot be written by its service).";
    return true;
  }
  const compatible = from.type === to.type || (["list", "text"].includes(from.type) && ["list", "text"].includes(to.type));
  if (!compatible) return `Cannot link ${from.type} to ${to.type}.`;
  if (!to.writable && !from.writable) return "Both fields are locked.";
  return true;
}

function uniqueLinkId(base) {
  let id = base;
  let n = 2;
  while (board.links.some((l) => l.id === id)) id = `${base}_${n++}`;
  return id;
}

function createLink(fromKey, toKey) {
  const ok = canLink(fromKey, toKey);
  if (ok !== true) {
    $("board-message").textContent = ok;
    return;
  }
  const from = board.catalog[fromKey];
  const to = board.catalog[toKey];
  const existing = board.links.find((l) => l.target === toKey);
  if (existing) {
    existing.source.push(fromKey);
    existing.direction = "to_target";
    existing.template = (existing.template || existing.source.slice(0, -1).map((k) => `{${k}}`).join(" ")) + ` {${fromKey}}`;
    existing.match_order = null;
    renderBoard();
    openEditor(existing.id);
    return;
  }
  let direction = "bidirectional";
  if (!to.writable) direction = "to_source";
  else if (!from.writable) direction = "to_target";
  const structural = ["tanks", "samples"].includes(from.type);
  if (structural && direction === "bidirectional") direction = "to_target";
  const link = {
    id: uniqueLinkId(`${from.key.split(".")[1]}`),
    source: [fromKey], target: toKey, direction, conflict: "prefer_non_empty",
    template: null, reverse: null, match_order: null, separator: ", ", when: null,
  };
  board.links.push(link);
  $("board-message").textContent = "";
  renderBoard();
  openEditor(link.id);
}

function renderBoard() {
  const info = boardPairInfo();
  if (!info) return;
  const targets = new Set(board.links.map((l) => l.target));
  const sources = new Set(board.links.flatMap((l) => l.source));
  for (const [side, serviceId] of [["source", info.source], ["target", info.target]]) {
    const ul = $(`fields-${side}`);
    ul.innerHTML = "";
    (info.fields[serviceId] || []).forEach((f) => {
      const li = fieldLi(f);
      li.classList.toggle("linked", targets.has(f.key) || sources.has(f.key));
      ul.appendChild(li);
    });
  }
  renderLinksTable();
  markDirty();
  const keys = board.links.filter((l) => l.match_order != null).sort((a, b) => a.match_order - b.match_order).map((l) => l.id);
  $("board-matchkeys").textContent = keys.length ? `Match keys: ${keys.join(" → ")} (then start time)` : "Match keys: none (start time only)";
  requestAnimationFrame(drawLines);
}

function directionLabel(link) {
  return { bidirectional: "↔", to_target: "→", to_source: "←", off: "off" }[link.direction] || link.direction;
}

function renderLinksTable() {
  const body = $("links-body");
  body.innerHTML = "";
  board.links.forEach((link) => {
    const tr = document.createElement("tr");
    tr.dataset.id = link.id;
    tr.classList.toggle("off", link.direction === "off");
    tr.classList.toggle("selected", link.id === board.selected);
    const src = link.source.map((k) => `<span class="key" title="${escapeHtml(k)}">${escapeHtml(board.catalog[k]?.label || k)}</span>`).join(" + ");
    tr.innerHTML = `<td>${escapeHtml(link.id)}</td><td>${src}${link.template ? ` <span class="muted">(template)</span>` : ""}</td>
      <td>${directionLabel(link)}</td>
      <td><span class="key" title="${escapeHtml(link.target)}">${escapeHtml(board.catalog[link.target]?.label || link.target)}</span></td>
      <td>${escapeHtml(link.direction)}</td><td>${escapeHtml(link.conflict)}</td><td>${link.match_order ?? ""}</td>
      <td><button type="button" class="secondary edit-link">Edit</button> <button type="button" class="secondary danger delete-link">✕</button></td>`;
    tr.querySelector(".edit-link").addEventListener("click", () => openEditor(link.id));
    tr.querySelector(".delete-link").addEventListener("click", () => deleteLink(link.id));
    body.appendChild(tr);
  });
}

function drawLines() {
  const svg = $("board-lines");
  const boardEl = $("board");
  if (!svg || !boardEl) return;
  svg.innerHTML = `<defs>
    <marker id="arrow-end" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker>
  </defs>`;
  const boardRect = boardEl.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${boardRect.width} ${boardRect.height}`);
  const liFor = (key) => boardEl.querySelector(`li[data-key="${CSS.escape(key)}"]`);
  board.links.forEach((link) => {
    const to = liFor(link.target);
    if (!to) return;
    link.source.forEach((srcKey) => {
      const from = liFor(srcKey);
      if (!from) return;
      const fromLeft = from.getBoundingClientRect().left < to.getBoundingClientRect().left;
      const a = from.getBoundingClientRect();
      const b = to.getBoundingClientRect();
      const x1 = (fromLeft ? a.right : a.left) - boardRect.left;
      const y1 = a.top + a.height / 2 - boardRect.top;
      const x2 = (fromLeft ? b.left : b.right) - boardRect.left;
      const y2 = b.top + b.height / 2 - boardRect.top;
      const dx = (x2 - x1) / 2;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`);
      path.style.color = "var(--accent)";
      if (link.direction === "off") path.classList.add("off");
      if (link.direction === "to_target" || link.direction === "bidirectional") path.setAttribute("marker-end", "url(#arrow-end)");
      if (link.direction === "to_source" || link.direction === "bidirectional") path.setAttribute("marker-start", "url(#arrow-end)");
      if (link.id === board.selected) path.classList.add("selected");
      path.addEventListener("click", () => openEditor(link.id));
      svg.appendChild(path);
    });
  });
}

function deleteLink(id) {
  board.links = board.links.filter((l) => l.id !== id);
  if (board.selected === id) closeEditor();
  renderBoard();
}

// ---------------------------------------------------------------- link editor

let editorLinkId = null;
let previewTimer = null;

function allowedDirections(link) {
  const target = board.catalog[link.target];
  const source = board.catalog[link.source[0]];
  const structural = ["tanks", "samples"].includes(target?.type) || ["tanks", "samples"].includes(source?.type);
  const options = [];
  const composite = link.source.length > 1 || !!link.template;
  const reversible = composite && !!link.reverse;
  if ((!composite || reversible) && !structural && target?.writable && source?.writable) options.push(["bidirectional", "Both ways"]);
  if (target?.writable) options.push(["to_target", "Source → target"]);
  if ((!composite || reversible) && source?.writable) options.push(["to_source", "Target → source"]);
  options.push(["off", "Off (not synced)"]);
  return options;
}

function openEditor(id) {
  const link = board.links.find((l) => l.id === id);
  if (!link) return;
  editorLinkId = id;
  board.selected = id;
  $("editor-title").textContent = `${link.source.map((k) => k).join(" + ")} → ${link.target}`;
  $("editor-id").value = link.id;
  $("editor-direction").innerHTML = optionList(allowedDirections(link), link.direction);
  if (!allowedDirections(link).some(([v]) => v === link.direction)) $("editor-direction").value = "off";
  $("editor-conflict").value = link.conflict;
  $("editor-match").value = link.match_order ?? "";
  const keyTypes = link.source.map((k) => board.catalog[k]?.type);
  $("editor-match").disabled = link.source.length > 1 || !["number", "datetime"].includes(keyTypes[0]);
  $("editor-separator").value = link.separator ?? ", ";
  $("editor-template").value = link.template || "";
  const textTarget = board.catalog[link.target]?.type === "text";
  $("editor-template-block").hidden = !textTarget;
  $("editor-reverse").value = link.reverse || "";
  $("editor-reverse-block").hidden = !textTarget || (link.source.length === 1 && !link.template);
  $("editor-field-picker").innerHTML = optionList(link.source.map((k) => [k, board.catalog[k]?.label || k]), link.source[0]);
  $("link-editor").hidden = false;
  renderLinksTable();
  drawLines();
  schedulePreview();
}

function closeEditor() {
  $("link-editor").hidden = true;
  editorLinkId = null;
  board.selected = null;
  renderLinksTable();
  drawLines();
}

function editorLink() {
  const link = board.links.find((l) => l.id === editorLinkId);
  if (!link) return null;
  const match = $("editor-match").value;
  return Object.assign({}, link, {
    id: $("editor-id").value.trim() || link.id,
    direction: $("editor-direction").value,
    conflict: $("editor-conflict").value,
    match_order: match === "" ? null : parseInt(match, 10),
    separator: $("editor-separator").value || ", ",
    template: $("editor-template").hidden ? link.template : ($("editor-template").value.trim() || null),
    reverse: $("editor-reverse-block").hidden ? link.reverse : ($("editor-reverse").value.trim() || null),
  });
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 300);
}

async function runPreview() {
  const link = editorLink();
  if (!link || $("editor-template-block").hidden) return;
  if (!link.template && link.source.length === 1) {
    $("editor-preview").textContent = "(plain copy)";
    $("editor-problems").textContent = "";
    return;
  }
  const res = await fetch("/api/fields/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ link }),
  });
  const data = await res.json();
  if (!res.ok) {
    $("editor-problems").textContent = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    return;
  }
  let text = data.ok ? (data.text || "(empty)") : "–";
  if (data.ok && "reverse_sample" in data) {
    text += "  |  reverse -> " + (data.reverse_sample ? JSON.stringify(data.reverse_sample) : "(pattern does not match its own template output)");
  }
  $("editor-preview").textContent = text;
  $("editor-problems").textContent = [...(data.problems || []), ...(data.warnings || [])].join("\n");
}

function applyEditor() {
  const updated = editorLink();
  if (!updated) return;
  if (updated.id !== editorLinkId && board.links.some((l) => l.id === updated.id)) {
    $("editor-problems").textContent = `A link named ${updated.id} already exists.`;
    return;
  }
  const index = board.links.findIndex((l) => l.id === editorLinkId);
  board.links[index] = updated;
  editorLinkId = updated.id;
  board.selected = updated.id;
  renderBoard();
  $("editor-title").textContent = `${updated.source.join(" + ")} → ${updated.target}`;
  $("board-message").textContent = "Applied to the board (not saved yet).";
}

function insertFieldIntoTemplate() {
  const key = $("editor-field-picker").value;
  const area = $("editor-template");
  const start = area.selectionStart ?? area.value.length;
  const end = area.selectionEnd ?? start;
  area.value = area.value.slice(0, start) + `{${key}}` + area.value.slice(end);
  area.focus();
  area.selectionStart = area.selectionEnd = start + key.length + 2;
  schedulePreview();
}

// ---------------------------------------------------------------- save / cancel / reset / test

async function saveBoard() {
  const info = boardPairInfo();
  if (!info) return;
  const linksChanged = isDirty();
  let payload;
  if (board.pairId === "default") {
    $("default-directionality").value = $("pair-direction").value;
    $("grace-window").value = $("pair-grace").value;
    $("default-propagate-deletes").checked = $("pair-propagate-deletes").checked;
    $("default-create-on-garmin").checked = $("pair-create-on-garmin").checked;
    payload = settingsPayload({ field_links: board.links });
  } else {
    const pairs = readPairsTable().map((p) => (p.id === board.pairId
      ? Object.assign(p, { field_links: board.links, directionality: $("pair-direction").value,
                           grace_window_minutes: parseInt($("pair-grace").value, 10) || null,
                           propagate_deletes: $("pair-propagate-deletes").checked,
                           create_on_garmin: $("pair-create-on-garmin").checked })
      : p));
    payload = settingsPayload({ sync_pairs: pairs });
  }
  const { ok, message } = await postSettings(payload);
  $("board-message").textContent = message;
  if (!ok) return;
  await loadSettings();
  board.saved = JSON.parse(JSON.stringify(board.links));
  await loadFields();
  if (linksChanged && confirm("Apply the changed mapping to all matched dives on the next run? (No = only new and changed dives)")) {
    const res = await fetch(`/api/sync/full-compare?pair=${encodeURIComponent(board.pairId)}`, { method: "POST" });
    const data = await res.json();
    $("board-message").textContent = res.ok ? `Saved. ${data.message}` : (data.detail || "Saved, but the full-compare flag could not be set.");
  }
}

function cancelBoard() {
  board.links = JSON.parse(JSON.stringify(board.saved));
  closeEditor();
  renderBoard();
  $("board-message").textContent = "Changes discarded.";
}

function resetBoard() {
  const info = boardPairInfo();
  if (!info || !confirm("Replace the board with the shipped defaults for this pair? (Nothing is saved until you press Save.)")) return;
  board.links = JSON.parse(JSON.stringify(info.default_links || []));
  closeEditor();
  renderBoard();
}

async function testBoard() {
  $("board-message").textContent = "Fetching the newest dives from both services… this takes a while.";
  $("board-test").disabled = true;
  try {
    const res = await fetch("/api/mapping/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pair: board.pairId, field_links: board.links, limit: 10 }),
    });
    const data = await res.json();
    if (!res.ok) {
      $("board-message").textContent = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      return;
    }
    if (!data.ok) {
      $("board-message").textContent = "The board is not valid: " + data.problems.join("; ");
      return;
    }
    $("test-result").hidden = false;
    const unmatched = Object.entries(data.unmatched || {}).map(([s, t]) => `${s}: ${t.length}`).join(", ");
    $("test-summary").textContent = `Fetched ${JSON.stringify(data.fetched)}, ${data.matched} matched pair(s); unmatched ${unmatched || "none"}. Read-only: nothing was written.`;
    const body = $("test-body");
    body.innerHTML = "";
    data.rows.forEach((r) => {
      const tr = document.createElement("tr");
      const cls = r.conflict ? "result-conflict" : (r.result.startsWith("write") ? "result-write" : "");
      tr.innerHTML = `<td>${escapeHtml(r.dive_time)}</td><td>${escapeHtml(r.link)}</td><td class="key">${escapeHtml(r.source_key)}</td>
        <td>${escapeHtml(JSON.stringify(r.source_value))}</td><td class="key">${escapeHtml(r.target_key)}</td>
        <td>${escapeHtml(JSON.stringify(r.target_value))}</td><td class="${cls}">${escapeHtml(r.result)}${r.warnings?.length ? " ⚠" : ""}</td>`;
      body.appendChild(tr);
    });
    $("board-message").textContent = "";
  } finally {
    $("board-test").disabled = false;
  }
}

// ---------------------------------------------------------------- conflicts

async function loadConflicts() {
  const res = await fetch(`/api/conflicts?pair=${encodeURIComponent(board.pairId || "default")}`);
  const body = $("conflicts-body");
  body.innerHTML = "";
  if (!res.ok) {
    $("conflicts-message").textContent = "Could not load conflicts.";
    return;
  }
  const data = await res.json();
  if (!data.conflicts.length) {
    $("conflicts-message").textContent = "No conflicts waiting.";
    return;
  }
  $("conflicts-message").textContent = "";
  data.conflicts.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(c.dive_time)}</td><td>${escapeHtml(c.link_id)}</td>
      <td><span class="key">${escapeHtml(c.source_key)}</span><br>${escapeHtml(JSON.stringify(c.source_value))}</td>
      <td><span class="key">${escapeHtml(c.target_key)}</span><br>${escapeHtml(JSON.stringify(c.target_value))}</td>
      <td>${escapeHtml(c.seen_at)}</td>
      <td><button type="button" class="secondary pick-source">Use source</button> <button type="button" class="secondary pick-target">Use target</button></td>`;
    tr.querySelector(".pick-source").addEventListener("click", () => resolveConflict(c.id, "source"));
    tr.querySelector(".pick-target").addEventListener("click", () => resolveConflict(c.id, "target"));
    body.appendChild(tr);
  });
}

async function resolveConflict(id, winner) {
  $("conflicts-message").textContent = "Resolving…";
  const res = await fetch(`/api/conflicts/${encodeURIComponent(id)}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ winner, pair: board.pairId }),
  });
  const data = await res.json();
  $("conflicts-message").textContent = res.ok ? "Resolved." : (data.detail || "Failed.");
  loadConflicts();
}

// ---------------------------------------------------------------- profile

let pendingProfile = null;

async function checkProfile(apply) {
  const file = $("profile-file").files[0];
  if (!file) {
    $("profile-message").textContent = "Choose a profile file first.";
    return;
  }
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`/api/settings/import?apply=${apply ? "true" : "false"}`, { method: "POST", body: form });
  const data = await res.json();
  if (!res.ok) {
    const detail = data.detail;
    $("profile-message").textContent = typeof detail === "string" ? detail : (detail?.errors || [detail?.message]).join("; ");
    $("profile-apply").hidden = true;
    return;
  }
  const s = data.summary;
  const lines = [`Profile version ${s.version}`, ...(s.changes.length ? s.changes : ["no changes"])];
  if (s.skipped_links.length) lines.push(`skipped links (unknown fields): ${s.skipped_links.join(", ")}`);
  if (s.ignored_keys.length) lines.push(`ignored keys: ${s.ignored_keys.join(", ")}`);
  $("profile-summary").textContent = lines.join("\n");
  $("profile-summary").hidden = false;
  $("profile-apply").hidden = apply || !s.changes.length;
  $("profile-message").textContent = apply ? "Profile applied." : "Review the changes, then press Apply import.";
  if (apply) {
    await loadSettings();
    await loadFields();
  }
}

// ---------------------------------------------------------------- init

async function init() {
  document.querySelectorAll(".nav-item").forEach((el) => el.addEventListener("click", () => selectPage(el.dataset.page)));
  let startPage = "sync";
  try { startPage = localStorage.getItem("dive_sync_page") || "sync"; } catch (e) { /* private mode etc. */ }
  selectPage(startPage);

  loadVersion();
  loadStatus();
  setInterval(loadStatus, 15000);
  connectLogStream();
  // Cron rows read credentialsAccounts to populate their account dropdowns,
  // so it must be loaded before the cron table renders.
  await loadCredentialsStatus();
  loadSettings().then(loadFields);

  $("trigger-sync").addEventListener("click", triggerSync);
  $("credentials-form").addEventListener("submit", saveCredentials);
  $("test-credentials").addEventListener("click", testCredentials);
  $("garmin-add-account").addEventListener("click", () => addAccountRow("garmin"));
  $("divelogs-add-account").addEventListener("click", () => addAccountRow("divelogs"));
  $("submersion-store-type").addEventListener("change", updateSubmersionStoreFields);
  $("add-cron-job").addEventListener("click", () => addCronRow());
  $("add-pair").addEventListener("click", () => addPairRow());
  $("save-schedule").addEventListener("click", saveSchedule);
  $("notify-save").addEventListener("click", saveNotifyUrl);
  $("notify-test").addEventListener("click", testNotifyUrl);

  $("board-pair").addEventListener("change", (e) => selectPair(e.target.value));
  $("board-save").addEventListener("click", saveBoard);
  $("board-cancel").addEventListener("click", cancelBoard);
  $("board-reset").addEventListener("click", resetBoard);
  $("board-test").addEventListener("click", testBoard);
  $("editor-apply").addEventListener("click", applyEditor);
  $("editor-close").addEventListener("click", closeEditor);
  $("editor-delete").addEventListener("click", () => { if (editorLinkId) deleteLink(editorLinkId); });
  $("editor-insert").addEventListener("click", insertFieldIntoTemplate);
  $("editor-template").addEventListener("input", schedulePreview);
  $("editor-reverse").addEventListener("input", schedulePreview);
  $("editor-direction").addEventListener("change", schedulePreview);
  $("profile-check").addEventListener("click", () => checkProfile(false));
  $("profile-apply").addEventListener("click", () => checkProfile(true));
  window.addEventListener("resize", () => requestAnimationFrame(drawLines));
  window.addEventListener("beforeunload", (e) => {
    if (isDirty()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
