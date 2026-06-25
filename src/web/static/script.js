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

    // 7. Load and Render Dives Explorer
    function renderExplorerLists(data) {
        const garminList = document.getElementById("garmin-dives-list");
        const divelogsList = document.getElementById("divelogs-dives-list");
        const garminCount = document.getElementById("garmin-count");
        const divelogsCount = document.getElementById("divelogs-count");

        if (!garminList || !divelogsList) return;

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

        card.innerHTML = `
            <div class="dive-card-header">
                <span class="dive-title">${dive.location || "Unknown Location"}</span>
                <span class="dive-meta-date">${dive.date_time}</span>
            </div>
            <div class="dive-card-details">
                <span><span class="dive-detail-label">Dive:</span><span class="dive-detail-val">#${dive.dive_number}</span></span>
                <span><span class="dive-detail-label">Depth:</span><span class="dive-detail-val">${maxDepthM.toFixed(1)}m</span></span>
                <span><span class="dive-detail-label">Duration:</span><span class="dive-detail-val">${durationMin} min</span></span>
                ${dive.weight ? `<span><span class="dive-detail-label">Weight:</span><span class="dive-detail-val">${dive.weight}</span></span>` : ""}
                ${dive.visibility ? `<span><span class="dive-detail-label">Vis:</span><span class="dive-detail-val">${dive.visibility}</span></span>` : ""}
            </div>
            ${dive.notes ? `<p class="dive-notes" title="${escapeHtml(dive.notes)}">${escapeHtml(dive.notes)}</p>` : ""}
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
                    <span class="detail-value">${dive.buddy || 'None'}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Weight</span>
                    <span class="detail-value">${dive.weight || 'None'}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Visibility</span>
                    <span class="detail-value">${dive.visibility || 'None'}</span>
                </div>
                <div class="detail-item span-full">
                    <span class="detail-label">Notes</span>
                    <span class="detail-value notes-val">${escapeHtml(dive.notes || 'No description provided.')}</span>
                </div>
            </div>
        `;
        
        modalRawJson.textContent = "Loading raw JSON data...";
        try {
            const res = await fetch(`/api/dives/raw?service=${service}&filename=${dive.filename}`);
            if (!res.ok) throw new Error("Failed to load raw JSON.");
            const rawData = await res.json();
            modalRawJson.textContent = JSON.stringify(rawData, null, 2);
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
                    <input type="text" class="sheet-input" data-field="location" value="${escapeHtml(dive.location || '')}">
                </td>
                <td>
                    <input type="text" class="sheet-input" data-field="weight" value="${escapeHtml(dive.weight || '')}">
                </td>
                <td>
                    <input type="text" class="sheet-input" data-field="visibility" value="${escapeHtml(dive.visibility || '')}">
                </td>
                <td>
                    <input type="text" class="sheet-input" data-field="buddy" value="${escapeHtml(dive.buddy || '')}">
                </td>
                <td>
                    <input type="text" class="sheet-input" data-field="notes" value="${escapeHtml(dive.notes || '')}">
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
    
    // Periodically poll credentials status & active sync state
    setInterval(loadCredentialsStatus, 10000);
});
