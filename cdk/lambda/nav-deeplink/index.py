"""Lambda function to resolve a page-navigation request into a DeepLink.
Called as an AgentCore Gateway Lambda target.

This is a keyword-to-URL lookup with no reasoning in it, so it is deliberately
NOT an agent: a Runtime would pay a network round-trip and an LLM call for a
dictionary read.

It is also deliberately not an in-process `@tool` on the main agent. In-process
tools never touch the Gateway, so they never appear in Cedar or on the Admin
Console's Tool Policy page and cannot be granted or revoked per user. Navigation
sends the user into a specific feature page, which is a reasonable authorisation
boundary (an operator may not want every user routed to a management page), and
a control-plane story with one tool sitting outside the control plane is a hole.
The cost is one extra Lambda, one extra Gateway hop and one Cedar policy.

Unlike the device Lambdas this one takes NO user identity: the mapping is the
same for every user and the result is a static URL, so there is nothing to scope.
That is why `navigate_to_page` is absent from the agent's `scoped_suffixes` list
(see agent/agent.py) — absence there is a decision, not an oversight.

The page list lives in code rather than a table: pages change far less often than
the device fleet, and a table would add a read and a failure mode to a lookup.
"""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Neutral scheme — the reference solution is not branded.
SCHEME = "superapp"

# Each page carries the aliases a user might actually say, in both languages the
# rest of the product speaks. Matching is substring-based over these, so short
# aliases would over-match: keep them specific enough to be unambiguous.
PAGES = [
    {
        "page": "home",
        "path": "home",
        "label": {"en": "Home", "zh": "首页"},
        "description": "The app home screen with the device overview.",
        "aliases": ["home", "homepage", "home page", "main page", "dashboard",
                    "首页", "主页", "主界面"],
    },
    {
        "page": "device_list",
        "path": "devices",
        "label": {"en": "Devices", "zh": "设备列表"},
        "description": "The list of all the user's devices.",
        "aliases": ["device list", "devices", "my devices", "device page",
                    "设备列表", "设备页", "我的设备", "设备管理"],
    },
    {
        "page": "group_control",
        "path": "group_control",
        "label": {"en": "Group Control", "zh": "群控"},
        "description": "Control several devices together.",
        "aliases": ["group control", "group", "groups", "multi device control",
                    "control several", "群控", "群控页面", "分组控制", "批量控制"],
    },
    {
        "page": "light_effects",
        "path": "light_effects",
        "label": {"en": "Light Effects", "zh": "灯效"},
        "description": "Browse and apply lighting effects.",
        "aliases": ["light effect", "light effects", "lighting effect", "effects",
                    "scene light", "灯效", "灯效页面", "灯光效果", "氛围灯"],
    },
    {
        "page": "music_sync",
        "path": "music_sync",
        "label": {"en": "Music Sync", "zh": "音乐盛宴"},
        "description": "Make the lights react to music.",
        "aliases": ["music sync", "music mode", "lights with music", "music party",
                    "音乐盛宴", "音乐同步", "灯随音乐"],
    },
    {
        "page": "video_sync",
        "path": "video_sync",
        "label": {"en": "Video Sync", "zh": "观影盛宴"},
        "description": "Sync the lights to what is on the TV.",
        "aliases": ["video sync", "movie mode", "tv sync", "cinema mode",
                    "观影盛宴", "影音同步", "电视同步", "观影模式"],
    },
    {
        "page": "automation",
        "path": "automation",
        "label": {"en": "Automation", "zh": "自动化"},
        "description": "Automations that run on a schedule or a condition.",
        "aliases": ["automation", "automations", "automatic task", "schedule",
                    "scheduled task", "rule", "rules", "自动化", "自动化任务",
                    "定时任务", "任务页面"],
    },
    {
        "page": "one_tap",
        "path": "one_tap",
        "label": {"en": "One-Tap Commands", "zh": "一键指令"},
        "description": "Saved commands the user runs with one tap.",
        "aliases": ["one tap", "one-tap", "shortcut", "shortcuts", "quick command",
                    "一键指令", "快捷指令", "一键卡片"],
    },
    {
        "page": "sensor_history",
        "path": "sensor_history",
        "label": {"en": "Sensor History", "zh": "传感器历史"},
        "description": "Charts of the sensor readings over time.",
        "aliases": ["sensor history", "history", "sensor chart", "trend",
                    "readings", "传感器历史", "历史数据", "数据趋势", "环境历史"],
    },
    {
        "page": "settings",
        "path": "settings",
        "label": {"en": "Settings", "zh": "设置"},
        "description": "App and account settings.",
        "aliases": ["settings", "setting", "preferences", "account",
                    "设置", "设置页面", "偏好设置", "账号设置"],
    },
]


def _catalog():
    """The page list as the agent should relay it to a user."""
    return [
        {"page": p["page"], "label": p["label"], "description": p["description"]}
        for p in PAGES
    ]


def _deeplink(page, params=None):
    link = f"{SCHEME}://{page['path']}"
    if params:
        # Sorted so the same request always produces the same link — makes the
        # value stable in logs and tests.
        pairs = "&".join(f"{k}={params[k]}" for k in sorted(params) if params[k] != "")
        if pairs:
            link = f"{link}?{pairs}"
    return link


def _match(query):
    """Resolve free text to a page. Exact page name first, then aliases.

    Longest alias wins: "music sync" must not lose to a shorter alias that
    happens to also be contained in the query.
    """
    q = (query or "").strip().lower()
    if not q:
        return None

    for p in PAGES:
        if q == p["page"] or q == p["path"]:
            return p

    best, best_len = None, 0
    for p in PAGES:
        for alias in p["aliases"]:
            a = alias.lower()
            if a in q and len(a) > best_len:
                best, best_len = p, len(a)
    return best


def handler(event, context):
    logger.info("Received event: %s", json.dumps(event, default=str))

    try:
        query = event.get("page") or event.get("query") or event.get("target") or ""
        params = event.get("params")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = None
        if not isinstance(params, dict):
            params = None

        page = _match(query)
        if not page:
            # Returning the catalog beats a bare failure: the agent can tell the
            # user which pages exist instead of just saying no.
            return {
                "matched": False,
                "query": query,
                "reason": (
                    f"No page matches '{query}'." if query
                    else "No page was named in the request."
                ),
                "availablePages": _catalog(),
            }

        return {
            "matched": True,
            "page": page["page"],
            "label": page["label"],
            "deepLink": _deeplink(page, params),
        }

    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        return {"error": str(e)}
