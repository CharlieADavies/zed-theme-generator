"""Behavioural properties of the rainbow generator."""

from dataclasses import replace
from typing import cast

import pytest
from coloraide import Color
from hypothesis import example, given
from hypothesis import strategies as st
from support import (
    MURK_CASE,
    NEON_CASE,
    RainbowCase,
    assert_achromatic_hex,
    assert_cursor_visible,
    assert_valid_colors,
    dump_style,
    grey_hex,
    oklch_hex,
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
    build_rainbow_palette,
    candidate_backgrounds,
    mean_bg_distance,
    required_floor,
    select_background,
)

# A cycled repeat walks lightness away from the background, so it normally
# keeps at least its base's WCAG contrast. Two bounded, measured exceptions:
# (1) a base sitting on the background's own side (darker than a dark
# background, lighter than a light one) is crossed by its repeats — that loss
# is capped by the base's entire headroom, contrast(base, bg) - 1 <= ~0.45
# for the drawn background bands; (2) a base quantised against the white/black
# gamut corner sheds chroma (and with it luminance) while its repeats step in
# whole sRGB units — up to ~0.6 (P1's corner measurement). The two cannot
# compound: one needs the base next to the background, the other next to the
# far gamut corner.
REPEAT_CONTRAST_ALLOWANCE = 0.65

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
    assert_cursor_visible(dump)

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
    # input count cycle back through the list as lightness-shifted repeats.
    n = len(case.colors)
    for i, given_hex in enumerate(case.colors):
        assert hex_rgba(palette[ROLE_ORDER[i]]) == hex_rgba(Color(given_hex)), i

    # All slots sharing one base input are pairwise hex-distinct, and every
    # repeat holds its base's contrast against the background up to
    # REPEAT_CONTRAST_ALLOWANCE (see its derivation above).
    bg = palette["bg"]
    for base_index in range(n):
        roles = [ROLE_ORDER[i] for i in range(base_index, len(ROLE_ORDER), n)]
        hexes = [hex_rgba(palette[role]) for role in roles]
        assert len(set(hexes)) == len(hexes), (base_index, hexes)
        base_contrast = palette[roles[0]].contrast(bg)
        for role in roles[1:]:
            loss = base_contrast - palette[role].contrast(bg)
            assert loss <= REPEAT_CONTRAST_ALLOWANCE, (role, loss)

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


@given(case=rainbow_cases(), alpha=st.integers(min_value=0, max_value=255))
@example(case=NEON_CASE, alpha=0)
def test_rainbow_input_alpha_is_stripped(case: RainbowCase, alpha: int) -> None:
    """#rrggbbaa inputs render the identical theme to their #rrggbb parents."""
    suffix = f"{alpha:02x}"
    alphaed = RainbowParams.from_strings(
        name="prop",
        colors=tuple(color + suffix for color in case.colors),
        background=None if case.background is None else case.background + suffix,
        status_colors=(
            None
            if case.status_colors is None
            else tuple(color + suffix for color in case.status_colors)
        ),
    )
    alphaed_case = RainbowCase(
        alphaed, case.colors, case.background, case.status_colors
    )
    assert _render(alphaed_case) == _render(case)


@given(
    lightness=st.floats(min_value=0.0, max_value=1.0),
    chroma=st.floats(min_value=0.0, max_value=0.08),
    hue=st.floats(min_value=0.0, max_value=360.0, exclude_max=True),
    offset=st.floats(min_value=0.01, max_value=5.0),
    colors=st.lists(
        oklch_hex(lightness=(0.10, 0.985), chroma=(0.0, 0.30)), min_size=2, max_size=4
    ),
)
def test_rainbow_unreachable_floor_rejected(
    lightness: float, chroma: float, hue: float, offset: float, colors: list[str]
) -> None:
    """An explicit background that cannot host the floor fails fast, loudly.

    Achievability is computed exactly the way `build_rainbow_palette` does —
    fit the verbatim background, then measure against the gamut extreme on
    the background's own side — so the drawn floor sits just past whatever
    this background can reach. The input colours are irrelevant to the
    refusal (they are never contrast-nudged) and are drawn only to prove it.
    """
    background = (
        Color("oklch", [0.05 + lightness * (0.985 - 0.05), chroma, hue])
        .fit("srgb")
        .convert("srgb")
        .to_string(hex=True)
    )
    params = RainbowParams.from_strings(
        name="prop", colors=tuple(colors), background=background
    )
    fitted = Color(background).convert("oklch").fit("srgb")
    extreme = "white" if fitted["lightness"] < 0.5 else "black"
    achievable = Color(extreme).contrast(fitted)
    params = replace(params, minimum_bg_contrast=achievable + offset)
    with pytest.raises(ValueError, match="unreachable"):
        build_rainbow_palette(params)


# --- all-grey inputs stay achromatic --------------------------------------------

# The four status roles are the only palette entries allowed chroma in an
# all-grey theme: their hues are semantic anchors, not derived defaults, and
# with no input hue to snap to they sit on the pure anchors.
STATUS_ANCHORS = {
    "error": HUE_RED,
    "warning": HUE_YELLOW,
    "success": HUE_GREEN,
    "info": HUE_BLUE,
}


@given(
    colors=st.lists(
        grey_hex(lightness=(0.70, 0.985)), min_size=2, max_size=4, unique=True
    )
)
def test_all_grey_inputs_make_a_grayscale_theme(colors: list[str]) -> None:
    """Grey inputs yield grey outputs: no default hue sneaks in anywhere.

    The auto-selected background is neutral by construction (no input hue
    reaches the candidate grid), the repeat walk shifts all three sRGB
    channels together, and every derived chroma is zeroed — so everything
    except the semantically-anchored status roles ships as a grey hex.
    """
    params = RainbowParams.from_strings(name="prop", colors=tuple(colors))
    palette = cast("dict[str, Color]", build_rainbow_palette(params))
    for role, value in palette.items():
        if role in STATUS_ANCHORS:
            continue
        if role == "accents":
            for index, entry in enumerate(cast("list[Color]", value)):
                assert_achromatic_hex(entry, role=f"accents[{index}]")
            continue
        assert_achromatic_hex(cast("Color", value), role=role)
    for role, anchor in STATUS_ANCHORS.items():
        ok = palette[role].convert("oklch")
        assert ok["chroma"] > 0.01, role  # status semantics stay visible
        assert hue_distance(ok["hue"] % 360, anchor) <= 2.0, (role, ok["hue"], anchor)
