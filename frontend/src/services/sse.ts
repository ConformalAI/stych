/**
 * Shared SSE (Server-Sent Events) parsing utilities.
 *
 * All stream consumers (useChat/agentRunStream and any future raw-fetch
 * consumers) route through parseSseChunk. Keeping the buffer-splitting logic
 * in one place prevents implementations from drifting apart when bugs are
 * fixed.
 */

/**
 * Split a raw text buffer on newlines, parse every `data: <json>` line into
 * a typed event, and return the unparsed tail (the partial line that has not
 * yet received a terminating newline).
 */
export function parseSseChunk<T = unknown>(buffer: string): { events: T[]; remaining: string } {
    const lines = buffer.split('\n');
    const remaining = lines.pop() ?? '';
    const events: T[] = [];

    for (const line of lines) {
        if (line.startsWith('data: ')) {
            try {
                events.push(JSON.parse(line.slice(6)) as T);
            } catch {
                // Ignore malformed / non-JSON lines (comments, keep-alives, etc.)
            }
        }
    }

    return { events, remaining };
}
