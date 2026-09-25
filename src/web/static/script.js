const $ = (id) => document.getElementById(id);

let currentSettings = null;
let fieldsInfo = null;           // GET /api/fields -> {pairs: [...]}
let credentialsAccounts = { garmin: [], divelogs: [] };  // usernames, for account-selector dropdowns

// ---------------------------------------------------------------- pages / sidebar nav

function selectPage(page) {
  if (!document.querySelector(`.nav-item[data-page="${page}"]`)) page = "sync";   // a page that no longer exists
  document.querySelectorAll(".page").forEach((el) => { el.hidden = el.dataset.page !== page; });
  document.querySelectorAll(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.page === page));
  if (page === "mapping") requestAnimationFrame(drawLines);  // board was laid out while hidden (0-size rects)
  if (page === "conflicts") loadConflicts();
  try { localStorage.setItem("dive_sync_page", page); } catch (e) { /* private mode etc. */ }
}

// ---------------------------------------------------------------- status, log, sync

async function loadVersion() {
  try {
    const res = await fetch("/api/version");
    const data = await res.json();
    const plain = (v) => String(v || "").replace(/^v/, "");
    $("version").textContent = `v${plain(data.current_version)}` +
      (data.update_available ? ` (update available: v${plain(data.latest_version)})` : "");
    return data;
  } catch (e) {
    $("version").textContent = "";
    return null;
  }
}

async function loadAbout() {
  try {
    const info = await (await fetch("/api/about")).json();
    $("about-name").textContent = info.name;
    $("about-version").textContent = `Version ${info.version}`;
    $("about-author").textContent = info.author;
    for (const [id, url] of [["about-project", info.project_url], ["about-issues", info.issues_url]]) {
      $(id).href = url;
      $(id).textContent = url;
    }
    $("about-platform").textContent = info.platform;
    $("about-license-title").textContent = `License - ${info.license_name}`;
    $("about-license-summary").textContent = `DiveSync is free software under the ${info.license_name} license: use, copy, modify and share it, keeping the copyright notice. It comes without any warranty.`;
    $("about-license").textContent = info.license_text;
    $("about-components").innerHTML = info.components
      .map((c) => `<tr><th>${escapeHtml(c.name)}</th><td>${escapeHtml(c.version)}</td></tr>`).join("");
  } catch (e) {
    $("about-version").textContent = "Version unknown";
  }
}

async function checkForUpdates() {
  $("about-update").textContent = "Checking…";
  const data = await loadVersion();
  if (!data) {
    $("about-update").textContent = "Could not check.";
  } else if (data.update_available) {
    $("about-update").innerHTML = `Version ${escapeHtml(String(data.latest_version).replace(/^v/, ""))} is available: <a href="${escapeHtml(data.release_url)}" target="_blank" rel="noopener">download</a>`;
  } else {
    $("about-update").textContent = "You have the latest version.";
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

// A Garmin refresh is minutes long, so while one runs the status poll speeds
// up from every 15 s to every second and the progress line is shown
// (rework.md E14).
let statusTimer = null;

function scheduleStatus(intervalMs) {
  if (statusTimer) clearInterval(statusTimer);
  statusTimer = setInterval(loadStatus, intervalMs);
  scheduleStatus.interval = intervalMs;
}

function renderProgress(data) {
  const box = $("status-progress");
  if (!box) return;
  const p = data.progress;
  const active = !!(data.is_running || data.is_downloading || p);
  box.hidden = !active;
  if (!active) return;
  // The bar only shows real progress: a stage without a count (logging in,
  // reading the dive lists) is its text alone, not an animated bar.
  const bar = $("status-progress-bar");
  const known = !!p && p.fraction !== null && p.fraction !== undefined;
  bar.hidden = !known;
  if (known) bar.value = p.fraction;
  const parts = [];
  if (p && p.message) parts.push(p.message);
  if (p && p.total > 0) parts.push(`${p.done} of ${p.total}`);
  $("status-progress-text").textContent = parts.join(" — ") || "Working…";
}

async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    $("status-running").textContent = data.is_running ? "yes" : "no";
    $("status-next").textContent = data.next_scheduled_run ? new Date(data.next_scheduled_run).toLocaleString() : "none";
    renderProgress(data);
    renderJobResults(data.last_results);
    const busy = !!(data.is_running || data.is_downloading || data.progress);
    const wanted = busy ? 1000 : 15000;
    if (scheduleStatus.interval !== wanted) scheduleStatus(wanted);
  } catch (e) {
    $("status-running").textContent = "?";
  }
}

async function triggerSync() {
  $("trigger-message").textContent = "Starting…";
  const payload = {
    dry_run: $("trigger-dry-run").checked,
    source: $("trigger-source").value,
    target: $("trigger-target").value,
    only_new: $("trigger-only-new").checked,
    sync_gases: $("trigger-gases").checked,
    use_garmin_cache: $("trigger-garmin-cache").checked,
  };
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

// ---- source / target pickers (Sync now and scheduled jobs), as in the app

let syncEndpoints = [];     // [{spec, id, label}]

async function loadEndpoints() {
  try {
    syncEndpoints = (await (await fetch("/api/sync/endpoints")).json()).endpoints || [];
  } catch (e) {
    syncEndpoints = [];
  }
  for (const prefix of ["trigger", "job"]) {
    const source = $(`${prefix}-source`);
    source.innerHTML = optionList(syncEndpoints.map((e) => [e.spec, e.label]), source.value);
    fillTargets(prefix);
  }
}

function endpointOf(spec) {
  return syncEndpoints.find((e) => e.spec === spec) || null;
}

function endpointLabel(spec) {
  return endpointOf(spec)?.label || spec;
}

// every endpoint but the source's own service
function fillTargets(prefix, keep) {
  const source = endpointOf($(`${prefix}-source`).value);
  const target = $(`${prefix}-target`);
  const wanted = keep ?? target.value;
  target.innerHTML = optionList(syncEndpoints.filter((e) => !source || e.id !== source.id).map((e) => [e.spec, e.label]), wanted);
}

function swapEndpoints(prefix) {
  const source = $(`${prefix}-source`).value, target = $(`${prefix}-target`).value;
  $(`${prefix}-source`).value = target;
  fillTargets(prefix, source);
}

function appendLogLine(line) {
  // Newest first, matching the desktop app's sync log: the latest line is in
  // view without scrolling, and the oldest drops off the bottom.
  const pre = $("log-tail");
  const lines = pre.textContent ? pre.textContent.split("\n") : [];
  lines.unshift(line);
  pre.textContent = lines.slice(0, 400).join("\n");
  pre.scrollTop = 0;
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
  // A stored username with no stored password cannot log in; saying so here
  // beats letting the next sync be the one to discover it.
  const missing = account.username && account.has_password === false
    ? `<span class="row-warning">⚠ no password stored</span>`
    : "";
  return `
    <label>Username <input class="a-username" type="text" value="${escapeHtml(account.username || "")}"></label>
    <label>Password <input class="a-password" type="password" placeholder="${account.username ? "unchanged" : ""}"></label>
    ${tokenDirField}
    <button type="button" class="secondary remove-row">✕</button>
    ${missing}
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
  renderAccountRows("garmin", data.garmin_account_rows || []);
  renderAccountRows("divelogs", data.divelogs_account_rows || []);
  credentialsAccounts = { garmin: data.garmin_accounts || [], divelogs: data.divelogs_accounts || [] };
  for (const prefix of ["trigger", "job"]) {
    populateAccountSelect(`${prefix}-garmin-account`, credentialsAccounts.garmin, true);
    populateAccountSelect(`${prefix}-divelogs-account`, credentialsAccounts.divelogs, true);
    $(`${prefix}-garmin-account-label`).hidden = credentialsAccounts.garmin.length < 2;
    $(`${prefix}-divelogs-account-label`).hidden = credentialsAccounts.divelogs.length < 2;
  }
  if (data.subsurface_email) $("subsurface-email").value = data.subsurface_email;
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
  $("credentials-message").textContent = parts.join(" · ") || "Enter a username and password to test.";
}

// ---------------------------------------------------------------- settings, pairs, schedule

function populateDefaults(settings) {
  $("api-cooldown").value = settings.api_cooldown_seconds;
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

// rework.md G1: the Garmin <-> Divelogs pair is the explicit "garmin_divelogs"
// entry of sync_pairs. Its direction, board and options are edited through the
// default form and the board header, so the pairs table and the job pair
// picker leave it out.
const DEFAULT_PAIR = "garmin_divelogs";

function pairIds() {
  return (currentSettings?.sync_pairs || []).map((p) => p.id).filter((id) => id !== DEFAULT_PAIR);
}

// rework.md G0: a run writes exactly one side, so a direction is always
// "to <service>"; a two-way sync is two jobs, one per direction.
function directionOptionsFor(sourceId, targetId, names) {
  const name = (id, fallback) => (names && id === names.source ? names.source_name : names && id === names.target ? names.target_name : fallback);
  return [[`to_${targetId}`, name(targetId, `To ${targetId}`)], [`to_${sourceId}`, name(sourceId, `To ${sourceId}`)]];
}

function pairRowHtml(pair) {
  return `
    <td><input class="p-id" value="${escapeHtml(pair.id)}"></td>
    <td><input class="p-source" value="${escapeHtml(pair.source)}" placeholder="garmin"></td>
    <td><input class="p-target" value="${escapeHtml(pair.target)}" placeholder="uddf:export.uddf"></td>
    <td><input class="p-direction" value="${escapeHtml(pair.directionality)}" placeholder="to_target" title="to_<service>, to_target or to_source - one side per run"></td>
    <td><input class="p-enabled" type="checkbox" ${pair.enabled ? "checked" : ""}></td>
    <td><button type="button" class="secondary remove-row">✕</button></td>
  `;
}

function addPairRow(pair) {
  const row = document.createElement("tr");
  const p = pair || { id: `pair-${Date.now()}`, source: "garmin", target: "uddf:export.uddf", directionality: "to_target", enabled: true };
  row.innerHTML = pairRowHtml(p);
  row.dataset.rules = JSON.stringify(pair?.rules ?? null);
  row.dataset.matchKeys = JSON.stringify(pair?.match_keys ?? []);
  row.dataset.grace = pair?.grace_window_minutes ?? "";
  row.dataset.propagateDeletes = pair?.propagate_deletes ?? "";
  row.dataset.createOnGarmin = pair?.create_on_garmin ?? "";
  row.querySelector(".remove-row").addEventListener("click", () => row.remove());
  $("pairs-body").appendChild(row);
}

function readPairsTable() {
  return Array.from($("pairs-body").querySelectorAll("tr")).map((row) => {
    const rules = JSON.parse(row.dataset.rules || "null");
    const matchKeys = JSON.parse(row.dataset.matchKeys || "[]");
    const grace = row.dataset.grace;
    const propagateDeletes = row.dataset.propagateDeletes;
    const createOnGarmin = row.dataset.createOnGarmin;
    return {
      id: row.querySelector(".p-id").value.trim(),
      source: row.querySelector(".p-source").value.trim(),
      target: row.querySelector(".p-target").value.trim(),
      directionality: row.querySelector(".p-direction").value.trim() || "to_target",
      enabled: row.querySelector(".p-enabled").checked,
      grace_window_minutes: grace === "" || grace === "null" ? null : parseInt(grace, 10),
      rules,
      match_keys: matchKeys,
      propagate_deletes: propagateDeletes === "" || propagateDeletes === "null" ? null : propagateDeletes === "true",
      create_on_garmin: createOnGarmin === "" || createOnGarmin === "null" ? null : createOnGarmin === "true",
    };
  });
}

// ---- scheduled jobs: a readable list and one editor; ids are made up here

let jobs = [];              // the cron_jobs being edited
let editingJob = -1;        // index in jobs, or -1 for a new one
const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function pad2(n) { return String(n).padStart(2, "0"); }

// A job's two sides: its own source/target, or those of the pair it names
// (older jobs), turned the way its direction writes.
function jobSides(job) {
  if (job.source && job.target) return [job.source, job.target];
  const pair = (currentSettings?.sync_pairs || []).find((p) => p.id === (job.pair || DEFAULT_PAIR));
  if (!pair) return [job.pair || "?", ""];
  const direction = job.directionality || pair.directionality || "to_target";
  const targetId = (spec) => (endpointOf(spec)?.id || spec.split(":")[0]);
  const reversed = direction === "to_source" || direction === `to_${targetId(pair.source)}`;
  return reversed ? [pair.target, pair.source] : [pair.source, pair.target];
}

function jobWhen(job) {
  const at = `${pad2(job.hour)}:${pad2(job.minute)}`;
  if (job.frequency === "hourly") return `every hour at :${pad2(job.minute)}`;
  if (job.frequency === "weekly") return `every ${WEEKDAYS[job.day_of_week] || "week"} at ${at}`;
  if (job.frequency === "custom_minutes") return `every ${job.interval_minutes} minutes`;
  return `every day at ${at}`;
}

function jobOptions(job) {
  const bits = [job.only_new ? "only new dives" : "all dives"];
  if (!job.sync_gases) bits.push("no gases");
  if (job.use_garmin_cache === false) bits.push("no Garmin cache");
  if (job.garmin_username) bits.push(job.garmin_username);
  if (job.divelogs_username) bits.push(job.divelogs_username);
  return bits.join(", ");
}

function renderJobs() {
  const list = $("jobs-list");
  list.innerHTML = "";
  if (!jobs.length) list.innerHTML = `<li class="muted">No scheduled jobs yet.</li>`;
  jobs.forEach((job, i) => {
    const [source, target] = jobSides(job);
    const li = document.createElement("li");
    li.innerHTML = `<label class="inline-checkbox" title="Enabled"><input type="checkbox" class="job-enabled" ${job.enabled ? "checked" : ""}></label>
      <span class="job-what"><strong>${escapeHtml(endpointLabel(source))} &rarr; ${escapeHtml(endpointLabel(target))}</strong>,
      ${escapeHtml(jobWhen(job))} <span class="muted">(${escapeHtml(jobOptions(job))})</span></span>
      <button type="button" class="secondary job-edit">Edit</button>
      <button type="button" class="secondary danger job-remove" title="Remove">&#x2715;</button>`;
    li.querySelector(".job-enabled").addEventListener("change", (e) => { job.enabled = e.target.checked; });
    li.querySelector(".job-edit").addEventListener("click", () => openJobEditor(i));
    li.querySelector(".job-remove").addEventListener("click", () => { jobs.splice(i, 1); renderJobs(); });
    list.appendChild(li);
  });
}

function showJobTimeFields() {
  const f = $("job-frequency").value;
  $("job-day-label").hidden = f !== "weekly";
  $("job-time-label").hidden = !["daily", "weekly"].includes(f);
  $("job-minute-label").hidden = f !== "hourly";
  $("job-interval-label").hidden = f !== "custom_minutes";
}

function openJobEditor(index) {
  editingJob = index;
  const job = index >= 0 ? jobs[index] : null;
  const [source, target] = job ? jobSides(job) : [$("job-source").value, $("job-target").value];
  if (source) $("job-source").value = source;
  fillTargets("job", target);
  $("job-frequency").value = job?.frequency || "daily";
  $("job-day").value = String(job?.day_of_week ?? 1);
  $("job-time").value = job ? `${pad2(job.hour)}:${pad2(job.minute)}` : "06:00";
  $("job-minute").value = job?.minute ?? 0;
  $("job-interval").value = job?.interval_minutes ?? 60;
  $("job-only-new").checked = job ? !!job.only_new : true;
  $("job-gases").checked = job ? !!job.sync_gases : true;
  $("job-garmin-cache").checked = job ? job.use_garmin_cache !== false : true;
  $("job-garmin-account").value = job?.garmin_username || "";
  $("job-divelogs-account").value = job?.divelogs_username || "";
  $("job-editor-title").textContent = job ? "Change job" : "Add a job";
  $("job-add").textContent = job ? "Apply" : "Add job";
  showJobTimeFields();
  showJobEditor(true);
}

// The form is open while a job is added or changed; "+ Add job" otherwise.
function showJobEditor(open) {
  $("job-editor").hidden = !open;
  $("job-new").hidden = open;
  if (open) $("job-editor").scrollIntoView({ block: "nearest" });
}

function jobId(source, target, frequency) {
  const base = `${endpointOf(source)?.id || "source"}-to-${endpointOf(target)?.id || "target"}-${frequency.replace("custom_minutes", "interval")}`;
  let id = base, n = 2;
  while (jobs.some((j, i) => j.id === id && i !== editingJob)) id = `${base}-${n++}`;
  return id;
}

function applyJobEditor() {
  const source = $("job-source").value, target = $("job-target").value;
  if (!source || !target) return;
  const [hh, mm] = ($("job-time").value || "06:00").split(":").map((x) => parseInt(x, 10));
  const frequency = $("job-frequency").value;
  const old = editingJob >= 0 ? jobs[editingJob] : {};
  const job = Object.assign({}, old, {
    id: old.id || jobId(source, target, frequency),
    source, target, pair: null,
    directionality: `to_${endpointOf(target)?.id || target}`,
    frequency,
    hour: frequency === "hourly" || frequency === "custom_minutes" ? 0 : hh,
    minute: frequency === "hourly" ? parseInt($("job-minute").value, 10) || 0 : (frequency === "custom_minutes" ? 0 : mm),
    day_of_week: parseInt($("job-day").value, 10),
    interval_minutes: Math.max(5, parseInt($("job-interval").value, 10) || 60),
    only_new: $("job-only-new").checked,
    sync_gases: $("job-gases").checked,
    use_garmin_cache: $("job-garmin-cache").checked,
    enabled: old.enabled ?? true,
    garmin_username: $("job-garmin-account").value || null,
    divelogs_username: $("job-divelogs-account").value || null,
  });
  if (editingJob >= 0) jobs[editingJob] = job; else jobs.push(job);
  editingJob = -1;
  showJobEditor(false);
  $("schedule-message").textContent = "Not saved yet - press Save schedule.";
  renderJobs();
}

// The saved settings with what this page edits applied. The Garmin <->
// Divelogs pair's options (direction, grace window, deletes, create on
// Garmin) are edited on the Mapping page and come in through ``extra``.
function settingsPayload(extra) {
  const saved = currentSettings || {};
  return Object.assign({
    directionality: saved.directionality,
    sync_filters: Object.assign({}, saved.sync_filters || {}),
    grace_window_minutes: saved.grace_window_minutes,
    api_cooldown_seconds: parseFloat($("api-cooldown").value),
    propagate_deletes: !!saved.propagate_deletes,
    create_on_garmin: !!saved.create_on_garmin,
    create_device_dives_on_submersion: !!saved.create_device_dives_on_submersion,
    schedule: (saved.schedule || []).map((s) => ({ hour: s.hour, minute: s.minute })),
    cron_jobs: jobs,
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
  (currentSettings.sync_pairs || []).filter((p) => p.id !== DEFAULT_PAIR).forEach(addPairRow);
  jobs = JSON.parse(JSON.stringify(currentSettings.cron_jobs || []));
  renderJobs();
}

async function saveSchedule() {
  const { ok, message } = await postSettings(settingsPayload());
  $("schedule-message").textContent = message;
  if (ok) {
    await loadSettings();
    await loadFields();
  }
}

// ---------------------------------------------------------------- mapping board (rework.md G5: receiver rules)

const TYPE_ICON = { text: "T", number: "#", datetime: "⏱", gps: "⌖", list: "≡", tanks: "🛢", samples: "📈" };
const TEMPLATE_SOURCE_TYPES = ["text", "number", "datetime", "list"];
const MATCH_KEY_TYPES = ["number", "datetime"];
// board.rules: {receiverId: [rule]}; board.matchKeys: [[sourceSideKey, targetSideKey]]
// `armed` is the click-to-connect half-move: click a sender field, then click
// a receiver field. With 36 Submersion fields a drag often spans more than the
// viewport, and a drag in flight does not scroll the page reliably.
// viewTarget: the receiver on view (Target box); the board shows the rules for writing it
const board = { pairId: null, rules: {}, matchKeys: [], saved: null, catalog: {}, sourceId: null, targetId: null, selected: null, armed: null, viewTarget: null };

function boardPairInfo() {
  return (fieldsInfo?.pairs || []).find((p) => p.id === board.pairId) || null;
}

function boardState() {
  return JSON.stringify({ rules: board.rules, matchKeys: board.matchKeys });
}

function isDirty() {
  return board.saved !== null && boardState() !== board.saved;
}

function markDirty() {
  $("board-dirty").hidden = !isDirty();
}

function serviceOf(key) {
  return key.split(".")[0];
}

function otherSide(serviceId) {
  return serviceId === board.sourceId ? board.targetId : board.sourceId;
}

function rulesOf(receiver) {
  if (!board.rules[receiver]) board.rules[receiver] = [];
  return board.rules[receiver];
}

function ruleFor(receiver, fieldKey) {
  return rulesOf(receiver).find((r) => r.target === fieldKey) || null;
}

function findRule(receiver, id) {
  return rulesOf(receiver).find((r) => r.id === id) || null;
}

function fieldLabel(key) {
  return board.catalog[key]?.label || key;
}

function serviceName(serviceId) {
  const info = boardPairInfo();
  if (!info) return serviceId;
  return serviceId === info.source ? info.source_name : serviceId === info.target ? info.target_name : serviceId;
}

// A split is a templated rule on the *other* receiver with a reverse
// pattern: on runs towards `receiver` it takes the other side's field (the
// rule's target) apart into this receiver's fields (the rule's sources).
// The board shows it in this receiver's panel, where it does its work.
function splitActive(rule) {
  return !!(rule.template && rule.reverse && (rule.reverse_conflict || rule.conflict) !== "target_wins");
}

function splitsInto(receiver) {
  return rulesOf(otherSide(receiver)).filter(splitActive);
}

function splitFor(receiver, fieldKey) {
  return splitsInto(receiver).find((r) => r.source.includes(fieldKey)) || null;
}

function splitPolicy(rule) {
  return rule.reverse_conflict || rule.conflict;
}

async function loadFields() {
  const res = await fetch("/api/fields");
  fieldsInfo = await res.json();
  const known = (fieldsInfo.pairs || []).some((p) => p.id === board.pairId);
  const view = known ? board.viewTarget : null;       // a reload (e.g. after Save) keeps the direction on view
  if (selectPair(known ? board.pairId : DEFAULT_PAIR) && view) {
    board.viewTarget = view;
    renderViewSelects();
    renderBoard();
  }
}

// ---- source / target view ---------------------------------------------
// The header picks Source and Target; the pair joining them is found either
// way round and the board shows the one panel for writing the target.

function boardEndpoints() {
  const out = [];
  (fieldsInfo?.pairs || []).forEach((p) => {
    [[p.source, p.source_name], [p.target, p.target_name]].forEach(([id, name]) => {
      if (!out.some((e) => e[0] === id)) out.push([id, name]);
    });
  });
  return out;
}

function boardTargetsFor(source) {
  const linked = new Set((fieldsInfo?.pairs || []).filter((p) => p.source === source || p.target === source)
    .map((p) => (p.source === source ? p.target : p.source)));
  return boardEndpoints().filter(([id]) => linked.has(id));
}

function renderViewSelects() {
  const target = currentReceiver();
  const source = target ? otherSide(target) : board.sourceId;
  $("board-source").innerHTML = optionList(boardEndpoints(), source);
  $("board-target").innerHTML = optionList(boardTargetsFor(source), target);
}

function showView(source, target) {
  const pair = (fieldsInfo?.pairs || []).find((p) => source !== target &&
    ((p.source === source && p.target === target) || (p.source === target && p.target === source)));
  if (!pair) {
    renderViewSelects();
    return;
  }
  if (pair.id !== board.pairId && !selectPair(pair.id)) {
    renderViewSelects();
    return;
  }
  board.viewTarget = target;
  board.armed = null;
  renderViewSelects();
  renderBoard();
}

function storedPair(pairId) {
  return (currentSettings?.sync_pairs || []).find((p) => p.id === pairId) || null;
}

function savedBoardFor(pairId) {
  const pair = storedPair(pairId);
  const info = boardPairInfo();
  if (pair?.rules) return { rules: pair.rules, matchKeys: pair.match_keys || [] };
  return { rules: info?.default_rules || {}, matchKeys: info?.default_match_keys || [] };
}

function selectPair(pairId, force) {
  if (!force && isDirty() && !confirm("Discard unsaved changes on this board?")) return false;
  board.pairId = pairId;
  const info = boardPairInfo();
  if (!info) return;
  board.sourceId = info.source;
  board.targetId = info.target;
  board.catalog = {};
  Object.values(info.fields).flat().forEach((f) => { board.catalog[f.key] = f; });
  const saved = savedBoardFor(pairId);
  board.rules = JSON.parse(JSON.stringify(saved.rules));
  board.matchKeys = JSON.parse(JSON.stringify(saved.matchKeys));
  board.saved = boardState();
  board.selected = null;
  board.armed = null;
  $("pair-direction").innerHTML = optionList(directionOptionsFor(info.source, info.target, info), savedDirectionValue());
  $("pair-grace").value = pairGrace();
  $("pair-propagate-deletes").checked = pairPropagateDeletes();
  $("pair-create-on-garmin").checked = pairCreateOnGarmin();
  $("pair-create-on-garmin-label").hidden = ![info.source, info.target].includes("garmin");
  board.viewTarget = null;          // on view: the side scheduled runs write, until Source/Target change it
  closeEditor();
  renderViewSelects();
  renderBoard();
  return true;
}

// pairDirection spelled to_<service id>, as the "Scheduled runs write to" box offers it
function savedDirectionValue() {
  const direction = pairDirection();
  if (direction === "to_target") return `to_${board.targetId}`;
  if (direction === "to_source") return `to_${board.sourceId}`;
  return direction;
}

function pairDirection() {
  if (board.pairId === DEFAULT_PAIR) return currentSettings?.directionality || "to_divelogs";
  return storedPair(board.pairId)?.directionality || "to_target";
}

function pairGrace() {
  if (board.pairId === DEFAULT_PAIR) return currentSettings?.grace_window_minutes ?? 15;
  return storedPair(board.pairId)?.grace_window_minutes ?? currentSettings?.grace_window_minutes ?? 15;
}

function pairPropagateDeletes() {
  if (board.pairId === DEFAULT_PAIR) return !!currentSettings?.propagate_deletes;
  const value = storedPair(board.pairId)?.propagate_deletes;
  return value === null || value === undefined ? !!currentSettings?.propagate_deletes : !!value;
}

function pairCreateOnGarmin() {
  if (board.pairId === DEFAULT_PAIR) return !!currentSettings?.create_on_garmin;
  const value = storedPair(board.pairId)?.create_on_garmin;
  return value === null || value === undefined ? !!currentSettings?.create_on_garmin : !!value;
}

// The receiver on view: the Target box, else the side scheduled runs write.
function currentReceiver() {
  if (board.viewTarget) return board.viewTarget;
  const direction = $("pair-direction").value || pairDirection();
  if (direction === "to_target" || direction === `to_${board.targetId}`) return board.targetId;
  if (direction === "to_source" || direction === `to_${board.sourceId}`) return board.sourceId;
  return null;
}

// ---- rendering --------------------------------------------------------

function policyLabel(rule) {
  return rule.conflict === "target_wins" ? "never overwrite" : rule.conflict;
}

function ruleSummary(rule) {
  const from = rule.source.map((k) => escapeHtml(board.catalog[k]?.label || k)).join(" + ");
  const extras = [];
  if (rule.template) extras.push("template");
  if (rule.reverse) extras.push("splits back");
  return `← ${from}${extras.length ? ` <span class="muted">(${extras.join(", ")})</span>` : ""} <span class="policy">[${escapeHtml(policyLabel(rule))}]</span>`;
}

function receiverFieldLi(receiver, field) {
  const li = document.createElement("li");
  li.dataset.key = field.key;
  li.dataset.receiver = receiver;
  li.dataset.role = "receiver";
  li.draggable = !!field.writable;     // a receiver field can be pulled onto a sender field as well
  li.classList.add("receiver-field");
  li.classList.toggle("locked", !field.writable);
  const rule = ruleFor(receiver, field.key);
  const split = splitFor(receiver, field.key);
  const other = otherSide(receiver);
  li.classList.toggle("linked", !!rule || !!split);
  li.classList.toggle("selected", (!!rule && board.selected?.receiver === receiver && board.selected?.id === rule.id) ||
    (!!split && board.selected?.receiver === other && board.selected?.id === split.id));
  li.title = field.key;
  li.innerHTML = `<span class="type" title="${field.type}">${TYPE_ICON[field.type] || "?"}</span>
    <span class="name">${escapeHtml(field.label)}</span>
    ${field.unit ? `<span class="unit">${escapeHtml(field.unit)}</span>` : ""}
    ${field.writable ? "" : `<span class="lock" title="Cannot be written by its service">🔒</span>`}
    ${rule ? `<span class="rule ${rule.conflict === "target_wins" ? "noop" : ""}" title="${escapeHtml(rule.id)}">${ruleSummary(rule)}</span>
             <button type="button" class="secondary danger delete-rule" title="Delete rule">✕</button>` : ""}
    ${split ? `<span class="rule split" title="${escapeHtml(split.id)}">⇠ split of ${escapeHtml(fieldLabel(split.target))} <span class="policy">[${escapeHtml(splitPolicy(split))}]</span></span>` : ""}`;
  if (split) {
    li.querySelector(".rule.split").addEventListener("click", (e) => {
      if (board.armed) return;
      e.stopPropagation();
      openEditor(other, split.id, receiver);
    });
  }
  if (rule) {
    li.querySelector(".rule:not(.split)").addEventListener("click", (e) => {
      if (board.armed) return;                       // let the click land as a connection
      e.stopPropagation();
      openEditor(receiver, rule.id);
    });
    li.querySelector(".delete-rule").addEventListener("click", (e) => { e.stopPropagation(); deleteRule(receiver, rule.id); });
  }
  li.addEventListener("click", () => {
    if (board.armed && board.armed.receiver === receiver) connectArmed(receiver, field.key);
  });
  li.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", field.key);
    e.dataTransfer.effectAllowed = "link";
  });
  li.addEventListener("dragover", (e) => {
    const from = draggingKey;
    if (!from || dragging.role !== "sender" || dragging.receiver !== receiver) return;
    const ok = canDrop(from, receiver, field.key);
    li.classList.toggle("drop-ok", ok === true);
    li.classList.toggle("drop-no", ok !== true);
    e.preventDefault();
  });
  li.addEventListener("dragleave", () => li.classList.remove("drop-ok", "drop-no"));
  li.addEventListener("drop", (e) => {
    e.preventDefault();
    li.classList.remove("drop-ok", "drop-no");
    const from = e.dataTransfer.getData("text/plain");
    if (from && dragging.role === "sender" && dragging.receiver === receiver) {
      dragging.dropped = true;
      createRule(from, receiver, field.key);
    }
  });
  return li;
}

function senderFieldLi(receiver, field) {
  const li = document.createElement("li");
  li.dataset.key = field.key;
  li.dataset.receiver = receiver;
  li.dataset.role = "sender";
  li.draggable = true;
  li.title = field.key;
  const used = rulesOf(receiver).some((r) => r.source.includes(field.key)) ||
    splitsInto(receiver).some((r) => r.target === field.key);
  li.classList.toggle("linked", used);
  li.innerHTML = `<span class="type" title="${field.type}">${TYPE_ICON[field.type] || "?"}</span>
    <span class="name">${escapeHtml(field.label)}</span>
    ${field.unit ? `<span class="unit">${escapeHtml(field.unit)}</span>` : ""}`;
  li.classList.toggle("armed", !!board.armed && board.armed.receiver === receiver && board.armed.key === field.key);
  li.addEventListener("click", () => armSource(receiver, field.key));
  li.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", field.key);
    e.dataTransfer.effectAllowed = "link";
  });
  // ... and takes a receiver field dropped onto it (same rule, other way round)
  li.addEventListener("dragover", (e) => {
    const from = draggingKey;
    if (!from || dragging.role !== "receiver" || dragging.receiver !== receiver) return;
    const ok = canDrop(field.key, receiver, from);
    li.classList.toggle("drop-ok", ok === true);
    li.classList.toggle("drop-no", ok !== true);
    e.preventDefault();
  });
  li.addEventListener("dragleave", () => li.classList.remove("drop-ok", "drop-no"));
  li.addEventListener("drop", (e) => {
    e.preventDefault();
    li.classList.remove("drop-ok", "drop-no");
    const from = e.dataTransfer.getData("text/plain");
    if (from && dragging.role === "receiver" && dragging.receiver === receiver) {
      dragging.dropped = true;
      createRule(field.key, receiver, from);
    }
  });
  return li;
}

function armSource(receiver, key) {
  if (board.armed && board.armed.receiver === receiver && board.armed.key === key) {
    board.armed = null;
    $("board-message").textContent = "";
  } else {
    board.armed = { receiver, key };
    const label = board.catalog[key]?.label || key;
    $("board-message").textContent = `${label} armed - now click a ${receiver} field to take it (Esc cancels).`;
  }
  renderBoard();
}

function connectArmed(receiver, targetKey) {
  const armed = board.armed;
  board.armed = null;
  if (armed) createRule(armed.key, receiver, targetKey);
  else renderBoard();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && board.armed) {
    board.armed = null;
    $("board-message").textContent = "";
    renderBoard();
  }
});

// The window scrolls, not a container, and a drag in flight does not always
// scroll it (Safari in particular). Nudge it when the pointer nears an edge.
const DRAG_EDGE = 90, DRAG_STEP = 18;
document.addEventListener("dragover", (e) => {
  if (!draggingKey) return;
  if (e.clientY < DRAG_EDGE) window.scrollBy(0, -DRAG_STEP);
  else if (e.clientY > window.innerHeight - DRAG_EDGE) window.scrollBy(0, DRAG_STEP);
});

let draggingKey = null;
const dragging = { role: null, receiver: null, dropped: false };
document.addEventListener("dragstart", (e) => {
  const data = e.target?.dataset || {};
  draggingKey = data.key || null;
  dragging.role = data.role || null;
  dragging.receiver = data.receiver || null;
  dragging.dropped = false;
});
document.addEventListener("dragend", (e) => {
  if (draggingKey && e.target?.dataset?.role && !dragging.dropped) {
    $("board-message").textContent = "Drop a field onto a field in the other column of the same panel to add a rule.";
  }
  draggingKey = null;
  dragging.role = dragging.receiver = null;
});

function canDrop(fromKey, receiver, toKey) {
  const from = board.catalog[fromKey];
  const to = board.catalog[toKey];
  if (!from || !to) return "Unknown field.";
  if (serviceOf(fromKey) !== otherSide(receiver) || serviceOf(toKey) !== receiver) return "Drag a field from the left-hand list onto a field on the right.";
  if (!to.writable) return "This field is locked (cannot be written by its service).";
  if (!from.readable) return "This field cannot be read.";
  const existing = ruleFor(receiver, toKey);
  if (existing) {
    if (existing.source.includes(fromKey)) return "Already part of this rule.";
    if (!TEMPLATE_SOURCE_TYPES.includes(from.type)) return "Only text, number, date and list fields can be combined into a composite.";
    if (to.type !== "text") return "A composite can only target a text field.";
    return true;
  }
  const compatible = from.type === to.type || (["list", "text"].includes(from.type) && ["list", "text"].includes(to.type));
  if (!compatible) return `Cannot take a ${from.type} value into a ${to.type} field.`;
  return true;
}

function uniqueRuleId(receiver, base) {
  let id = base;
  let n = 2;
  while (findRule(receiver, id)) id = `${base}_${n++}`;
  return id;
}

// Dropping one sender text field onto a second receiver field it already
// feeds (by a plain rule or a split) offers to split it: one sender value
// taken apart into several receiver fields. The split is stored as a
// templated rule on the other receiver with reverse "auto" - see splitActive.
function splitProposal(fromKey, receiver, toKey) {
  const isText = (k) => board.catalog[k]?.type === "text";
  if (!isText(fromKey) || !isText(toKey) || !board.catalog[toKey]?.writable) return null;
  const other = otherSide(receiver);
  const composite = ruleFor(other, fromKey);
  let fields, replaced = [];
  if (composite && splitActive(composite)) {
    if (composite.source.includes(toKey)) return null;
    fields = [...composite.source, toKey];
  } else {
    const plain = rulesOf(receiver).find((r) => r.target !== toKey && !r.template &&
      r.source.length === 1 && r.source[0] === fromKey && isText(r.target));
    if (!plain) return null;
    fields = [plain.target, toKey];
    replaced.push(plain);
  }
  const own = ruleFor(receiver, toKey);
  if (own) replaced.push(own);
  const names = fields.map(fieldLabel).join(" + ");
  let question = `Split ${fieldLabel(fromKey)} into ${names}?\n\nOn runs to ${serviceName(receiver)} the value is cut at the text between the fields (", " by default; change it in the template).`;
  const dropped = replaced.filter((r) => r.target !== fields[0] || r.source[0] !== fromKey);
  if (dropped.length) question += `\n\nReplaces the rule on ${dropped.map((r) => `${fieldLabel(r.target)} (← ${r.source.map(fieldLabel).join(" + ")})`).join(", ")}.`;
  question += "\n\nCancel = add it as a normal rule instead.";
  return { question, apply: () => makeSplit(fromKey, receiver, fields, replaced) };
}

function makeSplit(fromKey, receiver, fields, replaced) {
  const other = otherSide(receiver);
  board.rules[receiver] = rulesOf(receiver).filter((r) => !replaced.includes(r));
  let rule = ruleFor(other, fromKey);
  if (rule && rule.source.every((k) => serviceOf(k) === receiver)) {
    const missing = fields.filter((k) => !rule.source.includes(k));
    if (!rule.template) rule.template = rule.source.map((k) => `{${k}}`).join(", ");
    missing.forEach((k) => { rule.template += `, {${k}}`; rule.source.push(k); });
    if (!rule.reverse) rule.reverse = "auto";
    if (!splitActive(rule) || !rule.reverse_conflict) rule.reverse_conflict = "prefer_source";
  } else {
    if (rule) board.rules[other] = rulesOf(other).filter((r) => r !== rule);
    rule = {
      id: uniqueRuleId(other, `${fromKey.split(".")[1]}_split`),
      target: fromKey, source: [...fields], conflict: "target_wins",
      template: fields.map((k) => `{${k}}`).join(", "), reverse: "auto", reverse_conflict: "prefer_source",
      separator: ", ", when: null,
    };
    rulesOf(other).push(rule);
  }
  $("board-message").textContent = `${fieldLabel(fromKey)} is now split into ${rule.source.map(fieldLabel).join(" + ")} (not saved yet).`;
  renderBoard();
  openEditor(other, rule.id, receiver);
}

function createRule(fromKey, receiver, toKey) {
  const ok = canDrop(fromKey, receiver, toKey);
  const split = splitProposal(fromKey, receiver, toKey);
  if (split && confirm(split.question)) {
    split.apply();
    return;
  }
  if (ok !== true) {
    $("board-message").textContent = ok;
    return;
  }
  const existing = ruleFor(receiver, toKey);
  if (existing) {
    existing.template = (existing.template || existing.source.map((k) => `{${k}}`).join(" ")) + ` {${fromKey}}`;
    existing.source.push(fromKey);
    renderBoard();
    openEditor(receiver, existing.id);
    return;
  }
  const rule = {
    id: uniqueRuleId(receiver, toKey.split(".")[1]),
    target: toKey, source: [fromKey], conflict: "prefer_non_empty",
    template: null, reverse: null, reverse_conflict: null, separator: ", ", when: null,
  };
  rulesOf(receiver).push(rule);
  $("board-message").textContent = "";
  renderBoard();
  openEditor(receiver, rule.id);
}

function deleteRule(receiver, id) {
  board.rules[receiver] = rulesOf(receiver).filter((r) => r.id !== id);
  if (board.selected?.receiver === receiver && board.selected?.id === id) closeEditor();
  renderBoard();
}

function renderBoard() {
  const info = boardPairInfo();
  if (!info) return;
  const active = currentReceiver();
  const container = $("receivers");
  container.innerHTML = "";
  // one panel: the rules for writing the target on view
  const receivers = [active || info.target];
  const names = { [info.source]: info.source_name, [info.target]: info.target_name };
  const scheduled = ($("pair-direction").value || savedDirectionValue()) === `to_${active}`;
  receivers.forEach((receiver) => {
    const sender = otherSide(receiver);
    const section = document.createElement("section");
    section.className = "receiver";
    section.dataset.receiver = receiver;
    const badge = scheduled
      ? `<span class="badge active">scheduled runs write here</span>`
      : `<span class="badge">scheduled runs write the other way</span>`;
    section.innerHTML = `<h3>${escapeHtml(names[sender])} → ${escapeHtml(names[receiver])} ${badge}</h3>
      <div class="board">
        <div class="board-column"><h4>${escapeHtml(names[sender])} fields (drag from here)</h4><ul class="field-list sender-fields"></ul></div>
        <svg class="board-lines" aria-hidden="true"></svg>
        <div class="board-column"><h4>${escapeHtml(names[receiver])} fields (drop here)</h4><ul class="field-list receiver-fields"></ul></div>
      </div>`;
    const rcvList = section.querySelector(".receiver-fields");
    (info.fields[receiver] || []).forEach((f) => rcvList.appendChild(receiverFieldLi(receiver, f)));
    const sndList = section.querySelector(".sender-fields");
    (info.fields[sender] || []).filter((f) => f.readable !== false).forEach((f) => sndList.appendChild(senderFieldLi(receiver, f)));
    container.appendChild(section);
  });
  renderMatchKeys();
  markDirty();
  requestAnimationFrame(drawLines);
}

function drawLines() {
  document.querySelectorAll("#receivers .receiver").forEach((section) => {
    const receiver = section.dataset.receiver;
    const svg = section.querySelector(".board-lines");
    const boardEl = section.querySelector(".board");
    if (!svg || !boardEl) return;
    svg.innerHTML = `<defs>
      <marker id="arrow-${receiver}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker>
    </defs>`;
    const boardRect = boardEl.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${boardRect.width} ${boardRect.height}`);
    const liFor = (list, key) => section.querySelector(`.${list} li[data-key="${CSS.escape(key)}"]`);
    const connect = (from, to, classes, selected, onClick) => {
      const a = from.getBoundingClientRect();
      const b = to.getBoundingClientRect();
      const x1 = a.right - boardRect.left;
      const y1 = a.top + a.height / 2 - boardRect.top;
      const x2 = b.left - boardRect.left;
      const y2 = b.top + b.height / 2 - boardRect.top;
      const dx = (x1 - x2) / 2;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M${x1},${y1} C${x1 - dx},${y1} ${x2 + dx},${y2} ${x2},${y2}`);
      path.style.color = "var(--accent)";
      classes.forEach((c) => path.classList.add(c));
      path.setAttribute("marker-end", `url(#arrow-${receiver})`);
      if (selected) path.classList.add("selected");
      path.addEventListener("click", onClick);
      svg.appendChild(path);
    };
    rulesOf(receiver).forEach((rule) => {
      const to = liFor("receiver-fields", rule.target);
      if (!to) return;
      rule.source.forEach((srcKey) => {
        const from = liFor("sender-fields", srcKey);
        if (!from) return;
        connect(from, to, rule.conflict === "target_wins" ? ["off"] : [],
          board.selected?.receiver === receiver && board.selected?.id === rule.id,
          () => openEditor(receiver, rule.id));
      });
    });
    const other = otherSide(receiver);
    splitsInto(receiver).forEach((rule) => {
      const from = liFor("sender-fields", rule.target);
      if (!from) return;
      rule.source.forEach((key) => {
        const to = liFor("receiver-fields", key);
        if (!to) return;
        connect(from, to, ["split"], board.selected?.receiver === other && board.selected?.id === rule.id,
          () => openEditor(other, rule.id, receiver));
      });
    });
  });
}

// ---- match keys -------------------------------------------------------

function matchKeyFields(serviceId) {
  const info = boardPairInfo();
  return (info?.fields[serviceId] || []).filter((f) => MATCH_KEY_TYPES.includes(f.type));
}

function renderMatchKeys() {
  const list = $("match-keys-list");
  list.innerHTML = "";
  if (!board.matchKeys.length) list.textContent = "none (start time only)";
  board.matchKeys.forEach(([a, b], i) => {
    const row = document.createElement("span");
    row.className = "key-row";
    row.innerHTML = `${i + 1}. ${escapeHtml(serviceName(serviceOf(a)))} ${escapeHtml(fieldLabel(a))} = ${escapeHtml(serviceName(serviceOf(b)))} ${escapeHtml(fieldLabel(b))}
      <button type="button" class="secondary up" title="Try earlier" ${i === 0 ? "disabled" : ""}>↑</button><button type="button" class="secondary danger remove" title="Remove">✕</button>`;
    row.querySelector(".up").addEventListener("click", () => {
      [board.matchKeys[i - 1], board.matchKeys[i]] = [board.matchKeys[i], board.matchKeys[i - 1]];
      renderMatchKeys(); markDirty();
    });
    row.querySelector(".remove").addEventListener("click", () => { board.matchKeys.splice(i, 1); renderMatchKeys(); markDirty(); });
    list.appendChild(row);
  });
  const a = $("match-key-a"), b = $("match-key-b");
  a.innerHTML = optionList(matchKeyFields(board.sourceId).map((f) => [f.key, f.label || f.key]), a.value);
  b.innerHTML = optionList(matchKeyFields(board.targetId).map((f) => [f.key, f.label || f.key]), b.value);
  $("match-key-a-name").textContent = serviceName(board.sourceId);
  $("match-key-b-name").textContent = serviceName(board.targetId);
}

function addMatchKey() {
  const a = $("match-key-a").value, b = $("match-key-b").value;
  if (!a || !b) return;
  if (board.catalog[a]?.type !== board.catalog[b]?.type) {
    $("board-message").textContent = "A match key pairs two fields of the same type.";
    return;
  }
  if (board.matchKeys.some(([x, y]) => x === a && y === b)) return;
  board.matchKeys.push([a, b]);
  renderMatchKeys();
  markDirty();
}

// ---- rule editor ------------------------------------------------------

let previewTimer = null;

function selectedRule() {
  return board.selected ? findRule(board.selected.receiver, board.selected.id) : null;
}

// `view` is the panel the rule was opened from: a split opened in the panel
// it writes reads "Activity name → Location + Dive site".
function openEditor(receiver, id, view) {
  const rule = findRule(receiver, id);
  if (!rule) return;
  board.selected = { receiver, id };
  const label = fieldLabel;
  const sources = rule.source.map(label).join(" + ");
  $("editor-title").textContent = view && view !== receiver
    ? `${label(rule.target)} → ${sources}  (split, on runs to ${serviceName(view)})`
    : `${sources} → ${label(rule.target)}  (written to ${serviceName(receiver)})`;
  $("editor-id").value = rule.id;
  $("editor-conflict").value = rule.conflict;
  $("editor-separator").value = rule.separator ?? ", ";
  const textTarget = board.catalog[rule.target]?.type === "text";
  const composite = rule.source.length > 1 || !!rule.template;
  $("editor-template").value = rule.template || "";
  // a plain one-field rule shows only its policy; the template opens on request
  $("editor-template-block").hidden = !(textTarget && composite);
  $("editor-template-toggle").hidden = !(textTarget && !composite);
  // the separator only means something where a list meets text
  $("editor-separator-label").hidden = ![...rule.source, rule.target].some((k) => board.catalog[k]?.type === "list");
  $("editor-advanced").open = false;
  const custom = rule.reverse && rule.reverse.trim().toLowerCase() !== "auto" ? rule.reverse : "";
  $("editor-split").checked = splitActive(rule);
  $("editor-split-label").textContent = `Split ${label(rule.target)} back into ${sources} on runs to ${serviceName(otherSide(receiver))}`;
  $("editor-reverse").value = custom;
  $("editor-reverse-advanced").open = !!custom;
  $("editor-reverse-conflict").value = rule.reverse_conflict && rule.reverse_conflict !== "target_wins" ? rule.reverse_conflict : "";
  $("editor-reverse-block").hidden = !textTarget || !composite;
  $("editor-split-options").hidden = !$("editor-split").checked;
  $("editor-field-picker").innerHTML = optionList(rule.source.map((k) => [k, label(k)]), rule.source[0]);
  $("rule-editor").hidden = false;
  renderBoard();
  schedulePreview();
}

function closeEditor() {
  $("rule-editor").hidden = true;
  if (board.selected) {
    board.selected = null;
    renderBoard();
  }
}

function editorRule() {
  const rule = selectedRule();
  if (!rule) return null;
  const reverseHidden = $("editor-reverse-block").hidden;
  const updated = Object.assign({}, rule, {
    id: $("editor-id").value.trim() || rule.id,
    conflict: $("editor-conflict").value,
    separator: $("editor-separator").value || ", ",
    template: $("editor-template-block").hidden ? rule.template : ($("editor-template").value.trim() || null),
    reverse: reverseHidden ? rule.reverse : ($("editor-split").checked ? ($("editor-reverse").value.trim() || "auto") : null),
    reverse_conflict: reverseHidden ? rule.reverse_conflict : ($("editor-split").checked ? ($("editor-reverse-conflict").value || null) : null),
  });
  // "same as above" on a rule that never overwrites its target would switch
  // the split off with it
  if (updated.reverse && !updated.reverse_conflict && updated.conflict === "target_wins") updated.reverse_conflict = "prefer_source";
  return updated;
}

// The preview endpoint validates a link; a rule is one aimed at its target.
function previewLinkFor(rule) {
  return {
    id: rule.id, source: rule.source, target: rule.target, template: rule.template, reverse: rule.reverse,
    direction: rule.reverse ? "bidirectional" : "to_target", conflict: "manual", separator: rule.separator, match_order: null, when: null,
  };
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 300);
}

async function runPreview() {
  const rule = editorRule();
  if (!rule || $("editor-template-block").hidden) return;
  if (!rule.template && rule.source.length === 1) {
    $("editor-preview").textContent = "(plain copy)";
    $("editor-problems").textContent = "";
    return;
  }
  const res = await fetch("/api/fields/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ link: previewLinkFor(rule) }),
  });
  const data = await res.json();
  if (!res.ok) {
    $("editor-problems").textContent = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    return;
  }
  let text = data.ok ? (data.text || "(empty)") : "–";
  if (data.ok && "reverse_sample" in data) {
    const sample = data.reverse_sample;
    text += "  |  split back: " + (sample
      ? Object.entries(sample).map(([k, v]) => `${fieldLabel(k)} = "${v}"`).join(" · ")
      : "(the split pattern does not match the template's own output)");
  }
  $("editor-preview").textContent = text;
  $("editor-problems").textContent = [...(data.problems || []), ...(data.warnings || [])].join("\n");
}

function applyEditor() {
  const updated = editorRule();
  if (!updated || !board.selected) return;
  const { receiver, id } = board.selected;
  if (updated.id !== id && findRule(receiver, updated.id)) {
    $("editor-problems").textContent = `A rule named ${updated.id} already exists on this side.`;
    return;
  }
  if (updated.reverse && !updated.template) {
    $("editor-problems").textContent = "A reverse pattern needs a template.";
    return;
  }
  const list = rulesOf(receiver);
  list[list.findIndex((r) => r.id === id)] = updated;
  board.selected = { receiver, id: updated.id };
  renderBoard();
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

// ---- save / cancel / reset / test ------------------------------------

function boardPairEntry() {
  // the stored pair (full dict) with this board's rules, keys and options applied
  // a combination of configured services not saved yet becomes a sync_pairs entry here
  const info = boardPairInfo();
  const stored = storedPair(board.pairId) || { id: board.pairId, source: info?.source_spec || board.sourceId,
                                              target: info?.target_spec || board.targetId, enabled: true };
  const entry = Object.assign({}, stored, { rules: board.rules, match_keys: board.matchKeys, directionality: $("pair-direction").value });
  delete entry.field_links;
  if (board.pairId !== DEFAULT_PAIR) {
    Object.assign(entry, {
      grace_window_minutes: parseInt($("pair-grace").value, 10) || null,
      propagate_deletes: $("pair-propagate-deletes").checked,
      create_on_garmin: $("pair-create-on-garmin").checked,
    });
  }
  return entry;
}

async function saveBoard() {
  const info = boardPairInfo();
  if (!info) return;
  const rulesChanged = isDirty();
  // the Garmin <-> Divelogs pair's options are the global defaults
  const globals = board.pairId === DEFAULT_PAIR ? {
    directionality: $("pair-direction").value,
    grace_window_minutes: parseInt($("pair-grace").value, 10),
    propagate_deletes: $("pair-propagate-deletes").checked,
    create_on_garmin: $("pair-create-on-garmin").checked,
  } : {};
  const others = readPairsTable().filter((p) => p.id !== board.pairId);
  const payload = settingsPayload(Object.assign(globals, { sync_pairs: [boardPairEntry(), ...others] }));
  const { ok, message } = await postSettings(payload);
  $("board-message").textContent = message;
  if (!ok) return;
  await loadSettings();
  board.saved = boardState();
  await loadFields();
  if (rulesChanged && confirm("Apply the changed mapping to all matched dives on the next run? (No = only new and changed dives)")) {
    const res = await fetch(`/api/sync/full-compare?pair=${encodeURIComponent(board.pairId)}`, { method: "POST" });
    const data = await res.json();
    $("board-message").textContent = res.ok ? `Saved. ${data.message}` : (data.detail || "Saved, but the full-compare flag could not be set.");
  }
}

function cancelBoard() {
  const saved = savedBoardFor(board.pairId);
  board.rules = JSON.parse(JSON.stringify(saved.rules));
  board.matchKeys = JSON.parse(JSON.stringify(saved.matchKeys));
  closeEditor();
  renderBoard();
  $("board-message").textContent = "Changes discarded.";
}

function resetBoard() {
  const info = boardPairInfo();
  if (!info || !confirm("Replace the board with the shipped defaults for this pair? (Nothing is saved until you press Save.)")) return;
  board.rules = JSON.parse(JSON.stringify(info.default_rules || {}));
  board.matchKeys = JSON.parse(JSON.stringify(info.default_match_keys || []));
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
      body: JSON.stringify({ pair: board.pairId, rules: board.rules, match_keys: board.matchKeys, limit: 10 }),
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
    $("test-summary").textContent = `Direction ${data.directionality} (writes ${data.receiver}): fetched ${JSON.stringify(data.fetched)}, ${data.matched} matched pair(s); unmatched ${unmatched || "none"}. Read-only: nothing was written.`;
    const body = $("test-body");
    body.innerHTML = "";
    data.rows.forEach((r) => {
      const tr = document.createElement("tr");
      const cls = r.conflict ? "result-conflict" : (r.result.startsWith("write") ? "result-write" : "");
      tr.innerHTML = `<td>${escapeHtml(r.dive_time)}</td><td>${escapeHtml(r.link)}${r.split ? " (split)" : ""}</td><td>${escapeHtml(r.receiver || "")}</td>
        <td class="key">${escapeHtml(r.source_key)}</td><td>${escapeHtml(JSON.stringify(r.source_value))}</td>
        <td class="key">${escapeHtml(r.target_key)}</td><td>${escapeHtml(JSON.stringify(r.target_value))}</td>
        <td class="${cls}">${escapeHtml(r.result)}${r.warnings?.length ? " ⚠" : ""}</td>`;
      body.appendChild(tr);
    });
    $("board-message").textContent = "";
  } finally {
    $("board-test").disabled = false;
  }
}

// ---------------------------------------------------------------- conflicts

async function loadConflicts() {
  const box = $("conflict-groups");
  let data;
  try {
    const res = await fetch("/api/conflicts/all");
    data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.status);
  } catch (e) {
    $("conflicts-message").textContent = `Could not load conflicts: ${e.message || e}`;
    return;
  }
  box.innerHTML = "";
  $("conflicts-message").textContent = data.groups.length ? "" : "No conflicts waiting.";
  data.groups.forEach((group) => {
    const section = document.createElement("div");
    section.className = "conflict-group";
    section.innerHTML = `<h3>${escapeHtml(group.label)} <span class="muted">- ${group.conflicts.length} ${group.conflicts.length === 1 ? "conflict" : "conflicts"}</span></h3>`;
    group.conflicts.forEach((c) => {
      const item = document.createElement("div");
      item.className = "conflict";
      item.innerHTML = `<div><strong>${escapeHtml(c.field_label)}</strong> <span class="muted">dive ${escapeHtml(c.dive_time)}</span></div>
        <table class="about-table">
          <tr><th>${escapeHtml(c.source_name)}</th><td>${escapeHtml(c.source_text)}</td>
              <td><button type="button" class="secondary keep-source" title="Writes this value to ${escapeHtml(c.target_name)}">Keep this</button></td></tr>
          <tr><th>${escapeHtml(c.target_name)}</th><td>${escapeHtml(c.target_text)}</td>
              <td><button type="button" class="secondary keep-target" title="Writes this value to ${escapeHtml(c.source_name)}">Keep this</button></td></tr>
        </table>`;
      item.querySelector(".keep-source").addEventListener("click", () => resolveConflict(c.pair_id, c.id, "source"));
      item.querySelector(".keep-target").addEventListener("click", () => resolveConflict(c.pair_id, c.id, "target"));
      section.appendChild(item);
    });
    box.appendChild(section);
  });
}

async function resolveConflict(pairId, id, winner) {
  $("conflicts-message").textContent = "Updating the service…";
  const res = await fetch(`/api/conflicts/${encodeURIComponent(id)}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ winner, pair: pairId }),
  });
  const data = await res.json();
  await loadConflicts();
  $("conflicts-message").textContent = res.ok ? "Resolved." : (data.detail || "Failed.");
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

// ---------------------------------------------------------------- Garmin dives + FIT files

// ---------------------------------------------------------------- init

async function init() {
  document.querySelectorAll(".nav-item").forEach((el) => el.addEventListener("click", () => selectPage(el.dataset.page)));
  let startPage = "sync";
  try { startPage = localStorage.getItem("dive_sync_page") || "sync"; } catch (e) { /* private mode etc. */ }
  selectPage(startPage);

  loadVersion();
  loadAbout();
  $("about-check").addEventListener("click", checkForUpdates);
  loadStatus();
  scheduleStatus(15000);
  connectLogStream();
  // Cron rows read credentialsAccounts to populate their account dropdowns,
  // so it must be loaded before the cron table renders.
  await loadCredentialsStatus();
  await loadEndpoints();
  loadSettings().then(loadFields);
  loadConflicts();

  $("trigger-sync").addEventListener("click", triggerSync);
  $("trigger-source").addEventListener("change", () => fillTargets("trigger"));
  $("trigger-swap").addEventListener("click", () => swapEndpoints("trigger"));
  $("job-source").addEventListener("change", () => fillTargets("job"));
  $("job-frequency").addEventListener("change", showJobTimeFields);
  showJobTimeFields();
  $("job-add").addEventListener("click", applyJobEditor);
  $("job-new").addEventListener("click", () => openJobEditor(-1));
  $("job-cancel").addEventListener("click", () => { editingJob = -1; showJobEditor(false); });
  $("conflicts-reload").addEventListener("click", loadConflicts);
  $("save-sync-settings").addEventListener("click", async () => {
    const { ok, message } = await postSettings(settingsPayload());
    $("sync-settings-message").textContent = message;
    if (ok) { await loadSettings(); await loadFields(); await loadEndpoints(); }
  });
  $("credentials-form").addEventListener("submit", saveCredentials);
  $("test-credentials").addEventListener("click", testCredentials);
  $("garmin-add-account").addEventListener("click", () => addAccountRow("garmin"));
  $("divelogs-add-account").addEventListener("click", () => addAccountRow("divelogs"));
  $("add-pair").addEventListener("click", () => addPairRow());
  $("save-schedule").addEventListener("click", saveSchedule);
  $("notify-save").addEventListener("click", saveNotifyUrl);
  $("notify-test").addEventListener("click", testNotifyUrl);

  $("board-source").addEventListener("change", (e) => {
    const targets = boardTargetsFor(e.target.value).map(([id]) => id);
    const keep = $("board-target").value;
    showView(e.target.value, targets.includes(keep) ? keep : targets[0]);
  });
  $("board-target").addEventListener("change", (e) => showView($("board-source").value, e.target.value));
  $("board-swap").addEventListener("click", () => {
    const target = currentReceiver();
    if (target) showView(target, otherSide(target));
  });
  $("board-save").addEventListener("click", saveBoard);
  $("board-cancel").addEventListener("click", cancelBoard);
  $("board-reset").addEventListener("click", resetBoard);
  $("board-test").addEventListener("click", testBoard);
  $("editor-apply").addEventListener("click", applyEditor);
  $("editor-close").addEventListener("click", closeEditor);
  $("editor-delete").addEventListener("click", () => { if (board.selected) deleteRule(board.selected.receiver, board.selected.id); });
  $("editor-insert").addEventListener("click", insertFieldIntoTemplate);
  $("editor-template").addEventListener("input", schedulePreview);
  $("editor-reverse").addEventListener("input", schedulePreview);
  $("editor-template-toggle").addEventListener("click", () => {
    $("editor-template-block").hidden = false;
    $("editor-template-toggle").hidden = true;
    schedulePreview();
  });
  $("editor-split").addEventListener("change", () => {
    $("editor-split-options").hidden = !$("editor-split").checked;
    schedulePreview();
  });
  $("match-key-add").addEventListener("click", addMatchKey);
  $("pair-direction").addEventListener("change", renderBoard);
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
