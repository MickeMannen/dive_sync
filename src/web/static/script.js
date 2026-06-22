document.addEventListener("DOMContentLoaded", () => {
    // DOM Elements
    const settingsForm = document.getElementById("settings-form");
    const triggerSyncBtn = document.getElementById("trigger-sync-btn");
    const dryRunCheckbox = document.getElementById("dry-run-checkbox");
    const logTerminal = document.getElementById("log-terminal");
    const clearLogBtn = document.getElementById("clear-log-btn");
    const autoscrollCheckbox = document.getElementById("autoscroll-checkbox");
    const globalStatusText = document.getElementById("global-status-text");
    const statusDot = document.querySelector(".status-dot");
    const garminStatusEl = document.getElementById("garmin-status");
    const divelogsStatusEl = document.getElementById("divelogs-status");
    
    // Scheduler Elements
    const addSlotBtn = document.getElementById("add-slot-btn");
    const newSlotTime = document.getElementById("new-slot-time");
    const scheduleSlotsList = document.getElementById("schedule-slots-list");

    let scheduleSlots = [];

    // 1. Load Settings and Populate Form
    async function loadSettings() {
        try {
            const response = await fetch("/api/settings");
            if (!response.ok) throw new Error("Failed to load settings.");
            
            const settings = await response.json();
            
            // Populate inputs
            document.getElementById("directionality").value = settings.directionality;
            document.getElementById("date_from").value = settings.sync_filters.date_from || "";
            document.getElementById("date_to").value = settings.sync_filters.date_to || "";
            document.getElementById("only_new").checked = settings.sync_filters.only_new;
            document.getElementById("sync_gases").checked = settings.sync_filters.sync_gases;
            document.getElementById("sync_fit").checked = settings.sync_filters.sync_fit;
            document.getElementById("grace_window_minutes").value = settings.grace_window_minutes;
            document.getElementById("api_cooldown_seconds").value = settings.api_cooldown_seconds;
            
            // Load schedules
            scheduleSlots = settings.schedule || [];
            renderScheduleSlots();
        } catch (err) {
            appendLogLine(`[ERROR] Failed to load configurations: ${err.message}`, "error");
        }
    }

    // 2. Load Credentials Status
    async function loadCredentialsStatus() {
        try {
            const response = await fetch("/api/credentials/status");
            const status = await response.json();
            
            updateCredentialBadge(garminStatusEl, status.garmin_configured);
            updateCredentialBadge(divelogsStatusEl, status.divelogs_configured);
        } catch (err) {
            appendLogLine(`[ERROR] Failed to fetch credential status: ${err.message}`, "error");
        }
    }

    function updateCredentialBadge(element, configured) {
        const badge = element.querySelector(".badge");
        const stateText = element.querySelector(".service-state");
        
        if (configured) {
            badge.className = "badge green";
            badge.textContent = "Active";
            stateText.textContent = "Credentials loaded and ready.";
        } else {
            badge.className = "badge red";
            badge.textContent = "Unconfigured";
            stateText.textContent = "Run setup.py to configure.";
        }
    }

    // 3. Render Schedule Slots
    function renderScheduleSlots() {
        scheduleSlotsList.innerHTML = "";
        if (scheduleSlots.length === 0) {
            scheduleSlotsList.innerHTML = `<li class="empty-state text-subtle">No schedules configured.</li>`;
            return;
        }

        // Sort schedule slot times
        scheduleSlots.sort((a, b) => {
            if (a.hour !== b.hour) return a.hour - b.hour;
            return a.minute - b.minute;
        });

        scheduleSlots.forEach((slot, index) => {
            const li = document.createElement("li");
            li.className = "schedule-item";
            
            const timeStr = `${String(slot.hour).padStart(2, '0')}:${String(slot.minute).padStart(2, '0')}`;
            li.innerHTML = `
                <span>🕒 Sync at ${timeStr} daily</span>
                <button class="delete-btn" data-index="${index}">&times;</button>
            `;
            
            li.querySelector(".delete-btn").addEventListener("click", () => {
                scheduleSlots.splice(index, 1);
                renderScheduleSlots();
            });
            
            scheduleSlotsList.appendChild(li);
        });
    }

    // Add new schedule slot
    addSlotBtn.addEventListener("click", () => {
        const timeVal = newSlotTime.value;
        if (!timeVal) return;
        
        const [hourStr, minuteStr] = timeVal.split(":");
        const hour = parseInt(hourStr);
        const minute = parseInt(minuteStr);
        
        // Check for duplicates
        const exists = scheduleSlots.some(slot => slot.hour === hour && slot.minute === minute);
        if (exists) {
            alert("This schedule slot already exists.");
            return;
        }

        scheduleSlots.push({ hour, minute });
        renderScheduleSlots();
        newSlotTime.value = "";
    });

    // 4. Save Settings Form
    settingsForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        
        const payload = {
            directionality: document.getElementById("directionality").value,
            sync_filters: {
                date_from: document.getElementById("date_from").value || null,
                date_to: document.getElementById("date_to").value || null,
                only_new: document.getElementById("only_new").checked,
                sync_gases: document.getElementById("sync_gases").checked,
                sync_fit: document.getElementById("sync_fit").checked
            },
            grace_window_minutes: parseInt(document.getElementById("grace_window_minutes").value),
            api_cooldown_seconds: parseFloat(document.getElementById("api_cooldown_seconds").value),
            schedule: scheduleSlots
        };

        try {
            const response = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });

            if (response.ok) {
                appendLogLine("[SYSTEM] Configuration updated successfully.", "system-msg");
                alert("Settings saved successfully.");
            } else {
                const errData = await response.json();
                throw new Error(errData.detail || "Failed to save settings.");
            }
        } catch (err) {
            appendLogLine(`[ERROR] Failed to save settings: ${err.message}`, "error");
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
    }

    clearLogBtn.addEventListener("click", () => {
        logTerminal.innerHTML = "";
    });

    // 6. Manual Sync execution Trigger
    triggerSyncBtn.addEventListener("click", async () => {
        const dryRun = dryRunCheckbox.checked;
        
        // Disable buttons
        triggerSyncBtn.disabled = true;
        triggerSyncBtn.querySelector(".btn-text").textContent = "Sync Running...";
        triggerSyncBtn.querySelector(".spinner").classList.remove("hidden");
        
        statusDot.className = "status-dot orange";
        globalStatusText.textContent = "Syncing...";

        try {
            const response = await fetch(`/api/sync/trigger?dry_run=${dryRun}`, {
                method: "POST"
            });
            
            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || "Failed to trigger sync.");
            }
            
            appendLogLine(`[SYSTEM] Synchronization job triggered (Dry Run: ${dryRun}). Check output console below.`, "system-msg");
            
            // Poll for sync completion status
            pollSyncStatus();
        } catch (err) {
            appendLogLine(`[ERROR] ${err.message}`, "error");
            resetSyncButton();
        }
    });

    async function pollSyncStatus() {
        const interval = setInterval(async () => {
            try {
                const response = await fetch("/api/sync/status");
                const state = await response.json();
                
                if (!state.is_running) {
                    clearInterval(interval);
                    resetSyncButton();
                    
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

    function resetSyncButton() {
        triggerSyncBtn.disabled = false;
        triggerSyncBtn.querySelector(".btn-text").textContent = "Trigger Manual Sync";
        triggerSyncBtn.querySelector(".spinner").classList.add("hidden");
        
        statusDot.className = "status-dot green";
        globalStatusText.textContent = "System Ready";
    }

    // Initialize
    loadSettings();
    loadCredentialsStatus();
    connectLogStream();
    
    // Periodically poll credentials status & active sync state
    setInterval(loadCredentialsStatus, 10000);
});
