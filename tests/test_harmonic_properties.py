"""Behavioural properties of the harmonic dark and light generators."""

from itertools import combinations
from typing import cast

import pytest
from coloraide import Color
from hypothesis import example, given
from support import (
    HEX_ROUNDING_TOLERANCE,
    PINKISH,
    ROSEWATER,
    TEXT_ROLES,
    HarmonicCase,
    assert_valid_colors,
    dump_style,
    harmonic_cases,
)

from zed_theme_generator import (
    HarmonicPaletteThemeGenerator,
    build_style,
    select_colors,
)
from zed_theme_generator.light import HarmonicLightPaletteThemeGenerator


@given(case=harmonic_cases())
@example(case=PINKISH)
@example(case=ROSEWATER)
def test_harmonic_theme_invariants(case: HarmonicCase) -> None:
    params = case.params
    palette = select_colors(params, direction=case.direction)
    functional = dump_style(build_style(palette, appearance=case.appearance))
    class_based = dump_style(case.make_generator(params).build_theme())

    # Deterministic, and the class path agrees with the functional one.
    assert functional == class_based

    assert_valid_colors(functional)

    # WCAG floors derived from the drawn params, re-measured from the 8-bit
    # hex output against the rendered background.
    background = Color(str(functional["background"]))
    syntax = functional["syntax"]
    assert isinstance(syntax, dict)

    def contrast(key: str) -> float:
        return Color(str(functional[key])).contrast(background)

    def syntax_contrast(token: str) -> float:
        entry = syntax[token]
        assert isinstance(entry, dict)
        return Color(str(entry["color"])).contrast(background)

    tolerance = HEX_ROUNDING_TOLERANCE
    assert contrast("editor.foreground") >= params.floor_primary - tolerance
    assert contrast("text") >= params.floor_primary - tolerance
    for token in ("keyword", "function", "string", "type", "number"):
        assert syntax_contrast(token) >= params.floor_syntax - tolerance, token
    assert syntax_contrast("comment") >= params.floor_muted - tolerance
    assert contrast("text.muted") >= params.floor_muted - tolerance
    assert contrast("predictive") >= params.floor_subtle - tolerance
    assert contrast("editor.line_number") >= params.floor_line_number - tolerance

    # Every pair of text roles stays perceptually separated.
    colors = cast("dict[str, Color]", palette)
    for a, b in combinations(TEXT_ROLES, 2):
        delta = colors[a].delta_e(colors[b], method="ok")
        assert delta >= params.min_text_delta, (a, b, delta)

    # The foreground sits on the far side of the background for the direction.
    bg_lightness = colors["bg"].convert("oklch")["lightness"]
    fg_lightness = colors["fg_editor"].convert("oklch")["lightness"]
    if case.direction > 0:
        assert fg_lightness > bg_lightness
    else:
        assert fg_lightness < bg_lightness


@given(case=harmonic_cases())
@example(case=PINKISH)
@example(case=ROSEWATER)
def test_wrong_direction_rejected(case: HarmonicCase) -> None:
    with pytest.raises(ValueError, match="generation needs a"):
        select_colors(case.params, direction=-case.direction)
    mismatched = (
        HarmonicLightPaletteThemeGenerator
        if case.direction > 0
        else HarmonicPaletteThemeGenerator
    )
    with pytest.raises(ValueError, match="generation needs a"):
        mismatched(case.params).build_theme()
