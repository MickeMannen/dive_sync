const $ = (id) => document.getElementById(id);

let currentSettings = null;

async function loadVersion() {
  try {
    const res = await fetch("/api/version");
    const data = await res.json();
    $("version").textContent = data.current_version ? `v${data.current_version}` : "";
  } catch (e) {
    // non-fatal
  }
}

async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    $("status-running").textContent = data.is_running ? "Yes" : "No";
    $("status-next").textContent = data.next_scheduled_run
      ? new Date(data.next_scheduled_run).toLocaleString()
      : "Not scheduled";
    $("status-last").textContent = data.last_results && Object.keys(data.last_results).length
      ? JSON.stringify(data.last_results)
      : "No sync has run yet";
  } catch (e) {
    // non-fatal, retry on next poll
  }
}

async function triggerSync() {
  $("trigger-message").textContent = "Starting...";
  const res = await fetch("/api/sync/trigger", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: $("trigger-dry-run").checked }),
  });
  const data = await res.json();
  $("trigger-message").textContent = res.ok ? "Sync triggered." : (data.detail || "Failed to trigger sync.");
  loadStatus();
}

function appendLogLine(line) {
  const el = $("log-tail");
  el.textContent += line + "\n";
  el.scrollTop = el.scrollHeight;
}

function connectLogStream() {
  const source = new EventSource("/api/logs/stream");
  source.onmessage = (event) => appendLogLine(event.data);
  source.onerror = () => {
    source.close();
    setTimeout(connectLogStream, 3000);
  };
}

async function loadCredentialsStatus() {
  const res = await fetch("/api/credentials/status");
  const data = await res.json();
  $("garmin-username").value = data.garmin_username || "";
  $("divelogs-username").value = data.divelogs_username || "";
  setBadge("garmin-configured", data.garmin_configured);
  setBadge("divelogs-configured", data.divelogs_configured);
}

function setBadge(id, on) {
  const el = $(id);
  el.textContent = on ? "configured" : "not configured";
  el.classList.toggle("on", !!on);
}

function credentialsPayload() {
  return {
    garmin_username: $("garmin-username").value,
    garmin_password: $("garmin-password").value,
    garmin_token_dir: $("garmin-token-dir").value || "tokens/garmin",
    divelogs_username: $("divelogs-username").value,
    divelogs_password: $("divelogs-password").value,
  };
}

async function saveCredentials(event) {
  event.preventDefault();
  const res = await fetch("/api/credentials", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentialsPayload()),
  });
  const data = await res.json();
  $("credentials-message").textContent = res.ok ? "Saved." : (data.detail || "Failed to save.");
  await loadCredentialsStatus();
}

async function testCredentials() {
  $("credentials-message").textContent = "Testing...";
  const res = await fetch("/api/credentials/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentialsPayload()),
  });
  const data = await res.json();
  const parts = [];
  if (data.garmin !== null) parts.push(`Garmin: ${data.garmin ? "OK" : "failed"}`);
  if (data.divelogs !== null) parts.push(`Divelogs: ${data.divelogs ? "OK" : "failed"}`);
  $("credentials-message").textContent = parts.join(" · ") || "Enter credentials to test.";
}

function populateDefaults(settings) {
  $("default-directionality").value = settings.directionality;
  $("grace-window").value = settings.grace_window_minutes;
  $("api-cooldown").value = settings.api_cooldown_seconds;
  $("default-only-new").checked = settings.sync_filters.only_new;
  $("default-sync-gases").checked = settings.sync_filters.sync_gases;
  $("default-sync-fit").checked = settings.sync_filters.sync_fit;
}

function cronRowHtml(job) {
  return `
    <td><input class="f-id" value="${job.id}"></td>
    <td>
      <select class="f-direction">
        <option value="bidirectional" ${job.directionality === "bidirectional" ? "selected" : ""}>Bidirectional</option>
        <option value="to_divelogs" ${job.directionality === "to_divelogs" ? "selected" : ""}>To Divelogs</option>
        <option value="to_garmin" ${job.directionality === "to_garmin" ? "selected" : ""}>To Garmin</option>
      </select>
    </td>
    <td>
      <select class="f-frequency">
        <option value="hourly" ${job.frequency === "hourly" ? "selected" : ""}>Hourly</option>
        <option value="daily" ${job.frequency === "daily" ? "selected" : ""}>Daily</option>
        <option value="weekly" ${job.frequency === "weekly" ? "selected" : ""}>Weekly</option>
        <option value="custom_minutes" ${job.frequency === "custom_minutes" ? "selected" : ""}>Every N minutes</option>
      </select>
    </td>
    <td><input class="f-hour" type="number" min="0" max="23" value="${job.hour}"></td>
    <td><input class="f-minute" type="number" min="0" max="59" value="${job.minute}"></td>
    <td><input class="f-day" type="number" min="0" max="6" value="${job.day_of_week}"></td>
    <td><input class="f-interval" type="number" min="1" value="${job.interval_minutes}"></td>
    <td><input class="f-only-new" type="checkbox" ${job.only_new ? "checked" : ""}></td>
    <td><input class="f-gases" type="checkbox" ${job.sync_gases ? "checked" : ""}></td>
    <td><input class="f-fit" type="checkbox" ${job.sync_fit ? "checked" : ""}></td>
    <td><input class="f-enabled" type="checkbox" ${job.enabled ? "checked" : ""}></td>
    <td><button type="button" class="secondary remove-row">✕</button></td>
  `;
}

function addCronRow(job) {
  const defaultJob = {
    id: `job-${Date.now()}`,
    directionality: "bidirectional",
    frequency: "daily",
    hour: 0,
    minute: 0,
    day_of_week: 0,
    interval_minutes: 60,
    only_new: true,
    sync_gases: true,
    sync_fit: false,
    enabled: true,
  };
  const row = document.createElement("tr");
  row.innerHTML = cronRowHtml(job || defaultJob);
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
    directionality: row.querySelector(".f-direction").value,
    frequency: row.querySelector(".f-frequency").value,
    hour: parseInt(row.querySelector(".f-hour").value, 10),
    minute: parseInt(row.querySelector(".f-minute").value, 10),
    day_of_week: parseInt(row.querySelector(".f-day").value, 10),
    interval_minutes: parseInt(row.querySelector(".f-interval").value, 10),
    only_new: row.querySelector(".f-only-new").checked,
    sync_gases: row.querySelector(".f-gases").checked,
    sync_fit: row.querySelector(".f-fit").checked,
    enabled: row.querySelector(".f-enabled").checked,
  }));
}

async function loadSettings() {
  const res = await fetch("/api/settings");
  currentSettings = await res.json();
  populateDefaults(currentSettings);
  populateCronTable(currentSettings.cron_jobs || []);
}

async function saveSchedule() {
  const payload = {
    directionality: $("default-directionality").value,
    sync_filters: {
      date_from: currentSettings?.sync_filters?.date_from ?? null,
      date_to: currentSettings?.sync_filters?.date_to ?? null,
      only_new: $("default-only-new").checked,
      sync_gases: $("default-sync-gases").checked,
      sync_fit: $("default-sync-fit").checked,
    },
    grace_window_minutes: parseInt($("grace-window").value, 10),
    api_cooldown_seconds: parseFloat($("api-cooldown").value),
    schedule: (currentSettings?.schedule || []).map((s) => ({ hour: s.hour, minute: s.minute })),
    cron_jobs: readCronTable(),
  };

  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  $("schedule-message").textContent = res.ok ? "Saved." : (data.detail || "Failed to save.");
  if (res.ok) await loadSettings();
}

function init() {
  loadVersion();
  loadStatus();
  setInterval(loadStatus, 15000);
  connectLogStream();
  loadCredentialsStatus();
  loadSettings();

  $("trigger-sync").addEventListener("click", triggerSync);
  $("credentials-form").addEventListener("submit", saveCredentials);
  $("test-credentials").addEventListener("click", testCredentials);
  $("add-cron-job").addEventListener("click", () => addCronRow());
  $("save-schedule").addEventListener("click", saveSchedule);
}

document.addEventListener("DOMContentLoaded", init);
