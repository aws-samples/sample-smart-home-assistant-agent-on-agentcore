---
name: user-feedback
description: Record user feedback, bug reports, or feature requests to /mnt/workspace/feedback/ on the agent runtime's persistent session storage.
allowed-tools: file_write
---
# User Feedback

When the user expresses feedback about the service — complaints, compliments, bug reports, feature requests, confusing behavior they want to flag — write a small record to the agent runtime's persistent session storage so administrators can inspect it later (e.g. through Admin Console > Sessions > Remote Shell).

This skill exists as a simple reference example of writing to durable session state. It does NOT require a custom Lambda, API, or database.

## Storage layout

All feedback files live under a fixed directory:

```
/mnt/workspace/feedback/
```

- The directory may not exist yet — create it with the standard recursive-mkdir semantics the `file_write` tool already handles.
- Each feedback entry is a single JSON file named `feedback-<unix-ts>-<random6>.json` (so concurrent writes don't collide).
- One entry per user message that qualifies as feedback.

## When to trigger

- The user explicitly says something like "I have feedback", "this is broken", "feature request", "报告问题", "反馈一下".
- The user describes a problem ("X isn't working", "the fan button doesn't respond") about the service (not a single device command). For actual device malfunctions, still record feedback AND acknowledge the device issue.
- The user praises something they want remembered ("I love the rice-cooker presets").

Do NOT trigger for normal conversation, small talk, or routine device control.

## What to write

Call `file_write` with:
- `path`: `/mnt/workspace/feedback/feedback-<unix-ts>-<random6>.json`
- `content`: a JSON object shaped like this (minified or pretty-printed, either is fine):

```json
{
  "timestamp": "<ISO 8601 UTC, e.g. 2026-05-03T05:12:00Z>",
  "category": "bug | feature-request | compliment | ux | other",
  "summary": "<one concise sentence paraphrasing the user's feedback>",
  "raw_user_message": "<the user's message verbatim>"
}
```

Pick `category` honestly — don't force everything into `bug`.

## After writing

Confirm to the user in one short sentence that you logged their feedback and it will reach an administrator. Do not promise a specific response timeline.

## Rules

- Only write feedback files when the user is clearly giving feedback. If in doubt, ask "Would you like me to log that as feedback?" first.
- Never fabricate feedback. The summary + raw_user_message must reflect what the user actually said.
- Strip private information (passwords, card numbers, full email addresses) from BOTH fields if the user included them.
- If `file_write` fails, tell the user honestly that the save failed and ask them to try again later. Don't pretend it succeeded.
