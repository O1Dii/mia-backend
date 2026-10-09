"""M33: design token drift, structure and WCAG contrast checks."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "design" / "scripts" / "build_tokens.py"
SOURCE = ROOT / "design" / "tokens" / "mia.tokens.json"

_spec = importlib.util.spec_from_file_location("build_tokens", SCRIPT)
assert _spec and _spec.loader
build_tokens = importlib.util.module_from_spec(_spec)
sys.modules["build_tokens"] = build_tokens
_spec.loader.exec_module(build_tokens)

TOKENS = json.loads(SOURCE.read_text(encoding="utf-8"))
THEMES = ("dark", "light")


# ---------------------------------------------------------------- drift


def test_generated_files_match_source() -> None:
    for path, content in build_tokens.outputs(None).items():
        assert path.exists(), f"{path} missing: run python design/scripts/build_tokens.py"
        assert path.read_text(encoding="utf-8") == content, (
            f"{path} drifted from design/tokens/mia.tokens.json; "
            "run python design/scripts/build_tokens.py"
        )


def test_generation_is_deterministic() -> None:
    assert build_tokens.outputs(None) == build_tokens.outputs(None)


def test_check_mode_exit_code() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_android_copy_matches_when_checkout_is_available() -> None:
    android = os.environ.get("MIA_ANDROID_DIR")
    if not android:
        pytest.skip("set MIA_ANDROID_DIR to the Android checkout to verify MiaTokens.kt there")
    target = Path(android) / build_tokens.KT_ANDROID_REL
    assert target.exists(), f"{target} missing"
    expected = build_tokens.outputs(None)[build_tokens.KT_CANONICAL]
    assert target.read_text(encoding="utf-8") == expected, "Android MiaTokens.kt drifted"


# ---------------------------------------------------------------- structure


def _colors(theme: str) -> dict[str, str]:
    return dict(build_tokens.leaves(TOKENS["color"][theme]))


def test_themes_have_identical_keys_and_valid_hex() -> None:
    dark, light = _colors("dark"), _colors("light")
    assert set(dark) == set(light)
    for theme in (dark, light):
        for key, value in theme.items():
            assert re.fullmatch(r"#[0-9a-f]{6}([0-9a-f]{2})?", value), (key, value)


def test_w3c_format_markers() -> None:
    assert TOKENS["color"]["$type"] == "color"
    for group in ("radius", "space", "size", "blur", "breakpoint"):
        assert TOKENS[group]["$type"] == "dimension"
    assert TOKENS["type"]["$type"] == "typography"


def test_touch_targets_meet_guidelines() -> None:
    assert build_tokens.px_num(build_tokens.val(TOKENS["size"], "touch-web")) >= 44
    assert build_tokens.px_num(build_tokens.val(TOKENS["size"], "touch-android")) >= 48


def test_css_exposes_both_themes_and_system_preference() -> None:
    css = (ROOT / "web" / "src" / "styles" / "tokens.css").read_text(encoding="utf-8")
    assert ":root[data-theme='light']" in css
    assert ":root[data-theme='dark']" in css
    assert "@media (prefers-color-scheme: light)" in css
    assert "--mia-color-text:" in css and "--mia-shadow-3:" in css


# ---------------------------------------------------------------- contrast


def _rgba(value: str) -> tuple[float, float, float, float]:
    h = value[1:]
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
    return r, g, b, a


Rgba = tuple[float, float, float, float]


def _over(top: str | Rgba, bottom: Rgba) -> Rgba:
    r, g, b, a = _rgba(top) if isinstance(top, str) else top
    br, bg, bb, _ = bottom
    return (r * a + br * (1 - a), g * a + bg * (1 - a), b * a + bb * (1 - a), 1.0)


def _lum(c: tuple[float, float, float, float]) -> float:
    def f(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def contrast(fg: tuple[float, float, float, float], bg: tuple[float, float, float, float]) -> float:
    fg = _over(fg, bg)
    hi, lo = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _backgrounds(theme: str) -> dict[str, tuple[float, float, float, float]]:
    c = _colors(theme)
    base = _rgba(c["bg"])
    opacity = build_tokens.val(TOKENS["gradient"]["aurora-opacity"], theme)
    out = {"bg": base, "bg-raised": _rgba(c["bg-raised"])}
    for level in ("surface-1", "surface-2", "surface-3"):
        out[f"{level}/bg"] = _over(c[level], base)
    out["surface-solid"] = _rgba(c["surface-solid"])
    out["surface-solid-3"] = _rgba(c["surface-solid-3"])
    for n in (1, 2, 3):
        r, g, b, _ = _rgba(c[f"aurora-{n}"])
        blob = _over((r, g, b, opacity), base)
        out[f"surface-1/aurora-{n}"] = _over(c["surface-1"], blob)
    return out


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("text", ["text", "text-secondary", "text-muted"])
def test_text_on_every_background_is_aa(theme: str, text: str) -> None:
    c = _colors(theme)
    for name, bg in _backgrounds(theme).items():
        ratio = contrast(_rgba(c[text]), bg)
        assert ratio >= 4.5, f"{theme}: {text} on {name} = {ratio:.2f}"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("role", ["accent-text", "danger", "warning", "success", "info"])
def test_status_and_accent_text_on_page_and_glass_is_aa(theme: str, role: str) -> None:
    c = _colors(theme)
    for name in ("bg", "surface-1/bg", "surface-solid", "surface-1/aurora-1"):
        ratio = contrast(_rgba(c[role]), _backgrounds(theme)[name])
        assert ratio >= 4.5, f"{theme}: {role} on {name} = {ratio:.2f}"


@pytest.mark.parametrize("theme", THEMES)
def test_text_on_accent_fill_is_aa(theme: str) -> None:
    c = _colors(theme)
    for fill in (c["accent"], c["accent-hover"]):
        assert contrast(_rgba(c["on-accent"]), _rgba(fill)) >= 4.5
    if theme == "dark":
        for stop in build_tokens.val(TOKENS["gradient"], "accent"):
            assert contrast(_rgba(c["on-accent"]), _rgba(stop["color"])) >= 4.5


@pytest.mark.parametrize("theme", THEMES)
def test_soft_status_backgrounds_keep_text_readable(theme: str) -> None:
    c = _colors(theme)
    base = _rgba(c["bg"])
    for role in ("danger", "warning", "success"):
        soft = _over(c[f"{role}-soft"], _over(c["surface-1"], base))
        assert contrast(_rgba(c[role]), soft) >= 4.5, (theme, role)
    soft = _over(c["accent-soft"], _over(c["surface-1"], base))
    assert contrast(_rgba(c["accent-text"]), soft) >= 4.5


@pytest.mark.parametrize("theme", THEMES)
def test_focus_ring_is_visible_non_text_contrast(theme: str) -> None:
    c = _colors(theme)
    for name in ("bg", "surface-1/bg", "surface-solid"):
        assert contrast(_rgba(c["focus"]), _backgrounds(theme)[name]) >= 3.0
