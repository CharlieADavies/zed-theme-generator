"""End-to-end invariants for the harmonic light generator using rosewater inputs."""

import json
import pathlib
import re
from itertools import combinations
from typing import cast

import pytest
from coloraide import Color

from zed_theme_generator import LIGHT_DIRECTION, ThemeParams, select_colors
from zed_theme_generator.gen.zed_theme import ThemeStyleContent
from zed_theme_generator.light import HarmonicLightPaletteThemeGenerator

HEX_RGBA = re.compile(r"^#[0-9a-f]{8}$")
STYLE_KEYS = {
    field.alias or name for name, field in ThemeStyleContent.model_fields.items()
}
# Absolute slack for floors re-measured from 8-bit hex output.
HEX_ROUNDING_TOLERANCE = 0.05

# Default master knobs; the tests read derived floors from here.
KNOBS = ThemeParams.from_strings(
    name="probe", background="#fdf4f8", foreground="#2b1930", accent="#c02579"
)

TEXT_ROLES = [
    "fg_editor", "keyword", "string", "function", "type", "number", "property",
    "operator", "title", "punctuation", "comment", "hint", "predictive",
]
SYNTAX_ROLES = [
    "keyword", "string", "function", "type", "number", "property", "operator",
    "punctuation", "comment",
]


def _palette(
    background: str,
    accent: str,
    *,
    ui_accent_mix: float = 0.55,
    surface_tint: float = 0.3,
    border_tint: float = 0.5,
) -> dict[str, Color]:
    params = ThemeParams.from_strings(
        name="probe",
        background=background,
        foreground="#2b1930",
        accent=accent,
        ui_accent_mix=ui_accent_mix,
        surface_tint=surface_tint,
        border_tint=border_tint,
    )
    # Only the Color-valued text roles are read from this, so the cast is safe.
    return cast("dict[str, Color]", select_colors(params, direction=LIGHT_DIRECTION))


def _mean_divergence(a: dict[str, Color], b: dict[str, Color]) -> float:
    deltas = [a[role].delta_e(b[role], method="ok") for role in SYNTAX_ROLES]
    return sum(deltas) / len(deltas)


@pytest.fixture(scope="module")
def generator() -> HarmonicLightPaletteThemeGenerator:
    return HarmonicLightPaletteThemeGenerator.from_cli(
        name="rosewater",
        background="#fdf4f8",
        foreground="#2b1930",
        accent="#c02579",
    )


@pytest.fixture(scope="module")
def style_json(generator: HarmonicLightPaletteThemeGenerator) -> dict[str, object]:
    return generator.build_theme().model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def test_all_schema_keys_emitted(style_json: dict[str, object]) -> None:
    assert set(style_json) == STYLE_KEYS


def test_collections_shapes(style_json: dict[str, object]) -> None:
    accents = style_json["accents"]
    players = style_json["players"]
    syntax = style_json["syntax"]
    assert isinstance(accents, list) and len(accents) == 6
    assert isinstance(players, list) and len(players) == 8
    assert isinstance(syntax, dict) and len(syntax) == 43
    for player in players:
        assert isinstance(player, dict)
        assert set(player) == {"cursor", "background", "selection"}
        assert player["background"] == "#000000ff"
        assert player["cursor"] == "#000000ff"
    for token_entry in syntax.values():
        assert isinstance(token_entry, dict)
        assert "color" in token_entry
        assert set(token_entry) <= {"color", "font_style", "font_weight"}


def test_colour_format(style_json: dict[str, object]) -> None:
    for key, value in style_json.items():
        if key in {"accents", "players", "syntax", "background.appearance"}:
            continue
        assert isinstance(value, str) and HEX_RGBA.match(value), f"{key}: {value!r}"
    assert style_json["background.appearance"] == "opaque"
    assert style_json["border.transparent"] == "#00000000"


def test_wcag_floors(style_json: dict[str, object]) -> None:
    # WCAG contrast is symmetric, so the dark-theme measurement works
    # unchanged against a light background.
    background = Color(str(style_json["background"]))
    syntax = style_json["syntax"]
    assert isinstance(syntax, dict)

    def contrast(value: object) -> float:
        return Color(str(value)).contrast(background)

    def syntax_contrast(token: str) -> float:
        token_entry = syntax[token]
        assert isinstance(token_entry, dict)
        return contrast(token_entry["color"])

    assert contrast(style_json["editor.foreground"]) >= KNOBS.floor_primary - HEX_ROUNDING_TOLERANCE
    assert contrast(style_json["text"]) >= KNOBS.floor_primary - HEX_ROUNDING_TOLERANCE
    for token in ("keyword", "function", "string", "type", "number"):
        assert syntax_contrast(token) >= KNOBS.floor_syntax - HEX_ROUNDING_TOLERANCE, token
    assert syntax_contrast("comment") >= KNOBS.floor_muted - HEX_ROUNDING_TOLERANCE
    assert contrast(style_json["text.muted"]) >= KNOBS.floor_muted - HEX_ROUNDING_TOLERANCE
    assert contrast(style_json["predictive"]) >= KNOBS.floor_subtle - HEX_ROUNDING_TOLERANCE
    assert (
        contrast(style_json["editor.line_number"])
        >= KNOBS.floor_line_number - HEX_ROUNDING_TOLERANCE
    )


def test_pairwise_text_separation() -> None:
    for background in ("#fdf4f8", "#f2fbf1", "#eefaf7"):
        palette = _palette(background, "#c02579")
        for a, b in combinations(TEXT_ROLES, 2):
            delta = palette[a].delta_e(palette[b], method="ok")
            assert delta >= KNOBS.min_text_delta, (background, a, b, delta)


def test_cross_theme_divergence() -> None:
    pink = _palette("#fdf4f8", "#c02579")
    green = _palette("#f2fbf1", "#c02579")
    teal = _palette("#eefaf7", "#c02579")
    # Measured 0.033 / 0.035 mean delta_e_ok; thresholds keep >=1.5x margin.
    assert _mean_divergence(pink, green) >= 0.02
    assert _mean_divergence(pink, teal) >= 0.02


def test_knob_divergence() -> None:
    base = _palette("#f2fbf1", "#c02579")
    soft = _palette(
        "#f2fbf1", "#c02579", ui_accent_mix=0.35, surface_tint=0.15, border_tint=0.3
    )
    # Same bg+accent: divergence must come from the knob coupling alone.
    # Measured 0.0099 mean delta_e_ok.
    assert _mean_divergence(base, soft) >= 0.005


def test_save_theme_round_trip(
    generator: HarmonicLightPaletteThemeGenerator, tmp_path: pathlib.Path
) -> None:
    style = generator.build_theme()
    path = generator.save_theme(style, name="rosewater", directory=tmp_path)
    lines = path.read_text().splitlines()
    comments = [line for line in lines if line.startswith("//")]
    assert len(comments) == 2
    assert comments[0].startswith("// inputs: ")
    assert comments[1].startswith("// palette: ")
    inputs = json.loads(comments[0].removeprefix("// inputs: "))
    assert inputs["name"] == "rosewater"
    assert inputs["background"] == "#fdf4f8ff"
    palette = json.loads(comments[1].removeprefix("// palette: "))
    assert HEX_RGBA.match(palette["keyword"])
    payload = json.loads("\n".join(line for line in lines if not line.startswith("//")))
    assert set(payload) == {"$schema", "name", "author", "themes"}
    assert payload["name"] == "rosewater"
    (theme,) = payload["themes"]
    assert theme["appearance"] == "light"
    assert theme["name"] == "rosewater-light"
    assert set(theme["style"]) == STYLE_KEYS


def test_dark_background_rejected() -> None:
    generator = HarmonicLightPaletteThemeGenerator.from_cli(
        name="nightshade",
        background="#0a1022",
        foreground="#ffe3f3",
        accent="#ee7ec6",
    )
    with pytest.raises(ValueError, match="Light generation needs a light background"):
        generator.build_theme()


def test_palette_darker_than_background() -> None:
    palette = _palette("#fdf4f8", "#c02579")
    fg_lightness = palette["fg_editor"].convert("oklch")["lightness"]
    bg_lightness = palette["bg"].convert("oklch")["lightness"]
    assert fg_lightness < bg_lightness


def test_default_direction_rejects_light_background() -> None:
    params = ThemeParams.from_strings(
        name="probe", background="#fdf4f8", foreground="#2b1930", accent="#c02579"
    )
    with pytest.raises(ValueError, match="Dark generation needs a dark background"):
        select_colors(params)
