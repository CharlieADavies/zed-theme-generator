"""End-to-end invariants for the harmonic generator using the pinkish inputs."""

import json
import pathlib
import re
from itertools import combinations
from typing import cast

import pytest
from coloraide import Color

from zed_theme_generator import (
    FLOOR_LINE_NUMBER,
    FLOOR_MUTED,
    FLOOR_PRIMARY,
    FLOOR_SUBTLE,
    FLOOR_SYNTAX,
    MIN_TEXT_DELTA,
    HarmonicPaletteThemeGenerator,
)
from zed_theme_generator.gen.zed_theme import ThemeStyleContent

HEX_RGBA = re.compile(r"^#[0-9a-f]{8}$")
STYLE_KEYS = {
    field.alias or name for name, field in ThemeStyleContent.model_fields.items()
}
# Absolute slack for floors re-measured from 8-bit hex output.
HEX_ROUNDING_TOLERANCE = 0.05

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
    generator = HarmonicPaletteThemeGenerator.from_cli(
        name="probe",
        background=background,
        foreground="#ffe3f3",
        accent=accent,
        target_contrast=0.76,
        harmony_type="wheel",
        ui_accent_mix=ui_accent_mix,
        surface_tint=surface_tint,
        border_tint=border_tint,
    )
    # Only the Color-valued text roles are read from this, so the cast is safe.
    return cast("dict[str, Color]", generator.select_colors())


def _mean_divergence(a: dict[str, Color], b: dict[str, Color]) -> float:
    deltas = [a[role].delta_e(b[role], method="ok") for role in SYNTAX_ROLES]
    return sum(deltas) / len(deltas)


@pytest.fixture(scope="module")
def generator() -> HarmonicPaletteThemeGenerator:
    return HarmonicPaletteThemeGenerator.from_cli(
        name="pinkish",
        background="#0a1022",
        foreground="#ffe3f3",
        accent="#ee7ec6",
        target_contrast=0.76,
        harmony_type="wheel",
        ui_accent_mix=0.55,
        surface_tint=0.3,
        border_tint=0.5,
    )


@pytest.fixture(scope="module")
def style_json(generator: HarmonicPaletteThemeGenerator) -> dict[str, object]:
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
    background = Color(str(style_json["background"]))
    syntax = style_json["syntax"]
    assert isinstance(syntax, dict)

    def contrast(value: object) -> float:
        return Color(str(value)).contrast(background)

    def syntax_contrast(token: str) -> float:
        token_entry = syntax[token]
        assert isinstance(token_entry, dict)
        return contrast(token_entry["color"])

    assert contrast(style_json["editor.foreground"]) >= FLOOR_PRIMARY - HEX_ROUNDING_TOLERANCE
    assert contrast(style_json["text"]) >= FLOOR_PRIMARY - HEX_ROUNDING_TOLERANCE
    for token in ("keyword", "function", "string", "type", "number"):
        assert syntax_contrast(token) >= FLOOR_SYNTAX - HEX_ROUNDING_TOLERANCE, token
    assert syntax_contrast("comment") >= FLOOR_MUTED - HEX_ROUNDING_TOLERANCE
    assert contrast(style_json["text.muted"]) >= FLOOR_MUTED - HEX_ROUNDING_TOLERANCE
    assert contrast(style_json["predictive"]) >= FLOOR_SUBTLE - HEX_ROUNDING_TOLERANCE
    assert (
        contrast(style_json["editor.line_number"])
        >= FLOOR_LINE_NUMBER - HEX_ROUNDING_TOLERANCE
    )


def test_pairwise_text_separation() -> None:
    for background in ("#001708", "#001613", "#0a1022"):
        palette = _palette(background, "#ee7ec6")
        for a, b in combinations(TEXT_ROLES, 2):
            delta = palette[a].delta_e(palette[b], method="ok")
            assert delta >= MIN_TEXT_DELTA, (background, a, b, delta)


def test_cross_theme_divergence() -> None:
    green = _palette("#001708", "#ee7ec6")
    teal = _palette("#001613", "#ee7ec6")
    navy = _palette("#0a1022", "#ee7ec6")
    # Measured 0.038 / 0.021 mean delta_e_ok; thresholds keep >=1.5x margin.
    assert _mean_divergence(green, navy) >= 0.02
    assert _mean_divergence(green, teal) >= 0.012


def test_knob_divergence() -> None:
    base = _palette("#001708", "#ee7ec6")
    soft = _palette(
        "#001708", "#ee7ec6", ui_accent_mix=0.35, surface_tint=0.15, border_tint=0.3
    )
    # Same bg+accent: divergence must come from the knob coupling alone.
    # Measured 0.0115 mean delta_e_ok.
    assert _mean_divergence(base, soft) >= 0.006


def test_syntax_font_styling(style_json: dict[str, object]) -> None:
    syntax = style_json["syntax"]
    assert isinstance(syntax, dict)
    styled = {
        token: entry
        for token, entry in syntax.items()
        if isinstance(entry, dict) and ("font_style" in entry or "font_weight" in entry)
    }
    assert set(styled) == {"emphasis", "emphasis.strong", "predictive", "title"}
    assert styled["emphasis"]["font_style"] == "italic"
    assert styled["predictive"]["font_style"] == "italic"
    assert styled["emphasis.strong"]["font_weight"] == 700
    assert styled["title"]["font_weight"] == 600


def test_save_theme_round_trip(
    generator: HarmonicPaletteThemeGenerator, tmp_path: pathlib.Path
) -> None:
    style = generator.build_theme()
    path = generator.save_theme(style, name="pinkish", directory=tmp_path)
    payload = json.loads(path.read_text())
    assert set(payload) == {"$schema", "name", "author", "themes"}
    assert payload["name"] == "pinkish"
    (theme,) = payload["themes"]
    assert theme["appearance"] == "dark"
    assert theme["name"] == "pinkish-dark"
    assert set(theme["style"]) == STYLE_KEYS
