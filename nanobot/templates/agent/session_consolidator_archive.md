Summarize this same-session conversation so a later turn can continue faithfully after the raw messages are hidden.

Preserve concrete operational context:
- the user's requests, corrections, constraints, and unresolved follow-ups;
- the assistant's conclusions and final answer;
- every tool or MCP method used, with exact tool names and salient query, URL, symbol, path, or other non-secret arguments;
- which tool calls succeeded or failed and what evidence their results supported;
- generated artifacts and paths that a later turn may reference.

This is a conversation-continuity summary, not long-term memory extraction. Do not apply SNIP filtering, do not emit `[skip]`, and do not discard one-off research merely because it may become stale. Never describe the next request as a new or first conversation when this summary exists.

Write a compact factual summary in the conversation's primary language. Do not add facts that are absent from the messages.
