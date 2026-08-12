"""App CSS must not hardcode theme colours, and the aliases must pass contrast.

Three frontends (admin console, chatbot, device simulator) ship custom CSS beside
Cloudscape components. Cloudscape hashes its own component CSS variables at build
time, so `var(--color-text-body-default)` cannot be referenced from app CSS — the
name never resolves and the **fallback silently wins**. Each app therefore samples
the resolved light/dark values in `src/theme/applyTheme.ts` and publishes them as
stable aliases (`--admin-*`, `--chat-*`, `--sim-*`).

Two ways that has gone wrong, both of which reached production:

  - **A raw `--color-*` reference.** The simulator's clock and media panels stayed
    WHITE in dark mode while every device card beside them flipped, because the
    fallback is a legitimate-looking colour rather than an error. An unresolvable
    `var()` is not a CSS error; it is a fallback.
  - **A hardcoded colour that only suits one mode.** The admin console's App.css was
    written dark-only. `.perm-tool-name { color: #e0e0e0 }` measured **1.32:1**
    against white — so in light mode the Tool Policy permission list looked greyed
    out and was reported as "you can no longer configure per-user tool permissions".
    Nothing was broken: all 17 checkboxes were enabled and interactive, and the API
    returned all 17 tools. The bug was purely that you could not read them.

That second one is the reason this file also checks CONTRAST. A palette can be
theme-aware and still illegible, and "looks fine on my monitor" is not a measurement
— the first two values chosen for the dim tier came out at 3.34:1 and 4.49:1, both
below the floor, and both looked acceptable by eye.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APPS = {
    "admin-console": "--admin-",
    "chatbot": "--chat-",
    "device-simulator": "--sim-",
}

# WCAG 2.1 AA for normal-size body text.
AA_NORMAL = 4.5

# Colours that are deliberately fixed regardless of theme, with the reason. Checked
# as a literal allowlist so a NEW hardcoded colour cannot hide among them.
ALLOWED_FIXED = {
    # ANSI terminal palette — renders on a fixed dark shell pane, and these are the
    # canonical 16 colours, not theme decisions.
    "#000", "#c00", "#0a0", "#c60", "#06c", "#c0c", "#0cc", "#fff",
    "#555", "#f66", "#6f6", "#ff6", "#6af", "#f6f", "#6ff",
    # Text on a filled/branded surface, where the surface is theme-independent.
    "#ffffff",
    # Brand accent and status colours, used with an icon or label, never alone.
    "#4a9eff", "#f87171", "#34d399", "#ff6b6b", "#8888aa", "#0a0a0f",
    # Foreground on the remote shell's hardcoded dark panes (#1a1a1a / #0a0a0a).
    # A terminal stays a terminal in both themes -- the ANSI palette above assumes
    # it -- so text rendered INSIDE those panes must not follow --admin-text.
    # Getting this wrong is silent in the opposite direction: a first pass here
    # aliased them and produced dark-on-dark. Placement was then checked in the
    # components (ShellModal.tsx vs AnsiOutput.tsx) rather than inferred from CSS
    # order, which is what separated the modal-body labels from the pane contents.
    "#ddd", "#999",
}


def _css_files(app):
    root = os.path.join(REPO, app, "src")
    out = []
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n.endswith(".css"):
                out.append(os.path.join(dirpath, n))
    return out


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _luminance(hex_colour):
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    parts = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    parts = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
             for c in parts]
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast(fg, bg):
    a, b = _luminance(fg), _luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


# ---------------------------------------------------------------------------
# No raw Cloudscape variables
# ---------------------------------------------------------------------------

def test_no_app_css_references_cloudscape_variables_directly():
    """The name is hashed at build time, so the fallback always wins."""
    offenders = []
    for app in APPS:
        for path in _css_files(app):
            body = _strip_comments(_read(path))
            for m in re.finditer(r"var\(\s*(--color-[a-z-]+)", body):
                line = body[:m.start()].count("\n") + 1
                offenders.append(f"{os.path.relpath(path, REPO)}:{line} {m.group(1)}")
    assert not offenders, (
        "app CSS references Cloudscape's own hashed variables; these never resolve "
        "and the var() fallback wins in BOTH themes:\n  " + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# Every app publishes aliases, and its CSS uses them
# ---------------------------------------------------------------------------

def test_every_app_publishes_theme_aliases_for_both_modes():
    for app, prefix in APPS.items():
        theme = os.path.join(REPO, app, "src", "theme", "applyTheme.ts")
        assert os.path.isfile(theme), f"{app} has no theme/applyTheme.ts"
        src = _read(theme)
        assert "setProperty" in src, (
            f"{app}/theme/applyTheme.ts does not publish any custom property, so "
            f"its CSS cannot follow the theme")
        for mode in ("light:", "dark:"):
            assert mode in src, f"{app} theme has no {mode} palette"
        assert prefix in src, f"{app} theme publishes no {prefix}* aliases"


def test_admin_console_css_uses_the_aliases():
    """Regression: this file was dark-only and used none of them."""
    css = "".join(_read(p) for p in _css_files("admin-console"))
    assert css.count("var(--admin-") >= 20, (
        f"only {css.count('var(--admin-')} alias uses in the admin console's CSS; "
        f"it was written dark-only and the light mode was unreadable")


# ---------------------------------------------------------------------------
# Contrast of the published palettes
# ---------------------------------------------------------------------------

def _palette(app, mode):
    """The alias -> hex map for one mode, parsed out of applyTheme.ts."""
    src = _read(os.path.join(REPO, app, "src", "theme", "applyTheme.ts"))
    start = src.index(f"{mode}: {{")
    depth, i = 0, start + len(f"{mode}: ")
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = src[start:i]
    return {m.group(1): m.group(2)
            for m in re.finditer(r"'(--[a-z-]+)':\s*'(#[0-9a-fA-F]{3,8})'", block)}


# (app, mode, the surface those text tokens sit on)
SURFACES = [
    ("admin-console", "light", "#ffffff"),
    ("admin-console", "dark", "#161d26"),
    ("chatbot", "light", "#ffffff"),
    ("chatbot", "dark", "#161d26"),
    ("device-simulator", "light", "#ffffff"),
    ("device-simulator", "dark", "#161d26"),
]


def test_text_tokens_meet_wcag_aa_against_their_surface():
    """The measurement the eye cannot do.

    Only `*text*` tokens are checked, and only against the container surface they
    are used on. Tokens named for a filled control (`*-inverse`, `*-outgoing-text`)
    sit on that control's own background and are excluded — checking them against
    the page surface would report a failure that does not exist.
    """
    failures = []
    for app, mode, surface in SURFACES:
        for name, colour in _palette(app, mode).items():
            if "text" not in name:
                continue
            if "inverse" in name or "outgoing" in name:
                continue
            ratio = contrast(colour, surface)
            if ratio < AA_NORMAL:
                failures.append(
                    f"{app} {mode}: {name}={colour} on {surface} is "
                    f"{ratio:.2f}:1 (need {AA_NORMAL}:1)")
    assert not failures, "text tokens below WCAG AA:\n  " + "\n  ".join(failures)


def test_the_dim_tier_is_actually_dimmer_but_still_legible():
    """It exists to be quieter than secondary. If it is not, drop it rather than
    keep a token that pretends to be a third tier."""
    light = _palette("admin-console", "light")
    dim, secondary = light["--admin-text-dim"], light["--admin-text-secondary"]
    on_white = contrast(dim, "#ffffff")
    assert on_white >= AA_NORMAL, f"{dim} is {on_white:.2f}:1 on white"
    assert contrast(dim, "#ffffff") <= contrast(secondary, "#ffffff"), (
        "the dim tier is not dimmer than the secondary tier")


def test_no_new_hardcoded_text_colour_creeps_into_the_admin_console():
    """Anything not on the reviewed allowlist must go through an alias.

    Written as an allowlist rather than a count so that ADDING a hardcoded colour
    fails, while the reviewed ANSI/brand ones keep passing.
    """
    offenders = []
    for path in _css_files("admin-console"):
        body = _strip_comments(_read(path))
        # Bare `color:` declarations only — not background-color / border-color, and
        # not the fallback inside an alias reference.
        for m in re.finditer(r"(?<![-\w])color:\s*(#[0-9a-fA-F]{3,6})\s*;", body):
            if m.group(1).lower() not in ALLOWED_FIXED:
                line = body[:m.start()].count("\n") + 1
                offenders.append(
                    f"{os.path.relpath(path, REPO)}:{line} {m.group(1)}")
    assert not offenders, (
        "hardcoded text colours in the admin console's CSS — use a --admin-* alias "
        "so both themes work:\n  " + "\n  ".join(offenders))
