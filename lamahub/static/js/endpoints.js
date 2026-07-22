/**
 * Lamahub - Endpoint Switcher
 * @description Loads the configured Ollama endpoints and lets the user switch
 * between them via the navbar <druid-select>. The selection is stored
 * client-side and sent with every request via the X-Ollama-Url header (see
 * requestHeaders in utils.js).
 */

/**
 * Reload every endpoint-scoped section of the dashboard.
 */
function refreshAllData() {
    loadRunningModels();
    loadTotalModels();
    loadTotalStorage();
    // loadStaged after loadModelsList so the Deploy tab's "deployed" badges are
    // verified against a warm model cache (see lastModels in models.js)
    loadFixedModels().then(loadModelsList).then(loadStaged);
    loadChatModelSelect();
}

/**
 * Show the selected endpoint's URL next to the switcher as a copyable button
 * (reuses the model-name copy styling and click handler).
 * @param {string} url - The endpoint URL to display.
 */
function updateEndpointUrlDisplay(url) {
    const el = document.getElementById("endpoint-url");
    if (!el) return;

    if (!url) {
        el.innerHTML = "";
        return;
    }

    const escaped = escapeHtml(url);
    const attr = escaped.replace(/"/g, "&quot;");
    el.innerHTML = `
        <span class="lh-endpoint-dot" aria-hidden="true"></span>
        <button type="button"
                class="model-name-button"
                data-model-name="${attr}"
                title="Copy endpoint URL"
                aria-label="Copy endpoint URL ${attr}">
            <span class="model-name-label">${escaped}</span>
        </button>
    `;
}

/**
 * Fetch configured endpoints and populate the navbar switcher.
 * @returns {Promise<void>} Resolves once the switcher is ready.
 */
async function loadEndpoints() {
    const select = document.getElementById("endpoint-select");
    if (!select) return;

    const data = await fetchAPI("/endpoints");
    const endpoints = (data && data.endpoints) || [];
    if (!endpoints.length) {
        select.hidden = true;
        return;
    }

    // Drop any stored selection that is no longer configured, falling back to
    // the server-reported default.
    const defaultUrl = data.default || endpoints[0].url;
    const stored = getSelectedEndpoint();
    const current = endpoints.some((e) => e.url === stored) ? stored : defaultUrl;
    setSelectedEndpoint(current);
    updateEndpointUrlDisplay(current);

    // druid-select reads and watches its light-DOM <option> children
    select.innerHTML = endpoints
        .map((endpoint) => {
            const label = endpoint.default ? `${endpoint.name} [Default]` : endpoint.name;
            return `<option value="${escapeHtml(endpoint.url)}">${escapeHtml(label)}</option>`;
        })
        .join("");
    select.value = current;

    // A switcher only makes sense with more than one endpoint.
    select.hidden = endpoints.length <= 1;

    select.addEventListener("change", (e) => {
        setSelectedEndpoint(e.detail.value);
        updateEndpointUrlDisplay(e.detail.value);
        refreshAllData();
    });
}
