/**
 * Lamahub - Utility Functions
 * @description Shared utilities for theme, API calls, and formatting.
 */

// Initialize markdown-it with highlight.js
let md;
const APP_BASE_PATH = (window.LLMM_BASE_PATH || "").replace(/\/$/, "");
const ENDPOINT_STORAGE_KEY = "ollama_endpoint";

function withBasePath(path = "") {
    if (!path) {
        return APP_BASE_PATH || "/";
    }

    const normalizedPath = path.startsWith("/") ? path : `/${path}`;
    return `${APP_BASE_PATH}${normalizedPath}`;
}

/**
 * Get the user's currently selected Ollama endpoint URL.
 * @returns {string} Stored endpoint URL, or "" to use the server default.
 */
function getSelectedEndpoint() {
    return localStorage.getItem(ENDPOINT_STORAGE_KEY) || "";
}

/**
 * Persist the selected Ollama endpoint URL.
 * @param {string} url - Endpoint URL, or falsy to clear the selection.
 */
function setSelectedEndpoint(url) {
    if (url) {
        localStorage.setItem(ENDPOINT_STORAGE_KEY, url);
    } else {
        localStorage.removeItem(ENDPOINT_STORAGE_KEY);
    }
}

/**
 * Build request headers, including the selected Ollama endpoint when set.
 * @param {Object} [extra] - Additional headers to merge in.
 * @returns {Object} Header map for fetch().
 */
function requestHeaders(extra = {}) {
    const headers = { "Content-Type": "application/json", ...extra };
    const endpoint = getSelectedEndpoint();
    if (endpoint) {
        headers["X-Ollama-Url"] = endpoint;
    }
    return headers;
}

function initializeMarkdown() {
    if (typeof markdownit !== "undefined" && typeof hljs !== "undefined") {
        console.log("Initializing markdown-it with highlight.js");
        md = markdownit({
            html: false,
            linkify: true,
            typographer: true,
            highlight: function (str, lang) {
                if (lang && hljs.getLanguage(lang)) {
                    try {
                        return hljs.highlight(str, { language: lang }).value;
                    } catch (__) {}
                }
                return "";
            },
        });
        console.log("Markdown-it initialized successfully");
    } else {
        console.warn("markdown-it or highlight.js not available", {
            markdownit: typeof markdownit,
            hljs: typeof hljs,
        });
    }
}

// Try to initialize immediately
initializeMarkdown();

/**
 * Make an API request to the backend.
 * @param {string} endpoint - API endpoint path.
 * @param {Object} options - Fetch options.
 * @returns {Promise<Object>} Response data or error object.
 */
async function fetchAPI(endpoint, options = {}) {
    try {
        const response = await fetch(withBasePath(`/api${endpoint}`), {
            headers: requestHeaders(),
            ...options,
        });
        return await response.json();
    } catch (error) {
        console.error("API Error:", error);
        return { error: error.message };
    }
}

/**
 * Format bytes to human-readable string.
 * @param {number} bytes - Byte count.
 * @returns {string} Formatted string (e.g., "1.5 GB").
 */
function formatBytes(bytes) {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return (bytes / Math.pow(k, i)).toFixed(1) + " " + sizes[i];
}

/**
 * Format date string to locale date.
 * @param {string} dateString - ISO date string.
 * @returns {string} Formatted locale date.
 */
function formatDate(dateString) {
    const date = new Date(dateString);
    return date.toLocaleDateString();
}

// legacy type names → druids.toast types
const TOAST_TYPES = { success: "ok", danger: "danger", warning: "warn", info: "info" };

/**
 * Show a temporary notification message via druids.toast (plain text).
 * @param {string} message - Message to display.
 * @param {string} type - success, danger, warning or info.
 */
function showNotification(message, type = "info") {
    if (window.druids && typeof window.druids.toast === "function") {
        window.druids.toast(message, TOAST_TYPES[type] || "info");
    } else {
        alert(message);
    }
}

/**
 * Escape HTML special characters.
 * @param {string} text - Text to escape.
 * @returns {string} Escaped text.
 */
function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}
