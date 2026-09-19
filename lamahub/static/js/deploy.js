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
        container.innerHTML = placeholderRow("Searching…");
    }

    const params = new URLSearchParams();
    if (query) params.set("q", query);
    if (cursor) params.set("cursor", cursor);
    // spin whichever button asked for the page; the width stays put
    const busyBtn = loadMore ? moreBtn : document.getElementById("hf-search-btn");
    busyBtn?.setAttribute("loading", "");
    const data = await fetchAPI(`/hf/search?${params.toString()}`);
    busyBtn?.removeAttribute("loading");

    if (data.error) {
        container.innerHTML = placeholderRow(`Error: ${escapeHtml(data.error)}`, "df-danger");
        moreBtn.hidden = true;
        return;
    }

    const rows = (data.items || []).map((item) => {
        const repoAttr = escapeAttr(item.id);
        const pipeline = item.pipeline.includes("image") ? "vision" : item.pipeline ? "text" : "-";
        const gated = item.gated ? ' <span class="df-badge">gated</span>' : "";
        return `
        <tr class="lh-hf-row" data-repo="${repoAttr}">
            <td data-value="${escapeHtml(item.id.toLowerCase())}"><a class="lh-hf-link" href="https://huggingface.co/${repoAttr}" target="_blank" rel="noopener">${escapeHtml(item.id)}</a>${gated}</td>
            <td data-value="${escapeHtml(item.author.toLowerCase())}">${escapeHtml(item.author)}</td>
            <td>${pipeline}</td>
            <td data-value="${item.quant_count}"><druid-popover placement="left" class="lh-quant-pop" data-repo="${repoAttr}"><druid-button slot="trigger" variant="outline" class="lh-quant-trigger">${item.quant_count} ▾</druid-button><div class="lh-quant-pop-body"><div class="lh-quant-pop-empty df-muted">Loading quants…</div></div></druid-popover></td>
            <td data-value="${escapeHtml(item.updated)}">${formatDate(item.updated)}</td>
            <td class="num" data-value="${item.downloads}">${formatCount(item.downloads)}</td>
        </tr>
    `;
    });

    if (!rows.length && !loadMore) {
        container.innerHTML = placeholderRow("No GGUF models found");
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
        results.removeAttribute("sort");
        if (results.querySelector(".lh-hf-row")) {
            hfSearch(false);
        }
    }
    const staged = document.getElementById("staged-table");
    if (staged) {
        staged.removeAttribute("sort");
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
        container.innerHTML = placeholderRow("Search HuggingFace for GGUF models to deploy");
    }
    if (moreBtn) moreBtn.hidden = true;
    if (table) table.removeAttribute("sort");
    hfNextCursor = "";
    hfLastQuery = "";
}

// Quant list lives in a <druid-popover> (druids 1.0.3 — the primitive that
// resolved the GAPS.md "anchored popover" gap). It owns top-layer placement,
// left-of-trigger positioning with flip, light-dismiss (outside-click / Esc),
// and same-trigger toggle, so all the hand-rolled positioning / dismissal /
// scroll-close boilerplate is gone. The trigger is a <druid-button
// variant="outline"> (the dropdown-trigger look, also a resolved gap). The
// panel's content is lazy-fetched the first time it opens (popover-toggle) so a
// search of N repos costs N zero quant calls up front. Deploy = clicking a
// quant row.

/**
 * Lazy-fill a quant popover's body the first time it opens (cached per repo).
 * @param {HTMLElement} popEl - The <druid-popover class="lh-quant-pop"> element.
 */
async function fillQuantPopover(popEl) {
    if (popEl.dataset.loaded === "1") return;
    const repo = popEl.dataset.repo;
    const body = popEl.querySelector(".lh-quant-pop-body");
    if (!repo || !body) return;

    if (!hfQuantsCache[repo]) {
        const data = await fetchAPI(`/hf/repo/${repo}/quants`);
        hfQuantsCache[repo] = data.quants || [];
    }
    popEl.dataset.loaded = "1";

    const quants = hfQuantsCache[repo];
    if (!quants.length) {
        body.innerHTML = '<div class="lh-quant-pop-empty df-muted">No deployable GGUF files</div>';
    } else {
        const repoAttr = escapeAttr(repo);
        body.innerHTML = `
        <table class="lh-quant-poptable"><tbody>
            ${quants
                .map((quant) => {
                    const famAttr = escapeAttr(quant.family);
                    const labelAttr = escapeAttr(quant.label);
                    const shardText = `${quant.shards.length} shard${quant.shards.length === 1 ? "" : "s"}`;
                    return `
                <tr class="lh-quant-pop-row"
                        onclick="selectQuant(event, '${repoAttr}', '${famAttr}', '${labelAttr}')"
                        title="Deploy ${escapeHtml(quant.label)}">
                    <td><span class="df-badge lh-quant-label">${escapeHtml(quant.label)}</span></td>
                    <td class="lh-quant-meta lh-quant-size">${formatBytes(quant.total_size)}</td>
                    <td class="lh-quant-meta">${shardText}</td>
                </tr>`;
                })
                .join("")}
        </tbody></table>
    `;
    }
    // The async fill changed the panel's size; nudge druid-popover to re-place
    // it against the trigger with the final dimensions (no-op if it closed).
    if (popEl.open && typeof popEl.position === "function") popEl.position();
}

/**
 * Deploy a quant chosen from the popover: close the popover, then open the
 * model-name confirm dialog (deployQuant).
 * @param {Event} event - The row click (used to find the owning popover).
 */
function selectQuant(event, repo, family, label) {
    const pop = event.target.closest("druid-popover");
    if (pop && typeof pop.hide === "function") pop.hide();
    deployQuant(repo, family, label);
}

let deployPollTimer = null;
// highest finished-job id already reported, so each result toasts exactly once
// (also across page reloads: seeded from the history on init)
let lastReportedJob = 0;

/**
 * Prompt for a model name and queue a deploy on the active endpoint.
 *
 * Deploys run server-side as detached tasks, one at a time (the rest wait in a
 * FIFO queue), so they keep going if the browser is closed during a multi-GB
 * download; the UI only observes them.
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
 * Queue a deploy on the active endpoint and attach the progress strip.
 * @param {string} repo - HF repo id.
 * @param {string} family - Quant family base path.
 * @param {string} modelName - Target model name on the endpoint.
 */
async function startDeploy(repo, family, modelName) {
    const result = await fetchAPI("/hf/deploy", {
        method: "POST",
        body: JSON.stringify({ repo, family, model_name: modelName }),
    });
    if (result.status === "queued") {
        showNotification(`Queued "${modelName}" (#${result.position})`, "info");
    } else if (result.status !== "started") {
        // refused (e.g. the same family is already queued for this endpoint)
        showNotification(result.message || "Deploy could not be started", "warning");
        return;
    }
    startDeployTracking();
}

/**
 * Remove a waiting deploy from the server-side queue.
 * @param {number} jobId - Queue job id.
 */
async function cancelQueuedDeploy(jobId) {
    const result = await fetchAPI(`/hf/deploy/queue/${jobId}`, { method: "DELETE" });
    if (result.status !== "success") {
        showNotification(result.message || "Could not cancel", "warning");
    }
    pollDeployStatus();
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
 * Render the waiting deploys under the progress strip.
 * @param {Array<object>} queue - Waiting jobs from /hf/deploy/status.
 */
function renderDeployQueue(queue) {
    const list = document.getElementById("hf-deploy-queue");
    if (!list) return;
    list.hidden = !queue.length;
    list.innerHTML = queue
        .map(
            (job, index) => `
        <div class="df-row lh-queue-row">
            <span class="df-muted">#${index + 1}</span>
            <span class="lh-queue-name">${escapeHtml(job.model_name)}</span>
            <span class="df-muted lh-queue-repo">${escapeHtml(job.repo)}</span>
            <druid-icon-button circle small class="df-danger" icon="x"
                    onclick="cancelQueuedDeploy(${job.id})"
                    label="Remove ${escapeHtml(job.model_name)} from the queue"></druid-icon-button>
        </div>`,
        )
        .join("");
}

/**
 * Poll /hf/deploy/status once and reflect it onto the progress strip + queue.
 * Reports every job that finished since the last poll (several can finish
 * between two polls when cached shards redeploy instantly), refreshes the
 * dashboard after each, and ends the poll once the queue has drained.
 */
async function pollDeployStatus() {
    const statusRow = document.getElementById("hf-deploy-status");
    const statusText = document.getElementById("hf-deploy-status-text");
    const progressBar = document.getElementById("hf-deploy-progress");
    const progressPercent = document.getElementById("hf-deploy-progress-percent");
    if (!statusRow) return;

    const data = await fetchAPI("/hf/deploy/status");
    if (!data || data.error) return;

    const finished = (data.history || []).filter((job) => job.id > lastReportedJob);
    for (const job of finished) {
        if (job.error) {
            showNotification(`Deploy of "${job.model_name}" failed: ${job.error}`, "danger");
        } else if (job.model) {
            showNotification(`Deployed "${job.model}" successfully!`, "success");
        }
        lastReportedJob = job.id;
    }
    if (finished.length) {
        // refresh models first so loadStaged sees the just-deployed model in the
        // cache (otherwise the fresh family would flash as "removed")
        await loadModelsList();
        loadStaged();
    }

    renderDeployQueue(data.queue || []);
    const job = data.current;
    if (!data.active || !job) {
        if (!data.active) {
            window.clearInterval(deployPollTimer);
            deployPollTimer = null;
        }
        statusRow.hidden = true;
        return;
    }

    statusRow.hidden = false;
    const modelEl = document.getElementById("hf-deploy-model");
    const stageEl = document.getElementById("hf-deploy-stage");
    const rateEl = document.getElementById("hf-deploy-rate");
    modelEl.textContent = job.model_name;
    modelEl.title = `${job.model_name}\n${job.repo}`;
    stageEl.textContent = deployStageLabel(job);
    // status carries the file and any retry note, e.g. "downloading x.gguf (1/2) — retry 1/6"
    statusText.textContent = job.status || "";
    statusText.title = job.status || "";

    rateEl.textContent = "";
    if (job.total > 0) {
        renderProgress(progressBar, progressPercent, job.completed, job.total);
        if (job.rate > 0) {
            const eta = (job.total - job.completed) / job.rate;
            rateEl.textContent = `${formatBytes(job.rate)}/s · ${formatDuration(eta)} left`;
        } else if (job.stage === "download" || job.stage === "upload") {
            rateEl.textContent = "waiting for data…";
        }
    } else if (job.stage === "create") {
        progressBar.setAttribute("value", "100");
        progressPercent.textContent = "assembling…";
    } else {
        progressBar.setAttribute("value", "0");
        progressPercent.textContent = "";
    }
}

/**
 * Short stage badge for the progress strip, e.g. "download 1/2".
 * @param {Object} job - The live deploy record.
 * @returns {string} Label.
 */
function deployStageLabel(job) {
    const action = (job.status || "").split(" ")[0];
    if (action === "verifying" || action === "reconnecting") return action;
    if (job.stage === "create") return "assemble";
    if (job.total_shards > 0) return `${job.stage} ${job.shard}/${job.total_shards}`;
    return job.stage || "starting";
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

    // Verify "deployed" families against what actually lives on the active
    // endpoint: if the model was deleted since, flip the badge to "removed" so
    // the stale state is visible and the redeploy action reads as meaningful.
    // Scoped to the active endpoint (families remember which endpoints they
    // reached) so a model deployed only elsewhere isn't wrongly flagged. Reads
    // the shared lastModels (no extra fetch); when it hasn't loaded yet the
    // check is simply skipped until the next refresh.
    const activeEndpoint = getSelectedEndpoint();
    // Compare names case-INSENSITIVELY: Ollama canonicalizes a known quant tag
    // on create (we send "qwen3-0.6b:q4_k_m", /api/tags reports it back as
    // "qwen3-0.6b:Q4_K_M"), so the meta.model_name we stored can differ in case
    // from the registered name. A case-sensitive Set.has() then wrongly reports
    // a live model as "removed". Lower-casing both sides fixes that.
    const modelSet = lastModels ? new Set(lastModels.map((m) => m.name.toLowerCase())) : null;

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
        list.innerHTML = placeholderRow("Nothing staged");
        return;
    }

    list.innerHTML = families
        .map((family) => {
            const idAttr = escapeAttr(family.id);
            const endpoints = (family.endpoints || []).map((url) => escapeHtml(url)).join("<br>") || "-";
            const shards = family.shards || [];
            const shardCount = shards.length;
            const doneShards = shards.filter((shard) => shard.sha256).length;
            // a deployed family whose model is no longer on the (active) endpoint
            // reads as "removed" — it dropped off the server and wants a redeploy
            let status = family.status;
            const modelKey = (family.model_name || "").toLowerCase();
            if (
                status === "deployed" &&
                modelSet &&
                (family.endpoints || []).includes(activeEndpoint) &&
                !modelSet.has(modelKey) &&
                !modelSet.has(`${modelKey}:latest`)
            ) {
                status = "removed";
            }
            // "downloading" that isn't the live deploy = an interrupted pull; the
            // done/total shard count makes the real state visible and shows it is
            // resumable (re-deploy continues via download-resume + blob dedupe)
            const statusLabel =
                status === "downloading"
                    ? `downloading ${doneShards}/${shardCount}`
                    : escapeHtml(status || "-");
            return `
        <tr>
            <td data-value="${idAttr.toLowerCase()}">${escapeHtml(family.repo)} <span class="df-badge">${escapeHtml(family.quant)}</span></td>
            <td class="num" data-value="${family.disk_size || 0}">${formatBytes(family.disk_size || 0)}</td>
            <td class="num" data-value="${shardCount}">${shardCount}</td>
            <td><span class="df-badge${status === "deployed" ? " ok" : status === "downloading" ? " warn" : status === "removed" ? " warn" : ""}">${statusLabel}</span></td>
            <td class="df-muted">${endpoints}</td>
            <td>
                <div class="df-row gap-sm lh-row-actions">
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

    // Lazy-load a repo's quants the first time its popover opens. druid-popover
    // owns open/close and follows its anchor through inner-container scrolling
    // (capture-phase listener since 1.0.5), so we only supply the content.
    // popover-toggle bubbles; listen on the stable table, not the tbody.
    const resultsTable = document.getElementById("hf-results-table");
    if (resultsTable) {
        resultsTable.addEventListener("popover-toggle", (event) => {
            const pop = event.target.closest("druid-popover.lh-quant-pop");
            if (!pop) return;
            const open = !!(event.detail && event.detail.open);
            // accent the trigger only while its popover is open (see .lh-quant-
            // trigger in app.css — quiet neutral border at rest, accent when open)
            pop.querySelector('[slot="trigger"]')?.classList.toggle("is-open", open);
            if (open) fillQuantPopover(pop);
        });
    }

    // note: loadStaged is driven by refreshAllData (after the models cache warms)
    // so the "deployed" badges verify against a loaded model list, not a cold one

    // if deploys are still running (e.g. the page was reloaded mid-download),
    // re-attach the progress strip; results finished before the reload are
    // treated as already reported
    const status = await fetchAPI("/hf/deploy/status");
    for (const job of (status && status.history) || []) {
        lastReportedJob = Math.max(lastReportedJob, job.id);
    }
    if (status && status.active) {
        startDeployTracking();
    }
}
