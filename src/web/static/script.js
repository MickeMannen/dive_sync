document.addEventListener("DOMContentLoaded", () => {
    // DOM Elements
    const settingsForm = document.getElementById("settings-form");
    const triggerSyncBtn = document.getElementById("trigger-sync-btn");
    const triggerDryRunBtn = document.getElementById("trigger-dry-run-btn");
    const logTerminal = document.getElementById("log-terminal");
    const clearLogBtn = document.getElementById("clear-log-btn");
    const autoscrollCheckbox = document.getElementById("autoscroll-checkbox");
    const globalStatusText = document.getElementById("global-status-text");
    const statusDot = document.querySelector(".status-dot");
    const garminStatusEl = document.getElementById("garmin-status");
    const divelogsStatusEl = document.getElementById("divelogs-status");
    const syncAllDatesCheckbox = document.getElementById("sync_all_dates");
    const dateFromInput = document.getElementById("date_from");
    const dateToInput = document.getElementById("date_to");
    
    // Scheduler Elements
    const showAddCronBtn = document.getElementById("show-add-cron-btn");
    const addCronContainer = document.getElementById("add-cron-container");
    const addCronForm = document.getElementById("add-cron-form");
    const cancelCronBtn = document.getElementById("cancel-cron-btn");
    const cronJobsList = document.getElementById("cron-jobs-list");
    const cronFrequencySelect = document.getElementById("cron-frequency");
    
    const cronDowGroup = document.getElementById("cron-dow-group");
    const cronTimeGroup = document.getElementById("cron-time-group");
    const cronHourlyGroup = document.getElementById("cron-hourly-group");
    const cronIntervalGroup = document.getElementById("cron-interval-group");

    let cronJobs = [];
    let legacyScheduleSlots = [];

    // 1. Load Settings and Populate Form
    async function loadSettings() {
        try {
            const response = await fetch("/api/settings");
            if (!response.ok) throw new Error("Failed to load settings.");
            
            const settings = await response.json();
            
            // Populate inputs
            document.getElementById("directionality").value = settings.directionality;
            const hasNoDates = !settings.sync_filters.date_from && !settings.sync_filters.date_to;
            if (syncAllDatesCheckbox) {
                syncAllDatesCheckbox.checked = hasNoDates;
            }
            dateFromInput.value = settings.sync_filters.date_from || "";
            dateToInput.value = settings.sync_filters.date_to || "";
            dateFromInput.disabled = hasNoDates;
            dateToInput.disabled = hasNoDates;
            document.getElementById("only_new").checked = settings.sync_filters.only_new;
            document.getElementById("sync_gases").checked = settings.sync_filters.sync_gases;
            document.getElementById("sync_fit").checked = settings.sync_filters.sync_fit;
            document.getElementById("grace_window_minutes").value = settings.grace_window_minutes;
            document.getElementById("api_cooldown_seconds").value = settings.api_cooldown_seconds;
            
            // Load schedules
            legacyScheduleSlots = settings.schedule || [];
            cronJobs = settings.cron_jobs || [];
            renderCronJobs();
        } catch (err) {
            appendLogLine(`[ERROR] Failed to load configurations: ${err.message}`, "error");
        }
    }

    // 2. Load Credentials Status
    async function loadCredentialsStatus() {
        try {
            const response = await fetch("/api/credentials/status");
            const status = await response.json();
            
            updateCredentialBadge(
                garminStatusEl,
                status.garmin_configured,
                status.garmin_username,
                status.garmin_accounts
            );
            updateCredentialBadge(
                divelogsStatusEl,
                status.divelogs_configured,
                status.divelogs_username,
                status.divelogs_accounts
            );
        } catch (err) {
            appendLogLine(`[ERROR] Failed to fetch credential status: ${err.message}`, "error");
        }
    }

    function updateCredentialBadge(element, configured, username = "", accounts = []) {
        if (!element) return;
        const badge = element.querySelector(".badge");
        const stateText = element.querySelector(".service-state");
        
        if (configured) {
            badge.className = "badge green";
            badge.textContent = "Active";
            if (accounts && accounts.length > 1) {
                stateText.innerHTML = `Account: <span style="color: var(--color-text-main, #f8fafc); font-weight: 500;">${escapeHtml(accounts.join(", "))}</span>`;
            } else if (username) {
                stateText.innerHTML = `Account: <span style="color: var(--color-text-main, #f8fafc); font-weight: 500;">${escapeHtml(username)}</span>`;
            } else {
                stateText.textContent = "Credentials loaded and ready.";
            }
        } else {
            badge.className = "badge red";
            badge.textContent = "Unconfigured";
            stateText.textContent = "Click Configure to set up.";
        }
    }

    // 3. Render Cron Jobs
    function renderCronJobs() {
        cronJobsList.innerHTML = "";
        if (cronJobs.length === 0) {
            cronJobsList.innerHTML = `<li class="empty-state text-subtle">No schedules configured.</li>`;
            return;
        }

        cronJobs.forEach((job) => {
            const li = document.createElement("li");
            li.className = "schedule-item";
            li.style.display = "flex";
            li.style.flexDirection = "column";
            li.style.gap = "0.5rem";
            li.style.padding = "1rem";
            li.style.borderBottom = "1px solid rgba(255, 255, 255, 0.05)";

            let freqText = "";
            if (job.frequency === "hourly") {
                freqText = `Hourly at :${String(job.minute).padStart(2, '0')}`;
            } else if (job.frequency === "daily") {
                freqText = `Daily at ${String(job.hour).padStart(2, '0')}:${String(job.minute).padStart(2, '0')}`;
            } else if (job.frequency === "weekly") {
                const days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
                freqText = `Weekly on ${days[job.day_of_week]} at ${String(job.hour).padStart(2, '0')}:${String(job.minute).padStart(2, '0')}`;
            } else if (job.frequency === "custom_minutes") {
                freqText = `Every ${job.interval_minutes} minutes`;
            }

            let dirText = "Bidirectional";
            if (job.directionality === "to_divelogs") dirText = "Garmin ➔ Divelogs";
            if (job.directionality === "to_garmin") dirText = "Divelogs ➔ Garmin";

            let badges = `<span style="font-size: 0.7rem; padding: 0.1rem 0.35rem; background: rgba(99, 102, 241, 0.15); border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 4px; color: var(--color-primary); font-weight: 600;">${dirText}</span>`;
            if (job.only_new) badges += ` <span style="font-size: 0.7rem; padding: 0.1rem 0.35rem; background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 4px; color: var(--color-text-subtle);">Incremental</span>`;
            if (job.sync_gases) badges += ` <span style="font-size: 0.7rem; padding: 0.1rem 0.35rem; background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 4px; color: var(--color-text-subtle);">Gases</span>`;
            if (job.sync_fit) badges += ` <span style="font-size: 0.7rem; padding: 0.1rem 0.35rem; background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 4px; color: var(--color-text-subtle);">FIT</span>`;

            li.innerHTML = `
                <div style="display: flex; justify-content: space-between; align-items: flex-start; width: 100%;">
                    <div>
                        <strong style="color: var(--color-text-main, #f8fafc); font-size: 0.95rem;">${escapeHtml(job.id)}</strong>
                        <div style="color: var(--color-text-secondary); font-size: 0.85rem; margin-top: 0.25rem;">
                            🕒 ${freqText}
                        </div>
                    </div>
                    <div style="display: flex; align-items: center; gap: 0.75rem;">
                        <label class="switch" style="transform: scale(0.85);">
                            <input type="checkbox" class="job-toggle" ${job.enabled ? "checked" : ""}>
                            <span class="slider round"></span>
                        </label>
                        <button class="delete-job-btn text-btn" style="color: var(--color-danger); font-size: 1.1rem; padding: 0 0.5rem; background: none; border: none; cursor: pointer;">&times;</button>
                    </div>
                </div>
                <div style="display: flex; gap: 0.5rem; margin-top: 0.25rem; flex-wrap: wrap;">
                    ${badges}
                </div>
            `;

            li.querySelector(".job-toggle").addEventListener("change", async (e) => {
                job.enabled = e.target.checked;
                const ok = await saveAllSettings();
                if (!ok) {
                    e.target.checked = !e.target.checked;
                    job.enabled = !e.target.checked;
                }
            });

            li.querySelector(".delete-job-btn").addEventListener("click", async () => {
                if (confirm(`Are you sure you want to delete the cron job "${job.id}"?`)) {
                    const index = cronJobs.indexOf(job);
                    if (index > -1) {
                        cronJobs.splice(index, 1);
                        const ok = await saveAllSettings();
                        if (ok) {
                            renderCronJobs();
                        } else {
                            cronJobs.splice(index, 0, job);
                        }
                    }
                }
            });

            cronJobsList.appendChild(li);
        });
    }

    // Toggle frequency specific fields
    cronFrequencySelect.addEventListener("change", () => {
        const val = cronFrequencySelect.value;
        cronDowGroup.classList.add("hidden");
        cronTimeGroup.classList.add("hidden");
        cronHourlyGroup.classList.add("hidden");
        cronIntervalGroup.classList.add("hidden");

        if (val === "hourly") {
            cronHourlyGroup.classList.remove("hidden");
        } else if (val === "daily") {
            cronTimeGroup.classList.remove("hidden");
        } else if (val === "weekly") {
            cronDowGroup.classList.remove("hidden");
            cronTimeGroup.classList.remove("hidden");
        } else if (val === "custom_minutes") {
            cronIntervalGroup.classList.remove("hidden");
        }
    });

    showAddCronBtn.addEventListener("click", () => {
        addCronContainer.classList.remove("hidden");
        showAddCronBtn.classList.add("hidden");
        document.getElementById("cron-job-id").value = "";
        document.getElementById("cron-direction").value = "bidirectional";
        cronFrequencySelect.value = "daily";
        cronFrequencySelect.dispatchEvent(new Event("change"));
        document.getElementById("cron-time").value = "00:00";
        document.getElementById("cron-minute-past").value = "0";
        document.getElementById("cron-interval").value = "60";
        document.getElementById("cron-only-new").checked = true;
        document.getElementById("cron-sync-gases").checked = true;
        document.getElementById("cron-sync-fit").checked = false;
    });

    cancelCronBtn.addEventListener("click", () => {
        addCronContainer.classList.add("hidden");
        showAddCronBtn.classList.remove("hidden");
    });

    addCronForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const jobId = document.getElementById("cron-job-id").value.trim();
        if (!jobId) return;

        if (cronJobs.some(j => j.id.toLowerCase() === jobId.toLowerCase())) {
            alert(`A cron job with ID "${jobId}" already exists.`);
            return;
        }

        const frequency = cronFrequencySelect.value;
        const directionality = document.getElementById("cron-direction").value;
        
        let hour = 0;
        let minute = 0;
        let dayOfWeek = 0;
        let intervalMinutes = 60;

        if (frequency === "hourly") {
            minute = parseInt(document.getElementById("cron-minute-past").value) || 0;
        } else if (frequency === "daily") {
            const timeVal = document.getElementById("cron-time").value || "00:00";
            const [h, m] = timeVal.split(":");
            hour = parseInt(h) || 0;
            minute = parseInt(m) || 0;
        } else if (frequency === "weekly") {
            dayOfWeek = parseInt(document.getElementById("cron-dow").value) || 0;
            const timeVal = document.getElementById("cron-time").value || "00:00";
            const [h, m] = timeVal.split(":");
            hour = parseInt(h) || 0;
            minute = parseInt(m) || 0;
        } else if (frequency === "custom_minutes") {
            intervalMinutes = parseInt(document.getElementById("cron-interval").value) || 60;
        }

        const onlyNew = document.getElementById("cron-only-new").checked;
        const syncGases = document.getElementById("cron-sync-gases").checked;
        const syncFit = document.getElementById("cron-sync-fit").checked;

        const newJob = {
            id: jobId,
            directionality: directionality,
            frequency: frequency,
            hour: hour,
            minute: minute,
            day_of_week: dayOfWeek,
            interval_minutes: intervalMinutes,
            only_new: onlyNew,
            sync_gases: syncGases,
            sync_fit: syncFit,
            enabled: true
        };

        cronJobs.push(newJob);
        
        const ok = await saveAllSettings();
        if (ok) {
            renderCronJobs();
            addCronContainer.classList.add("hidden");
            showAddCronBtn.classList.remove("hidden");
        } else {
            cronJobs.pop();
        }
    });

    async function saveAllSettings() {
        const payload = {
            directionality: document.getElementById("directionality").value,
            sync_filters: {
                date_from: (syncAllDatesCheckbox && syncAllDatesCheckbox.checked) ? null : (dateFromInput.value || null),
                date_to: (syncAllDatesCheckbox && syncAllDatesCheckbox.checked) ? null : (dateToInput.value || null),
                only_new: document.getElementById("only_new").checked,
                sync_gases: document.getElementById("sync_gases").checked,
                sync_fit: document.getElementById("sync_fit").checked
            },
            grace_window_minutes: parseInt(document.getElementById("grace_window_minutes").value) || 15,
            api_cooldown_seconds: parseFloat(document.getElementById("api_cooldown_seconds").value) || 1.0,
            schedule: legacyScheduleSlots,
            cron_jobs: cronJobs
        };

        try {
            const response = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });

            if (response.ok) {
                appendLogLine("[SYSTEM] Configuration updated successfully.", "system-msg");
                return true;
            } else {
                const errData = await response.json();
                throw new Error(errData.detail || "Failed to save settings.");
            }
        } catch (err) {
            appendLogLine(`[ERROR] Failed to save settings: ${err.message}`, "error");
            alert("Failed to save settings: " + err.message);
            return false;
        }
    }

    // 4. Save Settings Form
    settingsForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const ok = await saveAllSettings();
        if (ok) {
            alert("Settings saved successfully.");
        }
    });

    // 5. Stream Logs
    function connectLogStream() {
        const eventSource = new EventSource("/api/logs/stream");
        
        eventSource.onmessage = (event) => {
            const line = event.data;
            let type = "info";
            
            if (line.includes("[ERROR]") || line.includes("failed")) {
                type = "error";
            } else if (line.includes("[WARNING]")) {
                type = "warning";
            } else if (line.includes("[SYSTEM]") || line.includes("Completed") || line.includes("success")) {
                type = "system-msg";
            }
            
            appendLogLine(line, type);
        };

        eventSource.onerror = () => {
            // Silently attempt reconnection in 5s
            eventSource.close();
            setTimeout(connectLogStream, 5000);
        };
    }

    function appendLogLine(text, type = "info") {
        const div = document.createElement("div");
        div.className = `terminal-line ${type}`;
        div.textContent = text;
        logTerminal.appendChild(div);
        
        if (autoscrollCheckbox.checked) {
            logTerminal.scrollTop = logTerminal.scrollHeight;
        }

        // Update bottom footer telemetry ticker
        const ticker = document.getElementById("footer-log-ticker");
        if (ticker) {
            ticker.textContent = text;
            // Map types to classes
            if (type === "error") {
                ticker.className = "text-red-400 font-medium truncate";
            } else if (type === "warning") {
                ticker.className = "text-amber-400 font-medium truncate";
            } else if (type === "system-msg") {
                ticker.className = "text-laser-cyan italic truncate";
            } else {
                ticker.className = "text-[#94a3b8] truncate";
            }
        }
    }

    clearLogBtn.addEventListener("click", () => {
        logTerminal.innerHTML = "";
    });

    // 6. Manual Sync execution Trigger
    async function executeSync(isDryRun) {
        triggerSyncBtn.disabled = true;
        if (triggerDryRunBtn) triggerDryRunBtn.disabled = true;
        
        const activeBtn = isDryRun ? triggerDryRunBtn : triggerSyncBtn;
        if (activeBtn) {
            activeBtn.querySelector(".btn-text").textContent = isDryRun ? "Running Dry Run..." : "Running Sync...";
            activeBtn.querySelector(".spinner")?.classList.remove("hidden");
        }
        
        statusDot.className = "status-dot orange";
        globalStatusText.textContent = isDryRun ? "Dry-running..." : "Syncing...";

        const payload = {
            dry_run: isDryRun,
            directionality: document.getElementById("directionality").value,
            date_from: (syncAllDatesCheckbox && syncAllDatesCheckbox.checked) ? null : (dateFromInput.value || null),
            date_to: (syncAllDatesCheckbox && syncAllDatesCheckbox.checked) ? null : (dateToInput.value || null),
            only_new: document.getElementById("only_new").checked,
            sync_gases: document.getElementById("sync_gases").checked,
            sync_fit: document.getElementById("sync_fit").checked
        };

        try {
            const response = await fetch("/api/sync/trigger", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            
            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || "Failed to trigger sync.");
            }
            
            appendLogLine(`[SYSTEM] Synchronization job triggered (Dry Run: ${isDryRun}). Check output console below.`, "system-msg");
            pollSyncStatus();
        } catch (err) {
            appendLogLine(`[ERROR] ${err.message}`, "error");
            resetSyncButtons();
        }
    }

    triggerSyncBtn.addEventListener("click", () => executeSync(false));
    if (triggerDryRunBtn) {
        triggerDryRunBtn.addEventListener("click", () => executeSync(true));
    }

    if (syncAllDatesCheckbox) {
        syncAllDatesCheckbox.addEventListener("change", () => {
            const isChecked = syncAllDatesCheckbox.checked;
            dateFromInput.disabled = isChecked;
            dateToInput.disabled = isChecked;
            if (isChecked) {
                dateFromInput.value = "";
                dateToInput.value = "";
            }
        });
    }

    async function pollSyncStatus() {
        const interval = setInterval(async () => {
            try {
                const response = await fetch("/api/sync/status");
                const state = await response.json();
                
                if (!state.is_running) {
                    clearInterval(interval);
                    resetSyncButtons();
                    
                    if (state.last_results.error) {
                        appendLogLine(`[ERROR] Sync Job failed: ${state.last_results.error}`, "error");
                    } else {
                        appendLogLine("[SYSTEM] Synchronization run complete.", "system-msg");
                    }
                }
            } catch (err) {
                console.error("Error polling sync status:", err);
            }
        }, 1500);
    }

    function resetSyncButtons() {
        triggerSyncBtn.disabled = false;
        triggerSyncBtn.querySelector(".btn-text").textContent = "Run Sync Now";
        triggerSyncBtn.querySelector(".spinner")?.classList.add("hidden");
        
        if (triggerDryRunBtn) {
            triggerDryRunBtn.disabled = false;
            triggerDryRunBtn.querySelector(".btn-text").textContent = "Dry Run Now";
            triggerDryRunBtn.querySelector(".spinner")?.classList.add("hidden");
        }
        
        statusDot.className = "status-dot green";
        globalStatusText.textContent = "System Ready";
    }

    // 7. Load and Render Dives Explorer
    function renderExplorerLists(data) {
        const garminList = document.getElementById("garmin-dives-list");
        const divelogsList = document.getElementById("divelogs-dives-list");
        const garminCount = document.getElementById("garmin-count");
        const divelogsCount = document.getElementById("divelogs-count");

        if (!garminList || !divelogsList) return;

        // Update header telemetry total
        const headerTotal = document.getElementById("header-total-dives");
        if (headerTotal) {
            headerTotal.textContent = data.garmin.length + data.divelogs.length;
        }

        // Render Garmin
        garminList.innerHTML = "";
        garminCount.textContent = data.garmin.length;
        if (data.garmin.length === 0) {
            garminList.innerHTML = `<div class="empty-state text-subtle">No cached Garmin dives found. Please run sync or download raw data.</div>`;
        } else {
            data.garmin.forEach(dive => {
                const card = createDiveCard(dive, "garmin");
                garminList.appendChild(card);
            });
        }

        // Render Divelogs
        divelogsList.innerHTML = "";
        divelogsCount.textContent = data.divelogs.length;
        if (data.divelogs.length === 0) {
            divelogsList.innerHTML = `<div class="empty-state text-subtle">No cached Divelogs dives found. Please run sync or download raw data.</div>`;
        } else {
            data.divelogs.forEach(dive => {
                const card = createDiveCard(dive, "divelogs");
                divelogsList.appendChild(card);
            });
        }
    }

    async function loadDives() {
        const garminList = document.getElementById("garmin-dives-list");
        const divelogsList = document.getElementById("divelogs-dives-list");
        
        if (garminList) garminList.innerHTML = `<div class="empty-state text-subtle">Loading Garmin dives...</div>`;
        if (divelogsList) divelogsList.innerHTML = `<div class="empty-state text-subtle">Loading Divelogs dives...</div>`;

        try {
            const response = await fetch("/api/dives");
            if (!response.ok) throw new Error("Failed to load dives.");
            const data = await response.json();
            renderExplorerLists(data);
        } catch (err) {
            console.error("Error loading dives:", err);
            if (garminList) garminList.innerHTML = `<div class="empty-state text-subtle" style="color: var(--color-danger)">Error: ${err.message}</div>`;
            if (divelogsList) divelogsList.innerHTML = `<div class="empty-state text-subtle" style="color: var(--color-danger)">Error: ${err.message}</div>`;
        }
    }

    function createDiveCard(dive, type) {
        const card = document.createElement("div");
        card.className = "dive-item-card";
        card.style.cursor = "pointer";

        card.addEventListener("click", () => {
            showDiveDetails(type, dive);
        });

        const maxDepthM = parseFloat(dive.max_depth || 0);
        const durationMin = Math.round(parseInt(dive.duration || 0) / 60);

        const cleanNotes = (dive.notes && dive.notes !== "None") ? dive.notes : "";
        const cleanWeight = (dive.weight && dive.weight !== "None") ? dive.weight : "";
        const cleanVis = (dive.visibility && dive.visibility !== "None") ? dive.visibility : "";

        card.innerHTML = `
            <div class="dive-card-header">
                <span class="dive-title">${dive.location || "Unknown Location"}</span>
                <span class="dive-meta-date">${dive.date_time}</span>
            </div>
            <div class="dive-card-details">
                <span><span class="dive-detail-label">Dive:</span><span class="dive-detail-val">#${dive.dive_number}</span></span>
                <span><span class="dive-detail-label">Depth:</span><span class="dive-detail-val">${maxDepthM.toFixed(1)}m</span></span>
                <span><span class="dive-detail-label">Duration:</span><span class="dive-detail-val">${durationMin} min</span></span>
                ${cleanWeight ? `<span><span class="dive-detail-label">Weight:</span><span class="dive-detail-val">${cleanWeight}</span></span>` : ""}
                ${cleanVis ? `<span><span class="dive-detail-label">Vis:</span><span class="dive-detail-val">${cleanVis}</span></span>` : ""}
            </div>
            ${cleanNotes ? `<p class="dive-notes" title="${escapeHtml(cleanNotes)}">${escapeHtml(cleanNotes)}</p>` : ""}
        `;
        return card;
    }

    // Modal Control Logic
    const detailsModal = document.getElementById("dive-details-modal");
    const modalCloseBtn = document.getElementById("modal-close-btn");
    const modalTitle = document.getElementById("modal-dive-title");
    const modalTabFancy = document.getElementById("modal-tab-fancy");
    const modalTabRaw = document.getElementById("modal-tab-raw");
    const modalContentFancy = document.getElementById("modal-content-fancy");
    const modalContentRaw = document.getElementById("modal-content-raw");
    const modalRawJson = document.getElementById("modal-raw-json");

    if (modalCloseBtn) {
        modalCloseBtn.addEventListener("click", () => {
            detailsModal.classList.add("hidden");
        });
    }

    if (detailsModal) {
        detailsModal.addEventListener("click", (e) => {
            if (e.target === detailsModal) {
                detailsModal.classList.add("hidden");
            }
        });
    }

    if (modalTabFancy) {
        modalTabFancy.addEventListener("click", () => {
            modalTabFancy.classList.add("active");
            modalTabRaw.classList.remove("active");
            modalContentFancy.classList.remove("hidden");
            modalContentRaw.classList.add("hidden");
        });
    }

    if (modalTabRaw) {
        modalTabRaw.addEventListener("click", () => {
            modalTabRaw.classList.add("active");
            modalTabFancy.classList.remove("active");
            modalContentRaw.classList.remove("hidden");
            modalContentFancy.classList.add("hidden");
        });
    }

    async function showDiveDetails(service, dive) {
        if (!detailsModal) return;
        
        detailsModal.classList.remove("hidden");
        
        modalTabFancy.classList.add("active");
        modalTabRaw.classList.remove("active");
        modalContentFancy.classList.remove("hidden");
        modalContentRaw.classList.add("hidden");
        
        modalTitle.textContent = `Dive #${dive.dive_number} Details (${service.toUpperCase()})`;
        
        const durationMin = Math.round(parseInt(dive.duration || 0) / 60);
        const maxDepthM = parseFloat(dive.max_depth || 0).toFixed(1);
        
        const cleanNotes = (dive.notes && dive.notes !== "None") ? dive.notes : "No description provided.";
        const cleanBuddy = (dive.buddy && dive.buddy !== "None") ? dive.buddy : "None";
        const cleanWeight = (dive.weight && dive.weight !== "None") ? dive.weight : "None";
        const cleanVis = (dive.visibility && dive.visibility !== "None") ? dive.visibility : "None";

        modalContentFancy.innerHTML = `
            <div class="detail-grid">
                <div class="detail-item">
                    <span class="detail-label">Service Provider</span>
                    <span class="detail-value" style="text-transform: capitalize;">${service} Connect</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Dive Number</span>
                    <span class="detail-value highlight">#${dive.dive_number || 'N/A'}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Date & Time</span>
                    <span class="detail-value">${dive.date_time || 'N/A'}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Duration</span>
                    <span class="detail-value">${durationMin} min (${dive.duration || 0} sec)</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Max Depth</span>
                    <span class="detail-value highlight">${maxDepthM} m</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Location / Site</span>
                    <span class="detail-value">${dive.location || 'Unknown Location'}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Buddy</span>
                    <span class="detail-value">${cleanBuddy}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Weight</span>
                    <span class="detail-value">${cleanWeight}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Visibility</span>
                    <span class="detail-value">${cleanVis}</span>
                </div>
                <div class="detail-item span-full">
                    <span class="detail-label">Notes</span>
                    <span class="detail-value notes-val">${escapeHtml(cleanNotes)}</span>
                </div>
            </div>
        `;
        
        modalRawJson.textContent = "Loading raw JSON data...";
        try {
            const res = await fetch(`/api/dives/raw?service=${service}&filename=${dive.filename}`);
            if (!res.ok) throw new Error("Failed to load raw JSON.");
            const rawData = await res.json();
            modalRawJson.textContent = JSON.stringify(rawData, null, 2);
            
            // Extract tanks & gases data from raw JSON
            let tanks = [];
            if (service === "garmin") {
                const info = (rawData.details && rawData.details.diveInfo) || (rawData.summary && rawData.summary.diveInfo) || {};
                const diveGases = info.diveGases || [];
                const tankSensors = (rawData.tanksensor && rawData.tanksensor.tankSensors) || [];
                
                tanks = diveGases.map((gas, idx) => {
                    const o2 = gas.oxygenContent || 21;
                    const he = gas.heliumContent || 0;
                    
                    let startP = gas.tankStartingPressure;
                    let endP = gas.tankEndingPressure;
                    let size = gas.tankSize;
                    let volUsed = null;
                    let name = `Tank #${idx + 1}`;
                    let unitPressure = "bar";
                    let unitSize = "L";
                    
                    // Match with tank sensor data by index or gasIndex
                    const sensor = tankSensors.find(s => s.tankIndex === idx || s.tankIndex === gas.gasIndex);
                    if (sensor) {
                        if (sensor.name) name = sensor.name;
                        if (sensor.startingPressure !== undefined && sensor.startingPressure !== null) {
                            startP = sensor.startingPressure;
                        }
                        if (sensor.endingPressure !== undefined && sensor.endingPressure !== null) {
                            endP = sensor.endingPressure;
                        }
                        if (sensor.volumeUsed !== undefined && sensor.volumeUsed !== null) {
                            volUsed = sensor.volumeUsed;
                        }
                        if (sensor.pressureUnit) {
                            unitPressure = sensor.pressureUnit.toLowerCase();
                        }
                    }
                    
                    return {
                        name: name,
                        o2: o2,
                        he: he,
                        startPressure: startP,
                        endPressure: endP,
                        size: size,
                        volumeUsed: volUsed,
                        unitPressure: unitPressure,
                        unitSize: unitSize
                    };
                });
                
                // Fallback to tankSensors if no diveGases are listed but telemetry exists
                if (tanks.length === 0 && tankSensors.length > 0) {
                    tanks = tankSensors.map((sensor, idx) => {
                        const unitPressure = (sensor.pressureUnit || "bar").toLowerCase();
                        return {
                            name: sensor.name || `Tank #${idx + 1}`,
                            o2: 21,
                            he: 0,
                            startPressure: sensor.startingPressure,
                            endPressure: sensor.endingPressure,
                            size: null,
                            volumeUsed: sensor.volumeUsed,
                            unitPressure: unitPressure,
                            unitSize: "L"
                        };
                    });
                }
            } else if (service === "divelogs") {
                const rawTanks = (rawData.tanks || []).filter(tank => {
                    const hasVol = tank.vol !== undefined && tank.vol !== null && tank.vol !== "";
                    const hasStartP = tank.start_pressure !== undefined && tank.start_pressure !== null && tank.start_pressure !== 0;
                    const hasEndP = tank.end_pressure !== undefined && tank.end_pressure !== null && tank.end_pressure !== 0;
                    const hasTank = tank.tank !== undefined && tank.tank !== null && tank.tank !== "";
                    const hasTankName = tank.tankname !== undefined && tank.tankname !== null && tank.tankname !== "";
                    return hasVol || hasStartP || hasEndP || hasTank || hasTankName;
                });
                tanks = rawTanks.map((tank, idx) => {
                    const o2 = tank.o2 || 21;
                    const he = tank.he || 0;
                    const startP = tank.start_pressure;
                    const endP = tank.end_pressure;
                    const size = tank.vol;
                    
                    const isPsi = (startP > 500 || endP > 500);
                    
                    return {
                        name: `Tank #${idx + 1}`,
                        o2: o2,
                        he: he,
                        startPressure: startP,
                        endPressure: endP,
                        size: size,
                        volumeUsed: null,
                        unitPressure: isPsi ? "psi" : "bar",
                        unitSize: isPsi ? "cu ft" : "L"
                    };
                });
            }
            
            if (tanks.length > 0) {
                let tanksHtml = `
                    <div class="modal-section-divider"></div>
                    <h3 class="modal-section-title">🛡️ Tanks & Gases</h3>
                    <div class="tanks-container">
                `;
                
                tanks.forEach(tank => {
                    let gasType = "Air";
                    if (tank.he > 0) {
                        gasType = `Trimix (${Math.round(tank.o2)}/${Math.round(tank.he)})`;
                    } else if (tank.o2 > 21.5) {
                        gasType = `Nitrox (${Math.round(tank.o2)}%)`;
                    } else if (tank.o2 >= 99) {
                        gasType = "Oxygen (100%)";
                    }
                    
                    let pressureStr = "N/A";
                    if (tank.startPressure !== undefined && tank.startPressure !== null) {
                        pressureStr = `${Math.round(tank.startPressure)}`;
                        if (tank.endPressure !== undefined && tank.endPressure !== null) {
                            pressureStr += ` → ${Math.round(tank.endPressure)} ${tank.unitPressure}`;
                        } else {
                            pressureStr += ` ${tank.unitPressure}`;
                        }
                    }
                    
                    let sizeStr = "N/A";
                    if (tank.size !== undefined && tank.size !== null) {
                        sizeStr = `${tank.size} ${tank.unitSize}`;
                    }
                    
                    let volUsedStr = "N/A";
                    if (tank.volumeUsed !== undefined && tank.volumeUsed !== null) {
                        volUsedStr = `${Math.round(tank.volumeUsed)} ${tank.unitSize}`;
                    } else if (tank.startPressure !== undefined && tank.startPressure !== null &&
                        tank.endPressure !== undefined && tank.endPressure !== null &&
                        tank.size !== undefined && tank.size !== null) {
                        
                        const pDiff = tank.startPressure - tank.endPressure;
                        if (pDiff >= 0) {
                            if (tank.unitPressure === "psi") {
                                const cuftUsed = (pDiff / 3000) * tank.size;
                                volUsedStr = `${cuftUsed.toFixed(1)} cu ft`;
                            } else {
                                const litersUsed = pDiff * tank.size;
                                volUsedStr = `${Math.round(litersUsed)} L`;
                            }
                        }
                    }
                    
                    tanksHtml += `
                        <div class="tank-card">
                            <div class="tank-header">
                                <span class="tank-name">${tank.name}</span>
                                <span class="gas-type-badge ${tank.o2 > 21.5 || tank.he > 0 ? 'nitrox' : 'air'}">${gasType}</span>
                            </div>
                            <div class="tank-details-row">
                                <div class="tank-detail-subitem">
                                    <span class="tank-sublabel">Pressure (Start/End)</span>
                                    <span class="tank-subvalue">${pressureStr}</span>
                                </div>
                                <div class="tank-detail-subitem">
                                    <span class="tank-sublabel">Tank Size</span>
                                    <span class="tank-subvalue">${sizeStr}</span>
                                </div>
                                <div class="tank-detail-subitem">
                                    <span class="tank-sublabel">Volume Used</span>
                                    <span class="tank-subvalue highlighted-val">${volUsedStr}</span>
                                </div>
                            </div>
                        </div>
                    `;
                });
                
                tanksHtml += `</div>`;
                modalContentFancy.innerHTML += tanksHtml;
            }
        } catch (err) {
            modalRawJson.textContent = `Error loading raw JSON: ${err.message}`;
        }
    }

    // 8. Spreadsheet Editor Logic
    let sheetDives = { garmin: [], divelogs: [] };
    let sheetService = "garmin";
    let sheetPageSize = 20;
    let sheetCurrentPage = 1;
    let sheetSortCol = "date_time";
    let sheetSortDir = "desc";
    let sheetSearch = "";
    
    // Spreadsheet DOM Elements
    const sheetServiceSelect = document.getElementById("sheet-service-select");
    const sheetSizeSelect = document.getElementById("sheet-size-select");
    const sheetSearchInput = document.getElementById("sheet-search-input");
    const sheetPrevBtn = document.getElementById("sheet-prev-btn");
    const sheetNextBtn = document.getElementById("sheet-next-btn");
    const sheetPageInfo = document.getElementById("sheet-page-info");
    const sheetTotalInfo = document.getElementById("sheet-total-info");
    const sheetTbody = document.getElementById("spreadsheet-tbody");
    const sheetSavingIndicator = document.getElementById("sheet-saving-indicator");
    const sheetHeaders = document.querySelectorAll(".spreadsheet-table th.sortable");

    // Setup Table Headers Sorting Click Listeners
    sheetHeaders.forEach(th => {
        th.addEventListener("click", () => {
            const col = th.getAttribute("data-sort");
            if (sheetSortCol === col) {
                sheetSortDir = sheetSortDir === "asc" ? "desc" : "asc";
            } else {
                sheetSortCol = col;
                sheetSortDir = "desc"; // Default to desc for new columns
            }
            updateSortHeaders();
            renderSpreadsheet();
        });
    });

    function updateSortHeaders() {
        sheetHeaders.forEach(th => {
            const col = th.getAttribute("data-sort");
            const icon = th.querySelector(".sort-icon");
            if (!icon) return;
            if (col === sheetSortCol) {
                icon.textContent = sheetSortDir === "asc" ? " ▲" : " ▼";
            } else {
                icon.textContent = "";
            }
        });
    }

    async function loadSpreadsheetData() {
        if (!sheetTbody) return;
        sheetTbody.innerHTML = `<tr><td colspan="9" class="empty-state text-subtle">Loading spreadsheet data...</td></tr>`;
        try {
            const response = await fetch("/api/dives");
            if (!response.ok) throw new Error("Failed to load dives.");
            sheetDives = await response.json();
            
            updateSortHeaders();
            renderSpreadsheet();
        } catch (err) {
            console.error("Error loading spreadsheet dives:", err);
            sheetTbody.innerHTML = `<tr><td colspan="9" class="empty-state text-subtle" style="color: var(--color-danger)">Error loading data: ${err.message}</td></tr>`;
        }
    }

    function sortDives(dives, col, dir) {
        return [...dives].sort((a, b) => {
            let valA = a[col];
            let valB = b[col];
            
            // Handle undefined/null
            if (valA === undefined || valA === null) valA = '';
            if (valB === undefined || valB === null) valB = '';
            
            // Convert to appropriate type for sorting
            if (col === 'duration' || col === 'max_depth') {
                valA = parseFloat(valA) || 0;
                valB = parseFloat(valB) || 0;
            } else if (col === 'dive_number') {
                const numA = parseInt(valA, 10);
                const numB = parseInt(valB, 10);
                if (!isNaN(numA) && !isNaN(numB)) {
                    valA = numA;
                    valB = numB;
                } else {
                    valA = String(valA).toLowerCase();
                    valB = String(valB).toLowerCase();
                }
            } else {
                valA = String(valA).toLowerCase();
                valB = String(valB).toLowerCase();
            }
            
            if (valA < valB) return dir === 'asc' ? -1 : 1;
            if (valA > valB) return dir === 'asc' ? 1 : -1;
            return 0;
        });
    }

    function escapeHtml(str) {
        if (str === null || str === undefined) return "";
        const s = String(str);
        if (!s) return "";
        return s
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function renderSpreadsheet() {
        if (!sheetTbody) return;
        
        let list = sheetDives[sheetService] || [];
        
        // Filter list
        if (sheetSearch.trim()) {
            const searchLower = sheetSearch.toLowerCase().trim();
            list = list.filter(d => {
                const loc = (d.location || "").toLowerCase();
                const notes = (d.notes || "").toLowerCase();
                const buddy = (d.buddy || "").toLowerCase();
                const num = (d.dive_number || "").toLowerCase();
                return loc.includes(searchLower) || notes.includes(searchLower) || buddy.includes(searchLower) || num.includes(searchLower);
            });
        }
        
        // Sort list
        list = sortDives(list, sheetSortCol, sheetSortDir);
        
        // Paginate
        const totalRows = list.length;
        const totalPages = Math.max(1, Math.ceil(totalRows / sheetPageSize));
        
        if (sheetCurrentPage > totalPages) {
            sheetCurrentPage = totalPages;
        }
        if (sheetCurrentPage < 1) {
            sheetCurrentPage = 1;
        }
        
        const startIndex = (sheetCurrentPage - 1) * sheetPageSize;
        const endIndex = Math.min(startIndex + sheetPageSize, totalRows);
        const pagedList = list.slice(startIndex, endIndex);
        
        // Update pagination UI
        sheetPageInfo.textContent = `Page ${sheetCurrentPage} of ${totalPages}`;
        sheetTotalInfo.textContent = totalRows > 0 
            ? `Showing ${startIndex + 1}-${endIndex} of ${totalRows} dives`
            : `Showing 0-0 of 0 dives`;
            
        sheetPrevBtn.disabled = sheetCurrentPage <= 1;
        sheetNextBtn.disabled = sheetCurrentPage >= totalPages;
        
        // Render
        sheetTbody.innerHTML = "";
        
        if (pagedList.length === 0) {
            sheetTbody.innerHTML = `<tr><td colspan="9" class="empty-state text-subtle">No dives found matching criteria.</td></tr>`;
            return;
        }
        
        pagedList.forEach(dive => {
            const tr = document.createElement("tr");
            tr.dataset.filename = dive.filename;
            
            const isDivelogs = sheetService === "divelogs";
            
            tr.innerHTML = `
                <td>
                    <input type="text" class="sheet-input" data-field="dive_number" value="${escapeHtml(dive.dive_number || '')}" ${isDivelogs ? 'disabled title="Dive numbers are read-only for Divelogs.org"' : ''}>
                </td>
                <td>
                    <input type="text" class="sheet-input" data-field="date_time" value="${escapeHtml(dive.date_time || '')}">
                </td>
                <td>
                    <input type="number" class="sheet-input" data-field="duration" value="${dive.duration || 0}">
                </td>
                <td>
                    <input type="number" step="0.1" class="sheet-input" data-field="max_depth" value="${dive.max_depth || 0.0}">
                </td>
                <td>
                    <input type="text" class="sheet-input" data-field="location" value="${escapeHtml((dive.location && dive.location !== 'None') ? dive.location : '')}">
                </td>
                <td>
                    <input type="text" class="sheet-input" data-field="weight" value="${escapeHtml((dive.weight && dive.weight !== 'None') ? dive.weight : '')}">
                </td>
                <td>
                    <input type="text" class="sheet-input" data-field="visibility" value="${escapeHtml((dive.visibility && dive.visibility !== 'None') ? dive.visibility : '')}">
                </td>
                <td>
                    <input type="text" class="sheet-input" data-field="buddy" value="${escapeHtml((dive.buddy && dive.buddy !== 'None') ? dive.buddy : '')}">
                </td>
                <td>
                    <input type="text" class="sheet-input" data-field="notes" value="${escapeHtml((dive.notes && dive.notes !== 'None') ? dive.notes : '')}">
                </td>
            `;
            
            const inputs = tr.querySelectorAll(".sheet-input");
            inputs.forEach(input => {
                const field = input.getAttribute("data-field");
                let originalValue = input.value;
                
                const saveFieldChange = async () => {
                    const newValue = input.value;
                    if (newValue === originalValue) return;
                    
                    sheetSavingIndicator.classList.remove("hidden");
                    
                    const payload = {
                        service: sheetService,
                        filename: dive.filename
                    };
                    
                    if (field === "duration") {
                        payload[field] = parseInt(newValue, 10) || 0;
                    } else if (field === "max_depth") {
                        payload[field] = parseFloat(newValue) || 0.0;
                    } else if ((field === "buddy" || field === "notes" || field === "location" || field === "weight" || field === "visibility") && newValue.trim() === "") {
                        payload[field] = sheetService === "garmin" ? "None" : "";
                    } else {
                        payload[field] = newValue;
                    }
                    
                    try {
                        const response = await fetch("/api/dives/update", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(payload)
                        });
                        
                        if (!response.ok) {
                            const errData = await response.json();
                            throw new Error(errData.detail || "Failed to update dive.");
                        }
                        
                        const cell = input.closest("td");
                        cell.classList.remove("flash-success", "flash-error");
                        void cell.offsetWidth;
                        cell.classList.add("flash-success");
                        
                        const cacheDive = sheetDives[sheetService].find(d => d.filename === dive.filename);
                        if (cacheDive) {
                            if (field === "duration") {
                                cacheDive[field] = parseInt(newValue, 10) || 0;
                            } else if (field === "max_depth") {
                                cacheDive[field] = parseFloat(newValue) || 0.0;
                            } else {
                                cacheDive[field] = newValue;
                            }
                        }
                        
                        originalValue = newValue;
                    } catch (err) {
                        console.error("Failed saving field", field, err);
                        const cell = input.closest("td");
                        cell.classList.remove("flash-success", "flash-error");
                        void cell.offsetWidth;
                        cell.classList.add("flash-error");
                        
                        input.value = originalValue;
                    } finally {
                        sheetSavingIndicator.classList.add("hidden");
                    }
                };
                
                input.addEventListener("blur", saveFieldChange);
                input.addEventListener("keydown", (e) => {
                    if (e.key === "Enter") {
                        input.blur();
                    }
                });
            });
            
            sheetTbody.appendChild(tr);
        });
    }

    if (sheetServiceSelect) {
        sheetServiceSelect.addEventListener("change", () => {
            sheetService = sheetServiceSelect.value;
            sheetCurrentPage = 1;
            renderSpreadsheet();
        });
    }
    if (sheetSizeSelect) {
        sheetSizeSelect.addEventListener("change", () => {
            sheetPageSize = parseInt(sheetSizeSelect.value, 10);
            sheetCurrentPage = 1;
            renderSpreadsheet();
        });
    }
    if (sheetSearchInput) {
        sheetSearchInput.addEventListener("input", () => {
            sheetSearch = sheetSearchInput.value;
            sheetCurrentPage = 1;
            renderSpreadsheet();
        });
    }
    if (sheetPrevBtn) {
        sheetPrevBtn.addEventListener("click", () => {
            if (sheetCurrentPage > 1) {
                sheetCurrentPage--;
                renderSpreadsheet();
            }
        });
    }
    if (sheetNextBtn) {
        sheetNextBtn.addEventListener("click", () => {
            sheetCurrentPage++;
            renderSpreadsheet();
        });
    }

    // 9. Tab Navigation Logic
    const tabBtns = document.querySelectorAll(".tab-btn");
    const tabContents = document.querySelectorAll(".tab-content");

    tabBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetTab = btn.getAttribute("data-tab");

            // Update button active state
            tabBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            // Update content visibility
            tabContents.forEach(content => {
                if (content.id === `${targetTab}-tab`) {
                    content.classList.remove("hidden");
                } else {
                    content.classList.add("hidden");
                }
            });

            // Load dives if explorer or spreadsheet is chosen
            if (targetTab === "dives-explorer") {
                loadDives();
            } else if (targetTab === "spreadsheet-editor") {
                loadSpreadsheetData();
            }
        });
    });

    // Setup Dives Explorer Actions (Refresh & Download Raw)
    const refreshExplorerBtn = document.getElementById("refresh-explorer-btn");
    const downloadRawBtn = document.getElementById("download-raw-btn");

    if (refreshExplorerBtn) {
        refreshExplorerBtn.addEventListener("click", async () => {
            const originalText = refreshExplorerBtn.textContent;
            refreshExplorerBtn.disabled = true;
            refreshExplorerBtn.textContent = "Refreshing...";
            if (downloadRawBtn) downloadRawBtn.disabled = true;
            
            try {
                await loadDives();
            } finally {
                refreshExplorerBtn.disabled = false;
                refreshExplorerBtn.textContent = originalText;
                if (downloadRawBtn) downloadRawBtn.disabled = false;
            }
        });
    }

    let downloadInterval = null;

    if (downloadRawBtn) {
        downloadRawBtn.addEventListener("click", async () => {
            const originalText = downloadRawBtn.textContent;
            downloadRawBtn.disabled = true;
            downloadRawBtn.textContent = "Downloading...";
            if (refreshExplorerBtn) refreshExplorerBtn.disabled = true;
            
            try {
                const startResponse = await fetch("/api/dives/download?overwrite=true", {
                    method: "POST"
                });
                
                if (!startResponse.ok) {
                    const err = await startResponse.json();
                    throw new Error(err.detail || "Failed to start download.");
                }
                
                // Clear any existing interval
                if (downloadInterval) clearInterval(downloadInterval);
                
                // Start polling
                downloadInterval = setInterval(async () => {
                    try {
                        const statusResponse = await fetch("/api/dives/download/status");
                        if (!statusResponse.ok) return;
                        const status = await statusResponse.json();
                        
                        // Load current progress
                        const response = await fetch("/api/dives");
                        if (response.ok) {
                            const data = await response.json();
                            const totalLoaded = data.garmin.length + data.divelogs.length;
                            downloadRawBtn.textContent = `Downloading (${totalLoaded})...`;
                            renderExplorerLists(data);
                        }
                        
                        if (!status.is_running) {
                            clearInterval(downloadInterval);
                            downloadRawBtn.disabled = false;
                            downloadRawBtn.textContent = originalText;
                            if (refreshExplorerBtn) refreshExplorerBtn.disabled = false;
                            
                            if (status.error) {
                                alert(`Download completed with issue: ${status.error}`);
                            } else {
                                alert("Download complete! All cached dives have been loaded.");
                            }
                            
                            // Load one final time
                            loadDives();
                        }
                    } catch (pollErr) {
                        console.error("Error polling download status:", pollErr);
                    }
                }, 1000);
                
            } catch (err) {
                alert(`Error: ${err.message}`);
                downloadRawBtn.disabled = false;
                downloadRawBtn.textContent = originalText;
                if (refreshExplorerBtn) refreshExplorerBtn.disabled = false;
            }
        });
    }

    // Initialize
    loadSettings();
    loadCredentialsStatus();
    connectLogStream();
    loadVersionInfo();
    
    // Periodically poll credentials status & active sync state
    setInterval(loadCredentialsStatus, 10000);

    // ===== CREDENTIALS MODAL =====
    const credsModal = document.getElementById("credentials-modal");
    const credsForm = document.getElementById("credentials-form");
    const configureCredsBtn = document.getElementById("configure-creds-btn");
    const credsModalCloseBtn = document.getElementById("creds-modal-close-btn");
    const testCredsBtn = document.getElementById("test-creds-btn");
    const credsTestResults = document.getElementById("creds-test-results");

    function openCredsModal() {
        credsModal.classList.remove("hidden");
        credsTestResults.classList.add("hidden");
        document.getElementById("cred-garmin-pass").value = "";
        document.getElementById("cred-divelogs-pass").value = "";
        fetch("/api/credentials/status")
            .then(r => r.json())
            .then(status => {
                if (status.garmin_username) {
                    document.getElementById("cred-garmin-user").value = status.garmin_username;
                }
                if (status.divelogs_username) {
                    document.getElementById("cred-divelogs-user").value = status.divelogs_username;
                }
            })
            .catch(() => {});
    }

    function closeCredsModal() {
        credsModal.classList.add("hidden");
    }

    configureCredsBtn.addEventListener("click", openCredsModal);
    credsModalCloseBtn.addEventListener("click", closeCredsModal);
    credsModal.addEventListener("click", (e) => {
        if (e.target === credsModal) closeCredsModal();
    });

    function getCredsPayload() {
        return {
            garmin_username: document.getElementById("cred-garmin-user").value.trim(),
            garmin_password: document.getElementById("cred-garmin-pass").value.trim(),
            garmin_token_dir: "tokens/garmin",
            divelogs_username: document.getElementById("cred-divelogs-user").value.trim(),
            divelogs_password: document.getElementById("cred-divelogs-pass").value.trim()
        };
    }

    testCredsBtn.addEventListener("click", async () => {
        const payload = getCredsPayload();
        testCredsBtn.disabled = true;
        testCredsBtn.textContent = "Testing...";
        credsTestResults.classList.remove("hidden");
        credsTestResults.innerHTML = '<div class="creds-result-line skipped"><span>⏳ Testing connections...</span></div>';

        try {
            const response = await fetch("/api/credentials/test", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const results = await response.json();
            let html = "";

            if (results.garmin === true) {
                html += '<div class="creds-result-line success"><span class="result-icon">✓</span> Garmin Connect authentication succeeded</div>';
            } else if (results.garmin === false) {
                html += '<div class="creds-result-line failure"><span class="result-icon">✗</span> Garmin Connect authentication failed</div>';
            } else {
                html += '<div class="creds-result-line skipped"><span class="result-icon">—</span> Garmin Connect skipped (no credentials)</div>';
            }

            if (results.divelogs === true) {
                html += '<div class="creds-result-line success"><span class="result-icon">✓</span> Divelogs.org authentication succeeded</div>';
            } else if (results.divelogs === false) {
                html += '<div class="creds-result-line failure"><span class="result-icon">✗</span> Divelogs.org authentication failed</div>';
            } else {
                html += '<div class="creds-result-line skipped"><span class="result-icon">—</span> Divelogs.org skipped (no credentials)</div>';
            }

            credsTestResults.innerHTML = html;
        } catch (err) {
            credsTestResults.innerHTML = '<div class="creds-result-line failure"><span class="result-icon">✗</span> Test request failed: ' + err.message + '</div>';
        } finally {
            testCredsBtn.disabled = false;
            testCredsBtn.textContent = "Test Connection";
        }
    });

    credsForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const payload = getCredsPayload();
        const saveBtn = document.getElementById("save-creds-btn");
        saveBtn.disabled = true;
        saveBtn.textContent = "Saving...";

        try {
            const response = await fetch("/api/credentials", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });

            if (response.ok) {
                appendLogLine("[SYSTEM] Credentials saved successfully via web dashboard.", "system-msg");
                closeCredsModal();
                loadCredentialsStatus();
            } else {
                const errData = await response.json();
                throw new Error(errData.detail || "Failed to save credentials.");
            }
        } catch (err) {
            appendLogLine(`[ERROR] Failed to save credentials: ${err.message}`, "error");
            alert("Failed to save credentials: " + err.message);
        } finally {
            saveBtn.disabled = false;
            saveBtn.textContent = "Save Credentials";
        }
    });

    async function loadVersionInfo() {
        const versionEl = document.getElementById("app-version-info");
        if (!versionEl) return;
        
        try {
            const res = await fetch("/api/version");
            if (!res.ok) throw new Error("Status " + res.status);
            const info = await res.json();
            
            let html = `<span>Running version: <strong style="color: var(--color-primary);">${info.current_version}</strong></span>`;
            if (info.update_available) {
                html += ` <a href="${info.release_url}" target="_blank" class="update-link">▲ Update available: ${info.latest_version}</a>`;
            } else {
                html += ` <span style="font-size: 0.7rem; padding: 0.15rem 0.4rem; background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.2); color: var(--color-success); border-radius: var(--radius-full); margin-left: 0.5rem; font-weight: 700;">LATEST</span>`;
            }
            versionEl.innerHTML = html;
        } catch (err) {
            versionEl.textContent = "Version info unavailable";
        }
    }

    // 10. Real-time Clock for Telemetry Bar
    function startClock() {
        const clockEl = document.getElementById("header-clock");
        if (!clockEl) return;
        
        function updateTime() {
            const now = new Date();
            const timeStr = now.toLocaleTimeString("en-US", { hour12: false });
            clockEl.textContent = timeStr;
        }
        
        updateTime();
        setInterval(updateTime, 1000);
    }
    startClock();
});
