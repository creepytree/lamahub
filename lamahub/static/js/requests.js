/**
 * Lamahub - Request Helpers
 * @description Reader for the server-sent event streams (chat, pull, update).
 */

/**
 * POST to a streaming endpoint and hand every `data:` event to a callback.
 * Lines are buffered across network chunks, so an event split between two
 * reads is still parsed whole.
 * @param {string} path - API path below /api (e.g. "/chat").
 * @param {Object} body - JSON request body.
 * @param {function(Object): (boolean|void)} onEvent - Called per parsed event;
 *   return false to stop reading.
 * @returns {Promise<boolean>} true when the stream ended, false when stopped.
 */
async function streamSSE(path, body, onEvent) {
    const response = await fetch(withBasePath(`/api${path}`), {
        method: "POST",
        headers: requestHeaders(),
        body: JSON.stringify(body),
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();
        if (done) return true;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            let data;
            try {
                data = JSON.parse(line.slice(6));
            } catch (e) {
                continue;
            }
            if (onEvent(data) === false) {
                reader.cancel();
                return false;
            }
        }
    }
}
