/**
 * Lamahub - Deploy Tab
 * @description HuggingFace GGUF browse (search + expandable quant rows) and
 * the staged shard-download cache. Deploys run shards-direct: download ->
 * blob upload -> Ollama assembles the split (see HF_DEPLOY_DESIGN.md).
 * Generated markup uses <druid-*> elements and lh-* classes (see app.css).
 */

let hfNextCursor = "";
let hfLastQuery = "";
const hfQuantsCache = {};
let stagedById = {};

/**
 * Format large counts compactly (e.g. 2854700 -> "2.9M").
 * @param {number} count - Raw count.
 * @returns {string} Compact string.
 */
function formatCount(count) {
    if (count >= 1e6) return (count / 1e6).toFixed(1) + "M";
    if (count >= 1e3) return (count / 1e3).toFixed(1) + "K";
    return String(count);
}

/**
 * Suggest an Ollama model name for a repo + quant label.
 * @param {string} repo - HF repo id (author/name).
 * @param {string} label - Quant label (e.g. Q4_K_M).
 * @returns {string} Suggested name (e.g. "qwen3-coder-30b:q4_k_m").
 */
function suggestModelName(repo, label) {
    const base = repo
        .split("/")
        .pop()
        .toLowerCase()
        .replace(/-gguf$/i, "");
    return `${base}:${label.toLowerCase()}`;
}

/**
 * Run a HF search (button / Enter only, no per-keystroke calls).
 * @param {boolean} loadMore - Append the next cursor page instead of resetting.
 */
async function hfSearch(loadMore = false) {
    const input = document.getElementById("hf-search-input");
    const container = document.getElementById("hf-results");
    const moreBtn = document.getElementById("hf-load-more-btn");
    if (!container) return;

    const query = loadMore ? hfLastQuery : (input ? input.value.trim() : "");
    const cursor = loadMore ? hfNextCursor : "";
    if (!loadMore) {
        hfLastQuery = query;
        container.innerHTML = '<tr><td colspan="6" class="df-muted lh-table-placeholder">Searching...</td></tr>';
    }

    const params = new URLSearchParams();
    if (query) params.set("q", query);
    if (cursor) params.set("cursor", cursor);
    const data = await fetchAPI(`/hf/search?${params.toString()}`);

    if (data.error) {
        container.innerHTML = `<tr><td colspan="6" class="lh-table-placeholder df-danger">Error: ${escapeHtml(data.error)}</td></tr>`;
        moreBtn.hidden = true;
        return;
    }

    const rows = (data.items || []).map((item) => {
        const repoAttr = escapeHtml(item.id).replace(/"/g, "&quot;");
        const pipeline = item.pipeline.includes("image") ? "vision" : item.pipeline ? "text" : "-";
        const gated = item.gated ? ' <span class="df-badge">gated</span>' : "";
        return `
        <tr class="lh-hf-row" data-repo="${repoAttr}">
            <td data-sort="${escapeHtml(item.id.toLowerCase())}"><a class="lh-hf-link" href="https://huggingface.co/${repoAttr}" target="_blank" rel="noopener">${escapeHtml(item.id)}</a>${gated}</td>
            <td data-sort="${escapeHtml(item.author.toLowerCase())}">${escapeHtml(item.author)}</td>
            <td>${pipeline}</td>
            <td data-sort="${item.quant_count}"><span class="df-muted">${item.quant_count} ▾</span></td>
            <td data-sort="${escapeHtml(item.updated)}">${formatDate(item.updated)}</td>
            <td data-sort="${item.downloads}">${formatCount(item.downloads)}</td>
        </tr>
    `;
    });

    if (!rows.length && !loadMore) {
        container.innerHTML = '<tr><td colspan="6" class="df-muted lh-table-placeholder">No GGUF models found</td></tr>';
        moreBtn.hidden = true;
        return;
    }

    if (loadMore) {
        container.insertAdjacentHTML("beforeend", rows.join(""));
    } else {
        container.innerHTML = rows.join("");
    }
    hfNextCursor = data.next_cursor || "";
    moreBtn.hidden = !hfNextCursor;
}

/**
 * Reset sort order on both Deploy-tab tables back to their defaults.
 * HF results: clear indicators + re-run the search (download-desc). Staged:
 * clear indicators + reload (cheap, local — the store's natural order).
 */
function resetHfSort() {
    const results = document.getElementById("hf-results-table");
    if (results) {
        results.querySelectorAll("th[aria-sort]").forEach((th) => th.removeAttribute("aria-sort"));
        if (results.querySelector(".lh-hf-row")) {
            hfSearch(false);
        }
    }
    const staged = document.getElementById("staged-table");
    if (staged) {
        staged.querySelectorAll("th[aria-sort]").forEach((th) => th.removeAttribute("aria-sort"));
        loadStaged();
    }
}

/**
 * Clear the HF results back to the empty placeholder (collapse the browse table).
 */
function clearHfResults() {
    const input = document.getElementById("hf-search-input");
    const container = document.getElementById("hf-results");
    const moreBtn = document.getElementById("hf-load-more-btn");
    const table = document.getElementById("hf-results-table");
    if (input) input.value = "";
    if (container) {
        container.innerHTML =
            '<tr><td colspan="6" class="df-muted lh-table-placeholder">Search HuggingFace for GGUF models to deploy</td></tr>';
    }
    if (moreBtn) moreBtn.hidden = true;
    if (table) table.querySelectorAll("th[aria-sort]").forEach((th) => th.removeAttribute("aria-sort"));
    hfNextCursor = "";
    hfLastQuery = "";
}

/**
 * Toggle a repo row's quant sub-table (lazy-fetched, cached).
 * @param {HTMLTableRowElement} row - The clicked repo row.
 */
async function toggleQuantRow(row) {
    const existing = row.nextElementSibling;
    if (existing && existing.classList.contains("lh-quant-row")) {
        existing.remove();
        return;
    }

    const repo = row.dataset.repo;
    const quantRow = document.createElement("tr");
    quantRow.className = "lh-quant-row";
    quantRow.innerHTML =
        '<td colspan="6" class="lh-quant-cell"><span class="df-muted">Loading quants...</span></td>';
    row.after(quantRow);

    if (!hfQuantsCache[repo]) {
        const data = await fetchAPI(`/hf/repo/${repo}/quants`);
        hfQuantsCache[repo] = data.quants || [];
    }
    const quants = hfQuantsCache[repo];

    if (!quants.length) {
        quantRow.firstElementChild.innerHTML = '<span class="df-muted">No deployable GGUF files</span>';
        return;
    }

    const repoAttr = escapeHtml(repo).replace(/"/g, "&quot;");
    quantRow.firstElementChild.innerHTML = `
        <table class="lh-table lh-quant-table">
            <tbody>
                ${quants
                    .map((quant) => {
                        const famAttr = escapeHtml(quant.family).replace(/"/g, "&quot;");
                        const labelAttr = escapeHtml(quant.label).replace(/"/g, "&quot;");
                        return `
                <tr>
                    <td><span class="df-badge">${escapeHtml(quant.label)}</span></td>
                    <td>${formatBytes(quant.total_size)}</td>
                    <td>${quant.shards.length} shard${quant.shards.length === 1 ? "" : "s"}</td>
                    <td class="no-sort">
                        <druid-button variant="soft"
                                onclick="deployQuant('${repoAttr}', '${famAttr}', '${labelAttr}')">Deploy</druid-button>
                    </td>
                </tr>`;
                    })
                    .join("")}
            </tbody>
        </table>
    `;
}

let deployPollTimer = null;

/**
 * Prompt for a model name and start a deploy on the active endpoint.
 *
 * The deploy runs server-side as a detached task, so it keeps going if the
 * browser is closed during a multi-GB download; the UI only observes it.
 * @param {string} repo - HF repo id.
 * @param {string} family - Quant family base path.
 * @param {string} label - Quant label for the name suggestion.
 */
async function deployQuant(repo, family, label) {
    const suggested = suggestModelName(repo, label);
    const input = await druids.prompt(`Model name on the endpoint (empty = "${suggested}"):`, {
        title: `Deploy ${label}`,
        placeholder: suggested,
        confirmLabel: "Deploy",
    });
    if (input === null) return; // cancelled
    const modelName = input.trim() || suggested;
    startDeploy(repo, family, modelName);
}

/**
 * Start a deploy on the active endpoint and attach the progress strip.
 * @param {string} repo - HF repo id.
 * @param {string} family - Quant family base path.
 * @param {string} modelName - Target model name on the endpoint.
 */
async function startDeploy(repo, family, modelName) {
    const result = await fetchAPI("/hf/deploy", {
        method: "POST",
        body: JSON.stringify({ repo, family, model_name: modelName }),
    });
    if (result.status !== "started") {
        // refused (e.g. a deploy is already running — they are serialized)
        showNotification(result.message || "Deploy could not be started", "warning");
        return;
    }
    startDeployTracking();
}

/**
 * Begin polling the server-side deploy status and driving the progress strip.
 * Safe to call repeatedly — only one poller runs at a time.
 */
function startDeployTracking() {
    if (deployPollTimer) return;
    pollDeployStatus();
    deployPollTimer = window.setInterval(pollDeployStatus, 1000);
}

/**
 * Poll /hf/deploy/status once and reflect it onto the progress strip. Ends the
 * poll (and refreshes the dashboard) when the deploy finishes.
 */
async function pollDeployStatus() {
    const statusRow = document.getElementById("hf-deploy-status");
    const statusText = document.getElementById("hf-deploy-status-text");
    const progressBar = document.getElementById("hf-deploy-progress");
    const progressPercent = document.getElementById("hf-deploy-progress-percent");
    if (!statusRow) return;

    const data = await fetchAPI("/hf/deploy/status");

    if (!data || !data.active) {
        window.clearInterval(deployPollTimer);
        deployPollTimer = null;
        statusRow.hidden = true;
        if (data && data.error) {
            showNotification(`Deploy error: ${data.error}`, "danger");
        } else if (data && data.model) {
            showNotification(`Deployed "${data.model}" successfully!`, "success");
        }
        loadStaged();
        loadModelsList();
        loadTotalModels();
        loadTotalStorage();
        return;
    }

    statusRow.hidden = false;
    statusText.textContent = data.status || "Deploying...";
    if (data.total > 0) {
        const percent = Math.round((data.completed / data.total) * 100);
        progressBar.setAttribute("value", String(percent));
        progressPercent.textContent = `${formatBytes(data.completed)} / ${formatBytes(data.total)} (${percent}%)`;
    } else if (data.stage === "create") {
        progressBar.setAttribute("value", "100");
        progressPercent.textContent = "assembling…";
    } else {
        progressPercent.textContent = "";
    }
}

/**
 * Load the staged families into the Deploy tab table and the rail card.
 */
async function loadStaged() {
    const list = document.getElementById("staged-list");
    const usage = document.getElementById("staged-usage");
    const card = document.getElementById("staged-card");
    const summary = document.getElementById("staged-summary");
    if (!list) return;

    const data = await fetchAPI("/hf/staging");
    const families = data.families || [];
    stagedById = Object.fromEntries(families.map((family) => [family.id, family]));
    const totalBytes = families.reduce((sum, family) => sum + (family.disk_size || 0), 0);

    if (usage) {
        usage.textContent = families.length
            ? `${formatBytes(totalBytes)}${data.max_gb ? ` / ${data.max_gb} GB cap` : ""}`
            : "";
    }

    // rail card: compact count + size summary, hidden when empty
    if (card && summary) {
        card.hidden = families.length === 0;
        summary.innerHTML = `
            <div class="df-stat-number">${formatBytes(totalBytes)}</div>
            <span class="df-stat-caption">${families.length} shard famil${families.length === 1 ? "y" : "ies"} cached</span>
        `;
    }

    if (!families.length) {
        list.innerHTML = '<tr><td colspan="6" class="df-muted lh-table-placeholder">Nothing staged</td></tr>';
        return;
    }

    list.innerHTML = families
        .map((family) => {
            const idAttr = escapeHtml(family.id).replace(/"/g, "&quot;");
            const endpoints = (family.endpoints || []).map((url) => escapeHtml(url)).join("<br>") || "-";
            const shards = family.shards || [];
            const shardCount = shards.length;
            const doneShards = shards.filter((shard) => shard.sha256).length;
            // "downloading" that isn't the live deploy = an interrupted pull; the
            // done/total shard count makes the real state visible and shows it is
            // resumable (re-deploy continues via download-resume + blob dedupe)
            const statusLabel =
                family.status === "downloading"
                    ? `downloading ${doneShards}/${shardCount}`
                    : escapeHtml(family.status || "-");
            return `
        <tr>
            <td data-sort="${idAttr.toLowerCase()}">${escapeHtml(family.repo)} <span class="df-badge">${escapeHtml(family.quant)}</span></td>
            <td data-sort="${family.disk_size || 0}">${formatBytes(family.disk_size || 0)}</td>
            <td data-sort="${shardCount}">${shardCount}</td>
            <td><span class="df-badge${family.status === "deployed" ? " ok" : family.status === "downloading" ? " warn" : ""}">${statusLabel}</span></td>
            <td class="df-muted">${endpoints}</td>
            <td class="no-sort">
                <div class="lh-row-actions">
                    <druid-icon-button circle small class="lh-redeploy" icon="rotate-cw"
                            onclick="redeployStaged('${idAttr}')"
                            label="Redeploy ${escapeHtml(family.model_name || "")} to the current endpoint"></druid-icon-button>
                    <druid-icon-button circle small class="df-danger" icon="x"
                            onclick="pruneStaged('${idAttr}')"
                            label="Prune ${idAttr} from cache"></druid-icon-button>
                </div>
            </td>
        </tr>
    `;
        })
        .join("");
}

/**
 * Redeploy a staged family to the current endpoint (re-pushes the cached
 * shards; instant via blob dedupe unless the endpoint is missing them).
 * @param {string} familyId - Staged family id.
 */
function redeployStaged(familyId) {
    const family = stagedById[familyId];
    if (!family) return;
    startDeploy(family.repo, family.family, family.model_name);
}

/**
 * Prune one staged family from the local cache after confirmation.
 * @param {string} familyId - Staged family id.
 */
async function pruneStaged(familyId) {
    const ok = await druids.confirm(
        "Remove these cached shards from disk? Models already deployed to an endpoint are not affected.",
        { title: "Prune staged download", confirmLabel: "Prune", danger: true },
    );
    if (!ok) return;

    const result = await fetchAPI(`/hf/staging/${encodeURIComponent(familyId)}`, { method: "DELETE" });
    if (result.status === "success") {
        showNotification("Pruned staged download", "success");
    } else {
        showNotification(`Error pruning: ${result.message}`, "danger");
    }
    loadStaged();
}

/**
 * Wire up the Deploy tab controls.
 */
async function initDeployTab() {
    const searchBtn = document.getElementById("hf-search-btn");
    if (searchBtn) {
        searchBtn.addEventListener("click", () => hfSearch(false));
    }

    const searchInput = document.getElementById("hf-search-input");
    if (searchInput) {
        searchInput.addEventListener("keypress", (e) => {
            if (e.key === "Enter") {
                hfSearch(false);
            }
        });
    }

    const moreBtn = document.getElementById("hf-load-more-btn");
    if (moreBtn) {
        moreBtn.addEventListener("click", () => hfSearch(true));
    }

    const resetSortBtn = document.getElementById("hf-reset-sort-btn");
    if (resetSortBtn) {
        resetSortBtn.addEventListener("click", resetHfSort);
    }

    const clearResultsBtn = document.getElementById("hf-clear-results-btn");
    if (clearResultsBtn) {
        clearResultsBtn.addEventListener("click", clearHfResults);
    }

    // expand/collapse quant sub-rows; ignore clicks on the repo link
    const results = document.getElementById("hf-results");
    if (results) {
        results.addEventListener("click", (event) => {
            if (event.target.closest("a")) return;
            const row = event.target.closest(".lh-hf-row");
            if (row) toggleQuantRow(row);
        });
    }

    // sorting reorders plain rows: collapse open quant sub-rows first so they
    // can't be scrambled away from their repo row (sortable.js emits sort-start)
    const resultsTable = document.getElementById("hf-results-table");
    if (resultsTable) {
        resultsTable.addEventListener("sort-start", () => {
            resultsTable.querySelectorAll(".lh-quant-row").forEach((row) => row.remove());
        });
    }

    loadStaged();

    // if a deploy is still running (e.g. the page was reloaded mid-download),
    // re-attach the progress strip to it
    const status = await fetchAPI("/hf/deploy/status");
    if (status && status.active) {
        startDeployTracking();
    }
}
