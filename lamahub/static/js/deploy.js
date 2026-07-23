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
            <td data-sort="${item.quant_count}"><druid-popover placement="left" class="lh-quant-pop" data-repo="${repoAttr}"><druid-button slot="trigger" variant="outline" class="lh-quant-trigger">${item.quant_count} ▾</druid-button><div class="lh-quant-pop-body"><div class="lh-quant-pop-empty df-muted">Loading quants…</div></div></druid-popover></td>
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
        const repoAttr = escapeHtml(repo).replace(/"/g, "&quot;");
        body.innerHTML = `
        <table class="lh-quant-poptable"><tbody>
            ${quants
                .map((quant) => {
                    const famAttr = escapeHtml(quant.family).replace(/"/g, "&quot;");
                    const labelAttr = escapeHtml(quant.label).replace(/"/g, "&quot;");
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
        // refresh models first so loadStaged sees the just-deployed model in the
        // cache (otherwise the fresh family would flash as "removed")
        await loadModelsList();
        loadStaged();
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

    // Verify "deployed" families against what actually lives on the active
    // endpoint: if the model was deleted since, flip the badge to "removed" so
    // the stale state is visible and the redeploy action reads as meaningful.
    // Scoped to the active endpoint (families remember which endpoints they
    // reached) so a model deployed only elsewhere isn't wrongly flagged. Reuses
    // the models list cached by loadModelsList (no extra fetch); when it hasn't
    // loaded yet the check is simply skipped until the next refresh.
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
            <td data-sort="${idAttr.toLowerCase()}">${escapeHtml(family.repo)} <span class="df-badge">${escapeHtml(family.quant)}</span></td>
            <td data-sort="${family.disk_size || 0}">${formatBytes(family.disk_size || 0)}</td>
            <td data-sort="${shardCount}">${shardCount}</td>
            <td><span class="df-badge${status === "deployed" ? " ok" : status === "downloading" ? " warn" : status === "removed" ? " warn" : ""}">${statusLabel}</span></td>
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

    // Lazy-load a repo's quants the first time its popover opens. druid-popover
    // handles the open/close/positioning itself; we only need the content.
    // popover-toggle bubbles, so listen on the TABLE, not the tbody
    // (#hf-results): sortable.min.js replaces the tbody with a clone on sort,
    // which would drop a tbody-bound listener. The table is stable across sorts.
    const resultsTable = document.getElementById("hf-results-table");
    if (resultsTable) {
        resultsTable.addEventListener("popover-toggle", (event) => {
            const pop = event.target.closest("druid-popover.lh-quant-pop");
            if (!pop) return;
            const open = !!(event.detail && event.detail.open);
            // accent the trigger only while its popover is open (see .lh-quant-
            // trigger in app.css — quiet neutral border at rest, accent when open)
            pop.querySelector('[slot="trigger"]')?.classList.toggle("is-open", open);
            if (open) {
                fillQuantPopover(pop);
                // druid-popover follows its anchor on *window* scroll, but the
                // results list scrolls in an inner container (no window scroll),
                // so the panel would hang detached. Close on any scroll outside
                // the panel instead. capture:true catches inner-container scrolls;
                // an internal scroll of the panel's own list is ignored.
                const onScroll = (e) => {
                    if (e.target instanceof Node && pop.contains(e.target)) return;
                    pop.hide();
                };
                pop._closeOnScroll = onScroll;
                window.addEventListener("scroll", onScroll, true);
            } else if (pop._closeOnScroll) {
                window.removeEventListener("scroll", pop._closeOnScroll, true);
                pop._closeOnScroll = null;
            }
        });
    }

    // note: loadStaged is driven by refreshAllData (after the models cache warms)
    // so the "deployed" badges verify against a loaded model list, not a cold one

    // if a deploy is still running (e.g. the page was reloaded mid-download),
    // re-attach the progress strip to it
    const status = await fetchAPI("/hf/deploy/status");
    if (status && status.active) {
        startDeployTracking();
    }
}
