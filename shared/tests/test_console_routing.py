"""The admin console's hash routing must stay wired to the tab list.

The console has no router: a tab is `useState<ActiveTab>` and the sidebar calls
`preventDefault()`, so nothing keeps the URL and the rendered tab in step unless
App.tsx does it explicitly. It didn't — `activeTab` was initialised to 'overview'
unconditionally, so every `#/<tab>` deep link rendered the overview while the
address bar said otherwise. Because the overview is a real, populated page, this
looked like a working link rather than a broken one; it was only visible by
reading the table headers.

`ActiveTab` is a TypeScript union, so nothing about that failure is checkable at
build time. These are source-text assertions on the pieces that have to be
present for routing to work at all — a compile-time union can't stand in for
them, and the console has no JS test runner.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "admin-console", "src")
APP = os.path.join(SRC, "App.tsx")
CONSOLE = os.path.join(SRC, "components", "AdminConsole.tsx")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _active_tabs():
    """The ACTIVE_TABS array literal, as a list of tab names."""
    body = _read(CONSOLE)
    m = re.search(r"export const ACTIVE_TABS = \[(.*?)\] as const;", body, re.S)
    assert m, "ACTIVE_TABS array not found — routing cannot be validated"
    return re.findall(r"'([A-Za-z]+)'", m.group(1))


def test_the_tab_list_is_a_runtime_value_not_only_a_type():
    """A bare `type ActiveTab = 'a' | 'b'` is erased at build time, so a hash
    cannot be checked against it. The union must be DERIVED from the array so the
    two can never disagree."""
    body = _read(CONSOLE)
    assert "export const ACTIVE_TABS" in body
    assert "export type ActiveTab = (typeof ACTIVE_TABS)[number];" in body, (
        "ActiveTab must be derived from ACTIVE_TABS, not written as a second list")


def test_every_tab_is_reachable_by_deep_link():
    tabs = _active_tabs()
    assert "overview" in tabs and "agents" in tabs
    assert len(tabs) == len(set(tabs)), f"duplicate tab: {tabs}"


def test_the_initial_tab_comes_from_the_url():
    """The specific regression: without this, `#/agents` renders the overview."""
    app = _read(APP)
    m = re.search(r"useState<ActiveTab>\((.*?)\);", app, re.S)
    assert m, "activeTab useState not found"
    init = m.group(1)
    assert "tabFromHash" in init, (
        f"activeTab is initialised without reading the hash: {init.strip()!r} — "
        "every deep link will render the default tab")


def test_the_url_is_updated_when_the_tab_changes():
    """The sidebar calls preventDefault(), so the hash only moves if App.tsx
    writes it. Without this, a tab is unshareable and the in-page
    `window.location.hash = '#/optimization'` link silently does nothing."""
    app = _read(APP)
    assert "history.replaceState" in app or "location.hash =" in app, (
        "nothing writes the hash when activeTab changes")
    assert "`#/${activeTab}`" in app


def test_the_back_button_is_handled():
    app = _read(APP)
    assert "'hashchange'" in app, (
        "no hashchange listener: the browser Back button would leave the URL and "
        "the rendered tab disagreeing")
    assert "removeEventListener('hashchange'" in app, "listener is never removed"


def test_every_side_nav_href_names_a_real_tab():
    """A typo'd href is indistinguishable from a working one — it lands on the
    overview."""
    tabs = set(_active_tabs())
    hrefs = set(re.findall(r"href: '#/([A-Za-z]+)'", _read(APP)))
    assert hrefs, "no side-nav hrefs found — scanner broken?"
    assert hrefs <= tabs, f"side nav links to unknown tab(s): {sorted(hrefs - tabs)}"


def test_in_page_hash_navigation_targets_a_real_tab():
    """AdminConsole navigates by assigning location.hash directly in a few
    places; those targets must also be routable."""
    tabs = set(_active_tabs())
    targets = set(re.findall(r"location\.hash = '#/([A-Za-z]+)'", _read(CONSOLE)))
    assert targets <= tabs, f"unknown hash target(s): {sorted(targets - tabs)}"
