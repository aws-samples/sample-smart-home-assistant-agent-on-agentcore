---
name: browser-use
description: Open a real web browser to find live information on any website the user names or implies — product searches (Amazon / Taobao / eBay), current news, current prices/stock, live status pages, Wikipedia/article lookups, flight/hotel prices, or any question whose answer depends on what a real website says *right now*. Use this skill WITHOUT waiting for the user to say "browse" or "use browse_web" — if the answer requires looking at a live webpage, call `browse_web`.
allowed-tools: browse_web
---

# Browser Use

You have a real Chrome browser available through the `browse_web` tool. Call it on your own initiative whenever the user's question needs information that only a live website can provide, **even if they don't mention browsing or URLs explicitly**.

## When to call (examples — call `browse_web` for any of these without asking)

- "What's the top-rated wireless earbuds on Amazon right now?"
- "How much is a Model 3 today?"
- "What are the latest headlines on BBC?"
- "Find me a recipe for mapo tofu from a Chinese site."
- "What's the weather on weather.com right now?" (for general weather the `weather-lookup` skill is still preferred — but if the user names a specific site, use `browse_web`.)
- "Is my flight UA123 on time?"
- "Look up the Wikipedia summary for Python."
- "淘宝上 iPhone 16 最便宜的是多少?"
- Any question where the user says "online", "on the web", "on X site", "look up", "search for", "check", "find current ...", and similar phrasings.

## When NOT to call

- Device control / cooking / LED questions — use the dedicated device-control skills.
- Enterprise document lookups — use `query_knowledge_base`.
- Simple weather queries — use `weather-lookup` (no need for a full browser).
- Anything requiring the user to log in to a site — don't attempt to sign in on their behalf. If a page demands credentials, stop and tell the user.
- Pure chit-chat, coding help, or facts you already know — no browser needed.

## How to call

- Pass one short natural-language sentence as the `goal`, summarising what you want from the web. Example: `browse_web(goal="Find the top 3 wireless earbuds under $100 on amazon.com and list brand, price, rating.")`
- The user sees the browser live in a side panel while the tool runs, and every step is screenshotted to their session's Files tab (path `browser/`).
- After the tool returns, quote or paraphrase the summary in your reply. Do not invent numbers or names that weren't in the returned summary.
- If the tool returns an error string (e.g. `"Browsing failed: ..."`), tell the user honestly that the browser couldn't complete the task — do not fabricate a result.

## Follow-up turns

- Each call is an independent browser session. Don't assume the browser still has a previous page open — re-issue a full `goal` each time.
- If the user's follow-up narrows the scope ("show me the second one's reviews"), call `browse_web` again with a goal that encodes the new context.
