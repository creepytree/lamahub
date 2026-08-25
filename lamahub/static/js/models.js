/**
 * Lamahub - Models Tab
 * @description Handles models list, running models, and model operations.
 * Generated markup uses <druid-*> elements and lh-* classes (see app.css).
 */

let fixedModels = [];
let fixedModelsLoaded = false;
// Last /models payload for the active endpoint, cached from loadModelsList so
// other views (e.g. the Deploy tab's staged table) can reuse it without a
// second fetch. Refreshed on every models reload; null until the first load.
let lastModels = null;

function normalizeModelName(modelName) {
    const trimmedName = String(modelName || "").trim();
    if (trimmedName && !trimmedName.includes(":")) {
        return `${trimmedName}:latest`;
    }
    return trimmedName;
}

// fixedModels entries are {name, num_ctx, source} where source is "env" or "user"
function getFixedEntry(modelName) {
    const normalizedName = normalizeModelName(modelName);
    return fixedModels.find((entry) => normalizeModelName(entry.name) === normalizedName) || null;
}

function isFixedModel(modelName) {
    return getFixedEntry(modelName) !== null;
}

function renderCopyableModelName(modelName) {
    const escapedName = escapeHtml(modelName);
    const escapedAttribute = escapedName.replace(/"/g, "&quot;");

    return `
        <button type="button"
                class="model-name-button"
                data-model-name="${escapedAttribute}"
                title="Copy model name"
                aria-label="Copy model name ${escapedAttribute}">
            <span class="model-name-label">${escapedName}</span>
        </button>
    `;
}

async function copyModelName(modelName) {
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(modelName);
        } else {
            const textArea = document.createElement("textarea");
            textArea.value = modelName;
            textArea.setAttribute("readonly", "");
            textArea.style.position = "absolute";
            textArea.style.left = "-9999px";
            document.body.appendChild(textArea);
            textArea.select();
            document.execCommand("copy");
            document.body.removeChild(textArea);
        }
    } catch (error) {
        console.error("Failed to copy model name:", error);
        showNotification(`Could not copy "${modelName}"`, "danger");
    }
}

function showCopiedState(button) {
    const label = button.querySelector(".model-name-label");
    if (!label) return;

    const originalLabel = button.dataset.modelName || label.textContent || "";
    const existingTimeout = button.dataset.copiedTimeoutId;
    if (existingTimeout) {
        window.clearTimeout(Number(existingTimeout));
    }

    label.textContent = "Copied!";
    label.classList.add("is-copied");
    const timeoutId = window.setTimeout(() => {
        label.textContent = originalLabel;
        label.classList.remove("is-copied");
        delete button.dataset.copiedTimeoutId;
    }, 1200);

    button.dataset.copiedTimeoutId = String(timeoutId);
}

function initializeModelNameCopyHandlers() {
    const containerIds = ["models-list", "running-models-list", "fixed-models-list", "endpoint-url"];

    containerIds.forEach((containerId) => {
        const container = document.getElementById(containerId);
        if (!container || container.dataset.copyHandlerBound === "true") return;

        container.addEventListener("click", (event) => {
            const button = event.target.closest(".model-name-button");
            if (!button) return;

            event.preventDefault();
            copyModelName(button.dataset.modelName || "");
            showCopiedState(button);
        });

        container.dataset.copyHandlerBound = "true";
    });
}

async function loadFixedModels() {
    const card = document.getElementById("fixed-models-card");
    const container = document.getElementById("fixed-models-list");
    if (!card || !container) return;

    const data = await fetchAPI("/models/fixed");

    if (data.error) {
        fixedModels = [];
        fixedModelsLoaded = true;
        card.hidden = false;
        container.innerHTML = `<span class="df-danger">Error: ${escapeHtml(data.error)}</span>`;
        return;
    }

    fixedModels = data.models || [];
    fixedModelsLoaded = true;
    if (fixedModels.length === 0) {
        card.hidden = true;
        container.innerHTML = "";
        return;
    }

    card.hidden = false;
    container.innerHTML = fixedModels
        .map((entry) => {
            const ctxLabel = entry.num_ctx ? `<span class="df-badge">${entry.num_ctx} ctx</span>` : "";
            const kindLabel = entry.kind
                ? `<span class="df-badge accent" title="Warm-loaded as ${entry.kind}">${escapeHtml(entry.kind)}</span>`
                : '<span class="df-badge" title="Load endpoint detected from the model\'s capabilities">auto</span>';
            const sourceLabel =
                entry.source === "env" ? '<span class="df-badge" title="Set via FIXED_MODELS">env</span>' : "";
            const unpinBtn =
                entry.source === "user"
                    ? `<druid-icon-button circle small class="df-danger" icon="x"
                            onclick="unpinModel('${entry.name}')"
                            label="Unpin ${entry.name}"></druid-icon-button>`
                    : "";
            // name on its own line so a long one does not squeeze the badges
            return `
                <div class="df-row lh-fixed-item">
                    <div class="lh-stack-info">
                        <div>${renderCopyableModelName(entry.name)}</div>
                        <div class="lh-badges">${ctxLabel}${kindLabel}${sourceLabel}</div>
                    </div>
                    ${unpinBtn}
                </div>
            `;
        })
        .join("");
}

/**
 * Ask for a pin's context length and load kind.
 * @param {string} modelName - Model being pinned.
 * @param {string[]} capabilities - Reported capabilities, used to preselect kind.
 * @returns {Promise<{num_ctx: number|null, kind: string|null}|null>} null if cancelled.
 */
function promptPinSettings(modelName, capabilities) {
    // detected kind is only shown as the auto option's hint; the server detects
    // again on every reconcile, so "auto" stays right if the model is replaced
    const detected = capabilities.includes("embedding") ? "embed" : "chat";
    const form = document.createElement("div");
    form.className = "df-stack gap-lg";
    form.innerHTML = `
        <label class="df-field">
            <span>Context length (num_ctx)</span>
            <input type="text" id="pin-ctx" placeholder="e.g. 8192" autocomplete="off">
            <small class="df-muted">Baked as the model's default for every client. Empty = only protect from deletion.</small>
        </label>
        <div class="df-field">
            <!-- div, not label: a custom element is not labelable, so a label here would associate with nothing -->
            <span>Model type</span>
            <druid-select id="pin-kind" value="">
                <option value="">Auto-detect (${detected})</option>
                <option value="chat">Chat / Completion</option>
                <option value="embed">Embedding</option>
            </druid-select>
            <small class="df-muted">Which endpoint keeps it warm. Vision, OCR, tools and thinking models all load as chat.</small>
        </div>
    `;

    return new Promise((resolve) => {
        let settings = null;
        const dialog = druids.modal({
            title: `Pin "${modelName}"`,
            content: form,
            actions: [
                { label: "Cancel" },
                {
                    label: "Pin",
                    variant: "primary",
                    onClick: (d) => {
                        const trimmed = form.querySelector("#pin-ctx").value.trim();
                        let numCtx = null;
                        if (trimmed) {
                            numCtx = parseInt(trimmed, 10);
                            if (!Number.isInteger(numCtx) || numCtx <= 0) {
                                showNotification("Context length must be a positive number", "warning");
                                return;
                            }
                        }
                        settings = { num_ctx: numCtx, kind: form.querySelector("#pin-kind").value || null };
                        d.close();
                    },
                },
            ],
        });
        dialog.addEventListener("close", () => resolve(settings), { once: true });
        form.querySelector("#pin-ctx").focus();
    });
}

/**
 * Pin a model, optionally baking a fixed context length as its default.
 * @param {string} modelName - Name of the model to pin.
 */
async function pinModel(modelName) {
    const capabilities = await fetchModelCapabilities(modelName);
    const settings = await promptPinSettings(modelName, capabilities);
    if (settings === null) return; // cancelled

    const result = await fetchAPI(`/models/fixed/${encodeURIComponent(modelName)}`, {
        method: "PUT",
        body: JSON.stringify(settings),
    });

    if (result.status === "success") {
        showNotification(
            result.message ? `Pinned "${modelName}" — ${result.message}` : `Pinned "${modelName}"`,
            "success",
        );
        fixedModelsLoaded = false;
        await loadFixedModels();
        loadModelsList();
    } else {
        showNotification(`Error pinning model: ${result.message}`, "danger");
    }
}

/**
 * Remove a UI pin from a model.
 * @param {string} modelName - Name of the model to unpin.
 */
async function unpinModel(modelName) {
    const result = await fetchAPI(`/models/fixed/${encodeURIComponent(modelName)}`, {
        method: "DELETE",
    });

    if (result.status === "success") {
        showNotification(`Unpinned "${modelName}"`, "success");
        fixedModelsLoaded = false;
        await loadFixedModels();
        loadModelsList();
    } else {
        showNotification(`Error unpinning model: ${result.message}`, "danger");
    }
}

/**
 * Describe where ollama put a running model, from /api/ps size vs size_vram.
 * @param {object} model - An /api/ps entry.
 * @returns {string} Badge markup, or "" when the server reports no sizes.
 */
function renderPlacement(model) {
    const size = model.size || 0;
    const vram = model.size_vram || 0;
    if (!size) return '<span class="df-badge">Active</span>';
    if (vram >= size) {
        return `<span class="df-badge ok" title="All ${formatBytes(size)} in GPU memory">GPU</span>`;
    }
    // spilling to system RAM is slow and usually unintended, so it is called out
    // rather than left to be inferred from two byte counts
    const onCpu = size - vram;
    if (!vram) {
        return `<span class="df-badge danger" title="No layers offloaded to the GPU: all ${formatBytes(size)} in system RAM">CPU only</span>`;
    }
    const pct = Math.round((vram / size) * 100);
    return `<span class="df-badge warn" title="${formatBytes(onCpu)} did not fit in GPU memory">${pct}% GPU</span>`;
}

/**
 * Spell out the VRAM/RAM split for a model that did not fit on the GPU.
 * @param {object} model - An /api/ps entry.
 * @returns {string} Markup, or "" when the model is fully on one device.
 */
function renderSplit(model) {
    const size = model.size || 0;
    const vram = model.size_vram || 0;
    if (!size || vram >= size) return "";

    // nothing to spell out at 0% on GPU: the badge and the size say it already
    if (!vram) return "";
    return `<span class="lh-split">${formatBytes(vram)} GPU + ${formatBytes(size - vram)} RAM</span>`;
}

/**
 * Load and display running models on the dashboard.
 */
async function loadRunningModels() {
    const container = document.getElementById("running-models-list");
    if (!container) return;

    const data = await fetchAPI("/models/running");

    if (data.error) {
        const markup = `<span class="df-danger">Error: ${escapeHtml(data.error)}</span>`;
        if (container.dataset.lastRenderedMarkup !== markup) {
            container.innerHTML = markup;
            container.dataset.lastRenderedMarkup = markup;
        }
        return;
    }

    if (!data.models || data.models.length === 0) {
        const markup = '<span class="df-muted">No models running</span>';
        if (container.dataset.lastRenderedMarkup !== markup) {
            container.innerHTML = markup;
            container.dataset.lastRenderedMarkup = markup;
        }
        return;
    }

    const markup = data.models
        .map((model) => {
            const details = model.details || {};
            const params = details.parameter_size || "-";
            const quant = details.quantization_level || "-";
            const ctxLabel = model.context_length
                ? `<span class="df-badge" title="Loaded context length">${model.context_length} ctx</span>`
                : "";
            // name on its own line, then where it actually sits, so a model that
            // fell back to RAM is visible at a glance instead of read off a size
            return `
        <div class="df-row lh-running-item">
            <div class="lh-stack-info">
                <div>
                    ${renderCopyableModelName(model.name)}
                    <span class="df-muted">${params} | ${quant}</span>
                </div>
                <div class="lh-badges">
                    ${renderPlacement(model)}
                    <span class="df-badge">${model.size ? formatBytes(model.size) : ""}</span>
                    ${ctxLabel}
                    ${renderSplit(model)}
                </div>
            </div>
            <div class="df-row lh-running-meta">
                <druid-icon-button circle small variant="soft" class="df-warn" icon="upload"
                        onclick="unloadModel('${model.name}')"
                        label="Unload model ${model.name}"></druid-icon-button>
            </div>
        </div>
    `;
        })
        .join("");

    if (container.dataset.lastRenderedMarkup !== markup) {
        container.innerHTML = markup;
        container.dataset.lastRenderedMarkup = markup;
    }
}

/**
 * Load and display total models count on the dashboard.
 */
async function loadTotalModels() {
    const container = document.getElementById("total-models");
    if (!container) return;

    const data = await fetchAPI("/models");

    if (data.error) {
        container.innerHTML = `<span class="df-danger">Error: ${escapeHtml(data.error)}</span>`;
        return;
    }

    const count = data.models ? data.models.length : 0;
    container.innerHTML = `
        <div class="df-stat-number">${count}</div>
        <span class="df-stat-caption">models installed</span>
    `;

    return data;
}

/**
 * Load and display total storage used by models.
 */
async function loadTotalStorage() {
    const container = document.getElementById("total-storage");
    if (!container) return;

    const data = await fetchAPI("/models");

    if (data.error) {
        container.innerHTML = `<span class="df-danger">Error: ${escapeHtml(data.error)}</span>`;
        return;
    }

    const totalBytes = data.models ? data.models.reduce((sum, model) => sum + (model.size || 0), 0) : 0;
    container.innerHTML = `
        <div class="df-stat-number">${formatBytes(totalBytes)}</div>
        <span class="df-stat-caption">total disk usage</span>
    `;
}

/**
 * Fetch a model's capabilities (e.g. completion, tools, vision, thinking).
 * @param {string} modelName - Name of the model to inspect.
 * @returns {Promise<string[]>} Capability list, or empty array on error.
 */
async function fetchModelCapabilities(modelName) {
    try {
        const data = await fetchAPI(`/models/${encodeURIComponent(modelName)}/info`);
        if (data && Array.isArray(data.capabilities)) {
            return [...data.capabilities].sort();
        }
    } catch (error) {
        // "no capabilities reported"
    }
    return [];
}

/**
 * Render a model's capabilities as badges.
 * @param {string[]} capabilities - Capability list.
 * @returns {string} HTML markup.
 */
function renderCapabilities(capabilities) {
    if (!capabilities.length) {
        return '<span class="df-muted">-</span>';
    }
    const badges = capabilities.map((cap) => `<span class="df-badge">${escapeHtml(cap)}</span>`).join("");
    return `<span class="lh-badges">${badges}</span>`;
}

/**
 * Render a full-width placeholder row for the models table.
 * @param {string} message - Text to show.
 * @param {string} [cls] - Extra class on the empty block (e.g. "df-danger").
 * @returns {string} HTML markup.
 */
function placeholderRow(message, cls = "") {
    return `<tr><td colspan="6"><div class="df-empty ${cls}">${message}</div></td></tr>`;
}

/**
 * Load and display the models list table.
 */
async function loadModelsList() {
    const container = document.getElementById("models-list");
    if (!container) return;

    container.innerHTML = placeholderRow("Loading…");

    if (!fixedModelsLoaded) {
        await loadFixedModels();
    }

    const data = await fetchAPI("/models");

    if (data.error) {
        lastModels = null;
        container.innerHTML = placeholderRow(`Error: ${escapeHtml(data.error)}`, "df-danger");
        return;
    }

    lastModels = data.models || [];

    if (!data.models || data.models.length === 0) {
        container.innerHTML = placeholderRow("No models installed");
        return;
    }

    // Capabilities aren't in the /models listing, so fetch each model's info in
    // parallel to populate the Capabilities column.
    const rows = await Promise.all(
        data.models.map(async (model) => {
            const details = model.details || {};
            const params = details.parameter_size || "-";
            const quant = details.quantization_level || "-";
            // Ensure size is a number, default to 0 if not available
            const sizeBytes = parseInt(model.size) || 0;
            const sizeDisplay = sizeBytes > 0 ? formatBytes(sizeBytes) : "-";
            const capabilities = await fetchModelCapabilities(model.name);
            const fixedEntry = getFixedEntry(model.name);
            const fixedModel = fixedEntry !== null;
            const isEnvFixed = fixedEntry?.source === "env";
            const isUserPinned = fixedEntry?.source === "user";
            const deleteTitle = fixedModel ? "Pinned models cannot be deleted" : `Delete model ${model.name}`;

            // pin toggle: env pins are shown active but locked; user pins toggle off; unpinned toggle on
            const pinClasses = ["lh-pin"];
            if (fixedModel) pinClasses.push("lh-pin-active");
            if (isEnvFixed) pinClasses.push("lh-locked");
            const pinTitle = isEnvFixed
                ? "Pinned via FIXED_MODELS"
                : isUserPinned
                  ? `Unpin${fixedEntry.num_ctx ? ` (warm at ${fixedEntry.num_ctx} ctx)` : ""}`
                  : "Pin / keep warm";
            const pinOnclick = isEnvFixed
                ? ""
                : isUserPinned
                  ? `onclick="unpinModel('${model.name}')"`
                  : `onclick="pinModel('${model.name}')"`;

            const deleteBtn = fixedModel
                ? `<druid-icon-button circle small class="lh-locked" icon="x" label="${deleteTitle}"></druid-icon-button>`
                : `<druid-icon-button circle small class="df-danger" icon="x" onclick="deleteModel('${model.name}')" label="${deleteTitle}"></druid-icon-button>`;

            return `
        <tr>
            <td data-value="${model.name.toLowerCase()}">${renderCopyableModelName(model.name)}</td>
            <td class="num" data-value="${sizeBytes}">${sizeDisplay}</td>
            <td data-value="${params.toLowerCase()}">${params}</td>
            <td data-value="${capabilities.join(",")}">${renderCapabilities(capabilities)}</td>
            <td data-value="${quant.toLowerCase()}">${quant}</td>
            <td>
                <div class="df-row gap-sm lh-row-actions">
                    <druid-icon-button circle small icon="pin"
                            class="${pinClasses.join(" ")}"
                            ${fixedModel ? "active" : ""}
                            ${pinOnclick}
                            label="${pinTitle} ${model.name}"></druid-icon-button>
                    <druid-icon-button circle small icon="rotate-cw"
                            class="update-btn"
                            onclick="updateModel('${model.name}')"
                            label="Update model ${model.name}"
                            data-model="${model.name}"></druid-icon-button>
                    ${deleteBtn}
                </div>
            </td>
        </tr>
    `;
        }),
    );

    // druid-table watches the rows with a MutationObserver, so the active sort
    // and filter reapply themselves after the rebuild
    container.innerHTML = rows.join("");
}

/**
 * Pull a new model with streaming progress display.
 */
async function pullModel() {
    const input = document.getElementById("model-name-input");
    const statusRow = document.getElementById("pull-status");
    const btn = document.getElementById("pull-model-btn");

    const modelName = input.value.trim();

    if (!modelName) {
        showNotification("Please enter a model name", "warning");
        return;
    }

    // `loading` spins the button and drops clicks without changing its width
    btn.setAttribute("loading", "");

    const statusText = document.getElementById("pull-status-text");
    const progressBar = document.getElementById("pull-progress");
    const progressPercent = document.getElementById("pull-progress-percent");

    statusText.textContent = "Starting pull...";
    progressPercent.textContent = "";
    progressBar.setAttribute("value", "0");
    statusRow.hidden = false;

    try {
        const response = await fetch(withBasePath("/api/models/pull"), {
            method: "POST",
            headers: requestHeaders(),
            body: JSON.stringify({ name: modelName }),
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            const text = decoder.decode(value);
            const lines = text.split("\n");

            for (const line of lines) {
                if (line.startsWith("data: ")) {
                    try {
                        const data = JSON.parse(line.slice(6));

                        if (data.error) {
                            showNotification(`Error: ${data.error}`, "danger");
                            statusRow.hidden = true;
                            btn.removeAttribute("loading");
                            return;
                        }

                        if (data.status && statusText) {
                            statusText.textContent = data.status;
                        }

                        if (data.total && data.completed !== undefined) {
                            const percent = Math.round((data.completed / data.total) * 100);
                            const completedStr = formatBytes(data.completed);
                            const totalStr = formatBytes(data.total);
                            if (progressBar) progressBar.setAttribute("value", String(percent));
                            if (progressPercent)
                                progressPercent.textContent = `${completedStr} / ${totalStr} (${percent}%)`;
                        }
                    } catch (e) {}
                }
            }
        }

        showNotification(`Model "${modelName}" pulled successfully!`, "success");
        input.value = "";
        loadModelsList();
    } catch (error) {
        showNotification(`Error: ${error.message}`, "danger");
    }

    statusRow.hidden = true;
    btn.removeAttribute("loading");
}

/**
 * Delete a model after confirmation.
 * @param {string} modelName - Name of the model to delete.
 */
async function deleteModel(modelName) {
    const ok = await druids.confirm(`Delete "${modelName}"? This cannot be undone.`, {
        title: "Delete model",
        confirmLabel: "Delete",
        danger: true,
    });
    if (!ok) {
        return;
    }

    const result = await fetchAPI(`/models/${encodeURIComponent(modelName)}`, {
        method: "DELETE",
    });

    if (result.status === "success") {
        showNotification(`Model "${modelName}" deleted successfully!`, "success");
        // refresh the staged badges too (after the models cache updates) so a
        // just-deleted deployed model flips to "removed" without a reload
        loadModelsList().then(loadStaged);
    } else {
        showNotification(`Error deleting model: ${result.message}`, "danger");
    }
}

/**
 * Unload a running model from memory.
 * @param {string} modelName - Name of the model to unload.
 */
async function unloadModel(modelName) {
    const ok = await druids.confirm(`Unload "${modelName}" from memory?`, {
        title: "Unload model",
        confirmLabel: "Unload",
    });
    if (!ok) {
        return;
    }

    const result = await fetchAPI(`/models/${encodeURIComponent(modelName)}/unload`, {
        method: "POST",
    });

    if (result.status === "success") {
        showNotification(`Model "${modelName}" unloaded successfully!`, "success");
        loadRunningModels();
    } else {
        showNotification(`Error unloading model: ${result.message}`, "danger");
    }
}

/**
 * Update a model with streaming progress display.
 * @param {string} modelName - Name of the model to update.
 */
async function updateModel(modelName) {
    const ok = await druids.confirm(`Update "${modelName}"?\n\nThis will download the latest version if available.`, {
        title: "Update model",
        confirmLabel: "Update",
    });
    if (!ok) {
        return;
    }

    // Find the update button for this model. `loading` spins it and drops
    // further clicks, so no separate spinner/lock class is needed.
    const updateBtn = document.querySelector(`.update-btn[data-model="${modelName}"]`);
    updateBtn?.setAttribute("loading", "");

    // Create a temporary status area
    const safeModelName = modelName.replace(/[^a-zA-Z0-9]/g, "-");
    const statusRow = document.createElement("tr");
    statusRow.id = `update-status-${safeModelName}`;
    statusRow.innerHTML = `
        <td colspan="6">
            <div class="df-alert accent lh-progress">
                <div class="df-row lh-progress-row">
                    <span id="update-status-text-${safeModelName}">Updating ${escapeHtml(modelName)}...</span>
                    <span id="update-progress-percent-${safeModelName}" class="df-muted"></span>
                </div>
                <druid-progress id="update-progress-bar-${safeModelName}" value="0" max="100"></druid-progress>
            </div>
        </td>
    `;

    // Insert status row after the model row
    const modelRow = updateBtn?.closest("tr");
    if (modelRow && modelRow.nextSibling) {
        modelRow.parentNode.insertBefore(statusRow, modelRow.nextSibling);
    } else if (modelRow) {
        modelRow.parentNode.appendChild(statusRow);
    }

    const statusText = document.getElementById(`update-status-text-${safeModelName}`);
    const progressBar = document.getElementById(`update-progress-bar-${safeModelName}`);
    const progressPercent = document.getElementById(`update-progress-percent-${safeModelName}`);

    try {
        const response = await fetch(withBasePath("/api/models/update"), {
            method: "POST",
            headers: requestHeaders(),
            body: JSON.stringify({ name: modelName }),
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            const text = decoder.decode(value);
            const lines = text.split("\n");

            for (const line of lines) {
                if (line.startsWith("data: ")) {
                    try {
                        const data = JSON.parse(line.slice(6));

                        if (data.error) {
                            showNotification(`Error updating ${modelName}: ${data.error}`, "danger");
                            statusRow.remove();
                            updateBtn?.removeAttribute("loading");
                            return;
                        }

                        if (data.status && statusText) {
                            statusText.textContent = `${modelName}: ${data.status}`;
                        }

                        if (data.total && data.completed !== undefined) {
                            const percent = Math.round((data.completed / data.total) * 100);
                            const completedStr = formatBytes(data.completed);
                            const totalStr = formatBytes(data.total);
                            if (progressBar) progressBar.setAttribute("value", String(percent));
                            if (progressPercent)
                                progressPercent.textContent = `${completedStr} / ${totalStr} (${percent}%)`;
                        }
                    } catch (e) {}
                }
            }
        }

        showNotification(`Model "${modelName}" updated successfully!`, "success");
        statusRow.remove();

        // Reload models list to update display
        loadModelsList();
    } catch (error) {
        showNotification(`Error updating model: ${error.message}`, "danger");
        statusRow.remove();
    }

    updateBtn?.removeAttribute("loading");
}

/**
 * Reset the table sort order to default. Clearing druid-table's `sort` drops
 * the indicator but not the already-reordered rows, so reload to restore the
 * server's natural order.
 */
function resetTableSort() {
    document.getElementById("models-table")?.removeAttribute("sort");
    loadModelsList();
}
