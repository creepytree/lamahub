/**
 * Lamahub - Chat/Prompt Tab
 * @description Handles chat interface and model interactions.
 * Bubbles are <druid-chat-message>; markdown rendering stays app-side.
 */

// Chat state
let chatMessages = [];
let isGenerating = false;

// Empty placeholder tool sent to tool-capable models: no arguments, no return.
const EMPTY_TOOL_TEMPLATE = {
    type: "function",
    function: {
        name: "placeholder",
        description: "Takes no arguments and returns nothing.",
        parameters: { type: "object", properties: {} },
    },
};

const SYSTEM_PROMPT_KEY = "systemPrompt";

/**
 * Get the saved system prompt, trimmed (empty string when unset).
 * @returns {string}
 */
function getSystemPrompt() {
    return (localStorage.getItem(SYSTEM_PROMPT_KEY) || "").trim();
}

/**
 * Persist (or clear) the system prompt and refresh the options indicator.
 * @param {string} text - System prompt text.
 */
function setSystemPrompt(text) {
    const value = (text || "").trim();
    if (value) {
        localStorage.setItem(SYSTEM_PROMPT_KEY, value);
    } else {
        localStorage.removeItem(SYSTEM_PROMPT_KEY);
    }
    updateSystemPromptIndicator();
}

/**
 * Reflect whether a system prompt is set as the button's active state.
 */
function updateSystemPromptIndicator() {
    const btn = document.getElementById("system-prompt-btn");
    if (btn) {
        btn.active = Boolean(getSystemPrompt());
    }
}

/**
 * Wire up the System Prompt dialog (open, prefill, save, clear).
 */
function initSystemPrompt() {
    const dialog = document.getElementById("system-prompt-modal");
    const openBtn = document.getElementById("system-prompt-btn");
    const input = document.getElementById("system-prompt-input");
    const saveBtn = document.getElementById("system-prompt-save");
    const clearBtn = document.getElementById("system-prompt-clear");

    if (openBtn && dialog) {
        openBtn.addEventListener("click", () => {
            if (input) input.value = getSystemPrompt();
            dialog.showModal();
        });
    }
    if (saveBtn) {
        saveBtn.addEventListener("click", () => {
            setSystemPrompt(input ? input.value : "");
            if (dialog) dialog.close();
        });
    }
    if (clearBtn) {
        clearBtn.addEventListener("click", () => {
            if (input) input.value = "";
            setSystemPrompt("");
        });
    }
    updateSystemPromptIndicator();
}

const THINKING_KEY = "thinkingEnabled";

/**
 * Whether thinking mode is currently toggled on.
 * @returns {boolean}
 */
function getThinkingEnabled() {
    return localStorage.getItem(THINKING_KEY) === "true";
}

/**
 * Persist the thinking toggle.
 * @param {boolean} enabled
 */
function setThinkingEnabled(enabled) {
    localStorage.setItem(THINKING_KEY, enabled ? "true" : "false");
}

/**
 * Wire up the Thinking toggle button (druid-button toggle).
 */
function initThinking() {
    const btn = document.getElementById("thinking-toggle-btn");
    if (btn) {
        btn.active = getThinkingEnabled();
        btn.addEventListener("toggle-change", (e) => setThinkingEnabled(e.detail.active));
    }
}

// images queued for next message: { data: base64 without prefix, mime, name }
let pendingImages = [];

// formats ollama vision models accept
const IMAGE_MIME_TYPES = ["image/png", "image/jpeg", "image/webp"];

/**
 * wire up attach (plus) button and hidden file input
 */
function initChatAttachments() {
    const attachBtn = document.getElementById("attach-image-btn");
    const fileInput = document.getElementById("chat-image-input");
    if (!attachBtn || !fileInput) return;

    attachBtn.addEventListener("click", () => fileInput.click());

    fileInput.addEventListener("change", async () => {
        for (const file of Array.from(fileInput.files)) {
            if (!IMAGE_MIME_TYPES.includes(file.type)) {
                showNotification(`"${file.name}" skipped: only PNG, JPEG and WebP images are supported`, "warning");
                continue;
            }
            const dataUrl = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject(reader.error);
                reader.readAsDataURL(file);
            });
            pendingImages.push({
                data: dataUrl.slice(dataUrl.indexOf(",") + 1),
                mime: file.type,
                name: file.name,
            });
        }
        // reset so same file can be picked again
        fileInput.value = "";
        renderAttachmentPreviews();
    });
}

/**
 * remove a pending image attachment
 * @param {number} index - index in pendingImages
 */
function removeAttachment(index) {
    pendingImages.splice(index, 1);
    renderAttachmentPreviews();
}

/**
 * render thumbnails of pending attachments above the chat input
 */
function renderAttachmentPreviews() {
    const container = document.getElementById("chat-attachments");
    if (!container) return;

    container.hidden = pendingImages.length === 0;
    container.innerHTML = pendingImages
        .map(
            (img, index) => `
                <div class="lh-attachment" title="${escapeHtml(img.name)}">
                    <img src="data:${img.mime};base64,${img.data}" alt="${escapeHtml(img.name)}">
                    <druid-icon-button circle small class="df-danger" icon="x"
                            onclick="removeAttachment(${index})"
                            label="Remove attachment"></druid-icon-button>
                </div>
            `,
        )
        .join("");
}

/**
 * Get the current prompt mode (always chat now).
 * @returns {string} 'chat'
 */
function getPromptMode() {
    return "chat";
}

/**
 * Get prompt options from the UI.
 * @param {string} [model] - The selected model name. When it is a pinned
 *   (fixed) model, num_ctx is deliberately omitted (see below).
 * @returns {Object} Options object for Ollama API
 */
function getPromptOptions(model) {
    const options = {};

    const temperature = document.getElementById("opt-temperature");
    if (temperature && temperature.value) {
        options.temperature = parseFloat(temperature.value);
    }

    const topK = document.getElementById("opt-top-k");
    if (topK && topK.value) {
        options.top_k = parseInt(topK.value);
    }

    const topP = document.getElementById("opt-top-p");
    if (topP && topP.value) {
        options.top_p = parseFloat(topP.value);
    }

    const repeatPenalty = document.getElementById("opt-repeat-penalty");
    if (repeatPenalty && repeatPenalty.value) {
        options.repeat_penalty = parseFloat(repeatPenalty.value);
    }

    const seed = document.getElementById("opt-seed");
    if (seed && seed.value) {
        options.seed = parseInt(seed.value);
    }

    const numPredict = document.getElementById("opt-num-predict");
    if (numPredict && numPredict.value) {
        options.num_predict = parseInt(numPredict.value);
    }

    // num_ctx is intentionally NOT sent for pinned models. The server keeps a
    // pinned model resident at its *baked* context via reconcile_fixed_ctx
    // (a keep_alive:-1 warm-load, re-asserted every ~30s — see
    // services/ollama.py). Ollama keys a runner by model+context: sending a
    // different num_ctx here would spin up a SECOND runner at that size and
    // evict the warm one, forcing a full reload of the (possibly huge) model
    // and silently overriding the pin. So for a pinned model we let the baked
    // default apply and omit num_ctx entirely; syncCtxLockForModel() also locks
    // the field in the UI so the value shown matches what actually applies.
    const numCtx = document.getElementById("opt-num-ctx");
    if (numCtx && numCtx.value && !(model && isFixedModel(model))) {
        options.num_ctx = parseInt(numCtx.value);
    }

    return options;
}

/**
 * Lock the num_ctx field to the pin when a fixed model is selected.
 * A pinned model is held resident at its baked context by the server; letting
 * the user set a conflicting num_ctx here would just reload the model at the
 * wrong size (see getPromptOptions). So we reflect the pinned value and disable
 * the field for fixed models, and re-enable it for everything else.
 */
function syncCtxLockForModel() {
    const select = document.getElementById("chat-model-select");
    const numCtx = document.getElementById("opt-num-ctx");
    if (!select || !numCtx) return;

    const label = document.getElementById("opt-num-ctx-label");
    const lock = label?.querySelector(".lh-ctx-lock");
    const tip = document.getElementById("opt-num-ctx-tip");
    const entry = getFixedEntry(select.value);

    if (entry && entry.num_ctx) {
        numCtx.value = entry.num_ctx;
        numCtx.disabled = true;
        // The "why" lives in a <druid-tooltip> (druids 1.0.3 — resolved the
        // GAPS.md tooltip gap); it works over the disabled input where a native
        // `title` is unreliable, and clearing `text` when unlocked hides it.
        tip?.setAttribute("text", `Locked to the pinned context (${entry.num_ctx}). Unpin the model to change it.`);
        label?.classList.add("lh-ctx-locked");
        if (lock) lock.hidden = false;
    } else {
        numCtx.disabled = false;
        tip?.removeAttribute("text");
        label?.classList.remove("lh-ctx-locked");
        if (lock) lock.hidden = true;
    }
}

/**
 * Load models into the chat model selector (druid-select watches its
 * light-DOM <option> children).
 */
async function loadChatModelSelect() {
    const select = document.getElementById("chat-model-select");
    if (!select) return;

    const data = await fetchAPI("/models");
    if (data.error || !data.models) return;

    const currentValue = select.value;
    select.innerHTML = data.models
        .map((model) => `<option value="${escapeHtml(model.name)}">${escapeHtml(model.name)}</option>`)
        .join("");

    // Restore previous selection if still available
    if (currentValue && data.models.some((m) => m.name === currentValue)) {
        select.value = currentValue;
    }

    // Keep the num_ctx field in sync with the (possibly restored) selection and
    // wire a one-time change listener so switching models locks/unlocks it.
    if (!select.dataset.ctxLockWired) {
        select.addEventListener("change", syncCtxLockForModel);
        select.dataset.ctxLockWired = "1";
    }
    syncCtxLockForModel();
}

/**
 * Render chat messages in the chat container.
 */
function renderChatMessages() {
    const container = document.getElementById("chat-messages");
    if (!container) return;

    // Preserve which thinking accordions the user has expanded. The full
    // innerHTML rebuild below recreates the <details> elements on every
    // streamed chunk and would otherwise collapse them again immediately.
    const openThinking = new Set(
        Array.from(container.querySelectorAll("details[data-thinking-index][open]")).map(
            (el) => el.dataset.thinkingIndex,
        ),
    );

    if (chatMessages.length === 0) {
        container.innerHTML = `
            <div class="df-muted lh-chat-empty">
                Select a model and start chatting
            </div>
        `;
        return;
    }

    container.innerHTML = chatMessages
        .map((msg, index) => {
            const isUser = msg.role === "user";
            const messageContent = md ? md.render(msg.content) : escapeHtml(msg.content);
            let content = "";
            if (msg.thinking) {
                const thinkingContent = md ? md.render(msg.thinking) : escapeHtml(msg.thinking);
                content += `<details data-thinking-index="${index}" class="lh-thinking"><summary>Thinking...</summary><div class="lh-thinking-body" data-thinking-body="${index}">${thinkingContent}</div></details>`;
            }
            if (msg.images && msg.images.length) {
                const thumbs = msg.images
                    .map((img) => `<img class="lh-message-image" src="data:${img.mime};base64,${img.data}" alt="${escapeHtml(img.name || "attached image")}">`)
                    .join("");
                content += `<div>${thumbs}</div>`;
            }
            content += `<div class="markdown-content" data-message-body="${index}">${messageContent}</div>`;
            const actionButtons = isUser
                ? `<druid-icon-button slot="actions" circle small icon="rotate-cw" onclick="resendMessage(${index})" label="Resend message"></druid-icon-button>
                   <druid-icon-button slot="actions" circle small icon="x" onclick="deleteMessage(${index})" label="Delete message"></druid-icon-button>`
                : "";
            return `
                <druid-chat-message sender="${isUser ? "user" : "assistant"}">
                    ${content}
                    ${actionButtons}
                </druid-chat-message>
            `;
        })
        .join("");

    // Re-expand any thinking accordions that were open before the rebuild.
    openThinking.forEach((idx) => {
        const el = container.querySelector(`details[data-thinking-index="${idx}"]`);
        if (el) el.open = true;
    });

    scrollChatToBottom(container);
}

/**
 * Scroll the chat container to the newest message.
 *
 * <druid-chat-message> is a custom element whose shadow DOM upgrades and lays
 * out asynchronously, so scrollHeight read synchronously after an innerHTML
 * rebuild is still stale (near zero) — a plain `scrollTop = scrollHeight` then
 * lands at the TOP, which looked like the chat "jumping to the start" on send.
 * Deferring past layout (double rAF covers the element upgrade) scrolls to the
 * real bottom so the view follows the conversation.
 * @param {HTMLElement} container - The chat messages container.
 */
function scrollChatToBottom(container) {
    requestAnimationFrame(() => {
        requestAnimationFrame(() => {
            container.scrollTop = container.scrollHeight;
        });
    });
}

/**
 * Update only the streaming message's thinking and content in place.
 *
 * Rebuilding the whole container per token (renderChatMessages) destroys and
 * recreates the <details> element on every chunk, so a click meant to expand
 * the thinking accordion never lands on a live element. Updating just the text
 * nodes keeps the accordion persistent and clickable while the model streams.
 * @param {number} index - Index of the message being streamed.
 */
function updateStreamingMessage(index) {
    const container = document.getElementById("chat-messages");
    if (!container) return;
    const msg = chatMessages[index];
    if (!msg) return;

    // First thinking token: the accordion isn't in the DOM yet, so do one full
    // render to insert it. Every subsequent update is in place.
    if (msg.thinking && !container.querySelector(`details[data-thinking-index="${index}"]`)) {
        renderChatMessages();
        return;
    }

    // Only keep pinned to the bottom if the user hasn't scrolled up to read.
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 50;

    if (msg.thinking) {
        const body = container.querySelector(`[data-thinking-body="${index}"]`);
        if (body) body.innerHTML = md ? md.render(msg.thinking) : escapeHtml(msg.thinking);
    }
    const msgBody = container.querySelector(`[data-message-body="${index}"]`);
    if (msgBody) msgBody.innerHTML = md ? md.render(msg.content) : escapeHtml(msg.content);

    if (nearBottom) container.scrollTop = container.scrollHeight;
}

/**
 * Send a message in chat mode.
 */
async function sendChatMessage() {
    const chatInput = document.getElementById("chat-input");
    const message = chatInput?.value.trim();
    if (!message) return;

    chatInput.value = "";
    const images = pendingImages;
    pendingImages = [];
    renderAttachmentPreviews();
    await sendMessage(message, images);
}

/**
 * Resend a previous user message, truncating newer chat context first.
 * @param {number} messageIndex - Index of the user message to resend.
 */
async function resendMessage(messageIndex) {
    if (isGenerating) {
        return;
    }

    const selectedMessage = chatMessages[messageIndex];
    if (!selectedMessage || selectedMessage.role !== "user") {
        return;
    }

    chatMessages = chatMessages.slice(0, messageIndex);
    renderChatMessages();
    await sendMessage(selectedMessage.content, selectedMessage.images || []);
}

/**
 * Delete a user message together with its assistant response.
 * @param {number} messageIndex - Index of the user message to delete.
 */
function deleteMessage(messageIndex) {
    if (isGenerating) {
        return;
    }

    const selectedMessage = chatMessages[messageIndex];
    if (!selectedMessage || selectedMessage.role !== "user") {
        return;
    }

    // Remove the user message and the assistant reply that follows it, if any.
    const removeCount = chatMessages[messageIndex + 1]?.role === "assistant" ? 2 : 1;
    chatMessages.splice(messageIndex, removeCount);
    renderChatMessages();
}

/**
 * Send chat message content with current context.
 * thinking + images are capability-gated: dropped with a notification
 * if the model lacks "thinking" / "vision"
 * @param {string} message - User message text.
 * @param {Array<Object>} [images] - attached images ({data, mime, name})
 */
async function sendMessage(message, images = []) {
    const modelSelect = document.getElementById("chat-model-select");
    const sendBtn = document.getElementById("send-chat-btn");
    const model = modelSelect?.value;
    const options = getPromptOptions(model);

    if (!model) {
        showNotification("Please select a model first", "warning");
        return;
    }

    if (!message || isGenerating) {
        return;
    }

    const capabilities = await fetchModelCapabilities(model);
    const supportsTools = capabilities.includes("tools");

    let think = getThinkingEnabled();
    if (think && !capabilities.includes("thinking")) {
        think = false;
        showNotification(`${model} does not support thinking — request sent without it`, "warning");
    }

    if (images.length && !capabilities.includes("vision")) {
        images = [];
        showNotification(`${model} does not support vision — message sent without images`, "warning");
    }

    // Add user message
    const userMsg = { role: "user", content: message };
    if (images.length) {
        userMsg.images = images;
    }
    chatMessages.push(userMsg);
    renderChatMessages();

    // Prepare for assistant response
    isGenerating = true;
    sendBtn.disabled = true;
    sendBtn.textContent = "...";

    // Add placeholder for assistant message
    const assistantMsg = { role: "assistant", content: "", thinking: "" };
    chatMessages.push(assistantMsg);
    renderChatMessages();

    try {
        const promptMode = getPromptMode();
        const contextMessages = chatMessages.slice(0, -1).map((m) => {
            const out = { role: m.role, content: m.content };
            // ollama wants raw base64 strings on message.images
            if (m.images && m.images.length) {
                out.images = m.images.map((img) => img.data);
            }
            return out;
        });

        // Prepend the system prompt (if set) so it leads the chat query.
        const systemPrompt = getSystemPrompt();
        if (systemPrompt) {
            contextMessages.unshift({ role: "system", content: systemPrompt });
        }

        const response =
            promptMode === "generate"
                ? await generate({
                      model: model,
                      prompt: message,
                      options: options,
                  })
                : await chat({
                      model: model,
                      messages: contextMessages,
                      options: options,
                      think: think,
                      // Tool-capable models get an empty placeholder tool that
                      // takes no args and returns nothing, so the tools path is
                      // exercised without changing the conversation.
                      tools: supportsTools ? [EMPTY_TOOL_TEMPLATE] : null,
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
                            chatMessages[chatMessages.length - 1].content = `Error: ${data.error}`;
                            renderChatMessages();
                            break;
                        }

                        // Chat mode response
                        if (data.message && data.message.content) {
                            chatMessages[chatMessages.length - 1].content += data.message.content;
                            updateStreamingMessage(chatMessages.length - 1);
                        }

                        // Handle thinking content if present
                        if (data.message && data.message.thinking) {
                            chatMessages[chatMessages.length - 1].thinking += data.message.thinking;
                            updateStreamingMessage(chatMessages.length - 1);
                        }
                    } catch (e) {}
                }
            }
        }
    } catch (error) {
        chatMessages[chatMessages.length - 1].content = `Error: ${error.message}`;
        renderChatMessages();
    } finally {
        isGenerating = false;
        sendBtn.disabled = false;
        sendBtn.textContent = "Send";
    }
}

/**
 * Clear the chat history.
 */
function clearChat() {
    chatMessages = [];
    renderChatMessages();
}
