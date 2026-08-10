"""Every t() key used by the admin console must exist in both locale files.

`t()` is `locales[language][key] ?? locales.en[key] ?? key` — a missing key does
not throw, it RENDERS THE KEY. That is how a button shipped to production reading
literally "common.refresh": TypeScript can't catch it (the argument is a string),
the build succeeds, and the page looks fine to anyone not reading the button.

A missing zh key is quieter still: it falls through to English, so the page stays
usable and half-translated, which is the kind of thing that survives a demo.

This is a source-text check rather than a runtime one because the console has no
JS test runner; adding one for this would be heavier than the check it enables.
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONSOLE = os.path.join(ROOT, "admin-console", "src")
LOCALES = os.path.join(CONSOLE, "i18n", "locales")

# `t('some.key')` — single or double quoted, whitespace tolerant.
T_CALL = re.compile(r"""\bt\(\s*['"]([A-Za-z0-9_.\-]+)['"]\s*\)""")
# A key definition at the start of a line in a locale file.
KEY_DEF = re.compile(r"""^\s*['"]([A-Za-z0-9_.\-]+)['"]\s*:""", re.M)

# Keys built by interpolation — `t(`agents.kind.${a.kind}`)` — cannot be found by
# scanning, so the possible suffixes are listed here. Each entry is
# (prefix, suffixes) and the product must exist in both locales. When a new agent
# kind is added to the backend's KIND_* constants this list is what fails.
DYNAMIC = [
    ("agents.kind.", ["orchestrator", "specialist", "voice", "variant", "tool"]),
]


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _locale_keys(name):
    return set(KEY_DEF.findall(_read(os.path.join(LOCALES, f"{name}.ts"))))


def _tsx_files():
    for base, _dirs, files in os.walk(CONSOLE):
        if "i18n" in base.split(os.sep):
            continue
        for f in files:
            if f.endswith((".tsx", ".ts")):
                yield os.path.join(base, f)


def _used_keys():
    """Every literal t() key, mapped to the files that use it."""
    used = {}
    for path in _tsx_files():
        for key in T_CALL.findall(_read(path)):
            used.setdefault(key, set()).add(os.path.relpath(path, ROOT))
    return used


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_every_used_key_is_defined(locale):
    keys = _locale_keys(locale)
    missing = {k: sorted(v) for k, v in _used_keys().items() if k not in keys}
    assert not missing, (
        f"{locale}.ts is missing {len(missing)} key(s); each renders as its own "
        f"name in the UI: {missing}")


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_interpolated_keys_cover_every_variant(locale):
    keys = _locale_keys(locale)
    missing = [f"{p}{s}" for p, suffixes in DYNAMIC for s in suffixes
               if f"{p}{s}" not in keys]
    assert not missing, f"{locale}.ts is missing interpolated key(s): {missing}"


def test_the_two_locales_define_the_same_keys():
    """An en-only key renders English inside a Chinese page; a zh-only key is dead
    weight that suggests a rename went half-done."""
    en, zh = _locale_keys("en"), _locale_keys("zh")
    assert not (en - zh), f"defined in en but not zh: {sorted(en - zh)}"
    assert not (zh - en), f"defined in zh but not en: {sorted(zh - en)}"


def test_the_scanner_actually_finds_keys():
    """A regex that matched nothing would make every assertion above vacuous."""
    used = _used_keys()
    assert len(used) > 100, f"only found {len(used)} t() calls — scanner broken?"
    assert "agents.title" in used


def test_the_agent_kind_list_matches_the_backend():
    """The dynamic suffixes are hand-listed here; this ties them to the KIND_*
    constants that actually produce the value, so adding a kind fails loudly."""
    import sys
    sys.path.insert(0, os.path.join(ROOT, "cdk", "lambda", "admin-api"))
    import agents as fleet

    backend = {v for k, v in vars(fleet).items()
               if k.startswith("KIND_") and isinstance(v, str)}
    listed = {s for p, suffixes in DYNAMIC if p == "agents.kind."
              for s in suffixes}
    assert backend == listed, (
        f"backend kinds {sorted(backend)} != translated kinds {sorted(listed)}")
