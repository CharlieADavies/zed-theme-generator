"""Behavioural properties of the rainbow generator."""

from typing import cast

import pytest
from coloraide import Color
from hypothesis import example, given
from support import (
    MURK_CASE,
    NEON_CASE,
    RainbowCase,
    assert_valid_colors,
    dump_style,
    rainbow_cases,
)

from zed_theme_generator import (
    HUE_BLUE,
    HUE_GREEN,
    HUE_RED,
    HUE_YELLOW,
    hex_rgba,
    hue_distance,
    render_theme_json,
    theme_family_payload,
)
from zed_theme_generator.rainbow import (
    ROLE_ORDER,
    RainbowParams,
    RainbowThemeGenerator,
    candidate_backgrounds,
    mean_bg_distance,
    required_floor,
    select_background,
)

# Oracle for select_background: the documented selection rule, restated
# independently of the implementation's search order.


def _floors(params: RainbowParams) -> list[float]:
    return [
        required_floor(i, len(params.colors), params) for i in range(len(params.colors))
    ]


def _is_feasible(candidate: Color, params: RainbowParams) -> bool:
    return all(
        candidate.contrast(color) >= floor
        for color, floor in zip(params.colors, _floors(params), strict=True)
    )


def _fallback_score(candidate: Color, params: RainbowParams) -> float:
    return min(
        candidate.contrast(color) / floor
        for color, floor in zip(params.colors, _floors(params), strict=True)
        if floor > 0
    )


def _render(case: RainbowCase) -> str:
    generator = RainbowThemeGenerator(case.params)
    return render_theme_json(
        theme_family_payload(
            generator.build_theme(),
            name="prop",
            appearance=generator.theme_appearance(),
        ),
        generator.comment_lines(),
    )


@given(case=rainbow_cases())
@example(case=NEON_CASE)
@example(case=MURK_CASE)
def test_rainbow_theme_invariants(case: RainbowCase) -> None:
    # Deterministic: two fresh generators render identical JSON.
    assert _render(case) == _render(case)

    generator = RainbowThemeGenerator(case.params)
    palette = cast("dict[str, Color]", generator.palette)
    dump = dump_style(generator.build_theme())
    assert_valid_colors(dump)

    # One unified background across the whole chrome.
    for key in (
        "editor.background",
        "terminal.background",
        "status_bar.background",
        "title_bar.background",
        "toolbar.background",
    ):
        assert dump[key] == dump["background"], key

    # The background's lightness decides the side.
    bg_lightness = palette["bg"].convert("oklch")["lightness"]
    appearance = generator.theme_appearance().value
    assert appearance == ("dark" if bg_lightness < 0.5 else "light")

    # An explicit background is used verbatim.
    if case.background is not None:
        assert dump["background"] == hex_rgba(Color(case.background))

    # Input colour i lands verbatim in prominence slot i; slots beyond the
    # input count are lightness-shifted repeats that never collide with their
    # verbatim source.
    n = len(case.colors)
    for i, given_hex in enumerate(case.colors):
        assert hex_rgba(palette[ROLE_ORDER[i]]) == hex_rgba(Color(given_hex)), i
    for i in range(n, len(ROLE_ORDER)):
        repeat = hex_rgba(palette[ROLE_ORDER[i]])
        assert repeat != hex_rgba(palette[ROLE_ORDER[i % n]]), ROLE_ORDER[i]

    # Status colours: verbatim when given, hue-anchored when derived.
    status_roles = ("error", "warning", "success", "info")
    if case.status_colors is not None:
        for key, source in zip(status_roles, case.status_colors, strict=True):
            assert dump[key] == hex_rgba(Color(source)), key
    else:
        anchors = {
            "error": HUE_RED,
            "warning": HUE_YELLOW,
            "success": HUE_GREEN,
            "info": HUE_BLUE,
        }
        for role, anchor in anchors.items():
            hue = palette[role].convert("oklch")["hue"] % 360
            # 30 degrees of input-hue snapping plus gamut-fit slack.
            assert hue_distance(hue, anchor) <= 35.0, (role, hue)


@given(case=rainbow_cases(explicit_background=False))
@example(case=NEON_CASE)
@example(case=MURK_CASE)
def test_background_selection_optimal(case: RainbowCase) -> None:
    params = case.params
    colors = list(params.colors)
    chosen = select_background(colors, params)
    candidates = candidate_backgrounds(colors, params)
    feasible = [c for c in candidates if _is_feasible(c, params)]
    if feasible:
        # Primary rule: feasible, and farthest from the inputs on average.
        assert _is_feasible(chosen, params)
        chosen_distance = mean_bg_distance(chosen, colors)
        for candidate in feasible:
            assert chosen_distance >= mean_bg_distance(candidate, colors)
    else:
        # Fallback: maximise the worst contrast-to-floor ratio, ties broken
        # by mean distance.
        chosen_score = _fallback_score(chosen, params)
        chosen_distance = mean_bg_distance(chosen, colors)
        for candidate in candidates:
            score = _fallback_score(candidate, params)
            assert chosen_score >= score
            if score == chosen_score:
                assert chosen_distance >= mean_bg_distance(candidate, colors)


def test_validation() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        RainbowParams.from_strings(name="x", colors=("#ff004c",))
    with pytest.raises(ValueError, match="exactly 4"):
        RainbowParams.from_strings(
            name="x",
            colors=("#ff004c", "#ffe600"),
            status_colors=("#ff0000", "#00ff00", "#0000ff"),
        )
