/**
 * Lamahub - Main Application
 * @description Initializes the application and sets up event listeners.
 * Tabs, toasts, the log view and accent theming are handled by the druids
 * framework; this only wires app behavior onto the druid elements.
 */

/**
 * Initialize page functionality on DOM load.
 */
document.addEventListener("DOMContentLoaded", function () {
    console.log("DOM loaded, initializing application");

    // Ensure markdown-it is initialized
    if (!md) {
        initializeMarkdown();
    }

    // Load all dashboard data. Resolve the active endpoint first so every
    // subsequent request carries the correct X-Ollama-Url header.
    console.log("Loading dashboard data");
    initializeModelNameCopyHandlers();
    loadEndpoints().then(refreshAllData);

    // Set up refresh intervals
    setInterval(() => loadRunningModels(), 4000);
    setInterval(() => {
        loadTotalModels();
        loadTotalStorage();
    }, 30000); // 30 seconds - reduced from 10s to minimize log noise

    // Set up model pull functionality
    const pullBtn = document.getElementById("pull-model-btn");
    if (pullBtn) {
        pullBtn.addEventListener("click", pullModel);
    }

    const modelSearchInput = document.getElementById("model-search-input");
    if (modelSearchInput) {
        modelSearchInput.addEventListener("search", applyModelSearch);
    }

    const modelInput = document.getElementById("model-name-input");
    if (modelInput) {
        modelInput.addEventListener("keypress", (e) => {
            if (e.key === "Enter") {
                pullModel();
            }
        });
    }

    // Set up reset sort button
    const resetSortBtn = document.getElementById("reset-sort-btn");
    if (resetSortBtn) {
        resetSortBtn.addEventListener("click", resetTableSort);
    }

    // Options panel visibility follows the toggle button's active state
    const optionsBtn = document.getElementById("prompt-options-btn");
    const optionsPanel = document.getElementById("prompt-options");
    if (optionsBtn && optionsPanel) {
        optionsBtn.addEventListener("toggle-change", (e) => {
            optionsPanel.hidden = !e.detail.active;
        });
    }

    // Set up chat functionality
    const sendChatBtn = document.getElementById("send-chat-btn");
    if (sendChatBtn) {
        sendChatBtn.addEventListener("click", sendChatMessage);
    }

    const chatInput = document.getElementById("chat-input");
    if (chatInput) {
        chatInput.addEventListener("keypress", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                sendChatMessage();
            }
        });
    }

    const clearChatBtn = document.getElementById("clear-chat-btn");
    if (clearChatBtn) {
        clearChatBtn.addEventListener("click", clearChat);
    }

    initSystemPrompt();
    initThinking();
    initChatAttachments();
});

// Socket.io event handlers
if (typeof socket !== "undefined") {
    socket.on("connect", function () {
        console.log("Connected to server");
    });

    socket.on("model_update", function (data) {
        console.log("Model update received:", data);
        loadFixedModels().then(loadModelsList);
        loadRunningModels();
        loadTotalModels();
        loadTotalStorage();
        loadChatModelSelect();
    });
}
