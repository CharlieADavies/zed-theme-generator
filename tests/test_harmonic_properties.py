"""Behavioural properties of the harmonic dark and light generators."""

import math
from dataclasses import replace
from itertools import combinations
from typing import NamedTuple, cast

import pytest
from coloraide import Color
from hypothesis import assume, example, given
from hypothesis import strategies as st
from support import (
    HEX_ROUNDING_TOLERANCE,
    PINKISH,
    ROSEWATER,
    TEXT_ROLES,
    HarmonicCase,
    assert_achromatic_hex,
    assert_cursor_visible,
    assert_valid_colors,
    dump_style,
    grey_hex,
    harmonic_cases,
    oklch_hex,
)

from zed_theme_generator import (
    DARK_DIRECTION,
    HUE_BLUE,
    HUE_GREEN,
    HUE_RED,
    HUE_YELLOW,
    LIGHT_DIRECTION,
    AppearanceContent,
    HarmonicPaletteThemeGenerator,
    HarmonyType,
    ThemeParams,
    build_style,
    hue_distance,
    select_colors,
)
from zed_theme_generator.generator import TINT_ARC_MAX
from zed_theme_generator.light import HarmonicLightPaletteThemeGenerator
from zed_theme_generator.schemas import MIN_L

# Every syntax token key whose colour must clear a documented floor tier,
# grouped by the floor it is promised: hue-bearing tokens hold floor_syntax,
# comments (and their doc variant) floor_muted, hint/predictive floor_subtle.
SYNTAX_FLOOR_TOKENS = (
    "keyword",
    "function",
    "string",
    "type",
    "number",
    "property",
    "operator",
    "punctuation",
    "title",
)
MUTED_FLOOR_TOKENS = ("comment", "comment.doc")


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
    assert_cursor_visible(functional)

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
    for token in SYNTAX_FLOOR_TOKENS:
        assert syntax_contrast(token) >= params.floor_syntax - tolerance, token
    for token in MUTED_FLOOR_TOKENS:
        assert syntax_contrast(token) >= params.floor_muted - tolerance, token
    assert contrast("text.muted") >= params.floor_muted - tolerance
    assert contrast("hint") >= params.floor_subtle - tolerance
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
        # Light direction: no text role walks to pure black. Measured on the
        # palette colour that ships; gamut fitting holds oklch lightness, so
        # only float noise is allowed below the clamp.
        for role in TEXT_ROLES:
            lightness = colors[role].convert("oklch")["lightness"]
            assert lightness >= MIN_L - 1e-4, (role, lightness)


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


# --- achromatic backgrounds lean on the accent ---------------------------------

# Palette roles that must carry the accent's cast when the background itself
# has no hue: every one of these gets an explicit chroma, so its hue survives
# serialisation. Chrome surfaces (surface/element/...) inherit the achromatic
# background's zero chroma and stay grey — their hue is unobservable — so the
# cast is asserted on the chroma-bearing roles only.
ACCENT_CAST_ROLES = (
    "border",
    "border_variant",
    "border_disabled",
    "border_focused",
    "border_selected",
    "line_number",
)


@st.composite
def _achromatic_bg_cases(draw: st.DrawFn) -> HarmonicCase:
    dark = draw(st.booleans())
    background = draw(grey_hex(lightness=(0.05, 0.25) if dark else (0.90, 0.985)))
    foreground = draw(
        oklch_hex(lightness=(0.60, 0.98) if dark else (0.05, 0.40), chroma=(0.0, 0.12))
    )
    # Accent chroma bounded away from zero: the property needs a genuinely
    # chromatic accent whose band hue is well-defined.
    accent = draw(oklch_hex(lightness=(0.40, 0.90), chroma=(0.05, 0.25)))
    params = ThemeParams.from_strings(
        name="prop",
        background=background,
        foreground=foreground,
        accent=accent,
        minimum_bg_contrast=draw(st.floats(min_value=7.0, max_value=11.0)),
        accent_mix=draw(st.floats(min_value=0.0, max_value=100.0)),
        surface_blend=draw(st.floats(min_value=0.0, max_value=100.0)),
        border_blend=draw(st.floats(min_value=0.0, max_value=100.0)),
    )
    if dark:
        return HarmonicCase(
            params,
            DARK_DIRECTION,
            AppearanceContent.dark,
            HarmonicPaletteThemeGenerator,
        )
    return HarmonicCase(
        params,
        LIGHT_DIRECTION,
        AppearanceContent.light,
        HarmonicLightPaletteThemeGenerator,
    )


@given(case=_achromatic_bg_cases())
def test_achromatic_background_leans_on_accent(case: HarmonicCase) -> None:
    """A hueless background borrows the accent's hue, not the pink fallback.

    With bg_hue == accent_hue, `hue_towards(bg_hue, accent_hue, tint)` is the
    identity for every tint, so chrome and borders sit on the accent band's
    hue up to gamut-fit/8-bit wobble. Typical wobble is <= 0.13 degrees, but
    a background that quantises to the pure-black corner leaves so little
    chroma on the disabled border that a single channel step swings its hue
    past 2 degrees (measured 2.1). The tolerance is 5.0: still an order of
    magnitude below the drift any fallback hue would produce — this property
    guards against defaulted hues, not sub-degree precision.
    """
    palette = cast(
        "dict[str, Color]", select_colors(case.params, direction=case.direction)
    )
    accent_hue = palette["accent"].convert("oklch")["hue"] % 360
    for role in ACCENT_CAST_ROLES:
        hue = palette[role].convert("oklch")["hue"]
        assert not math.isnan(hue), role  # the role carries real chroma
        assert hue_distance(hue % 360, accent_hue) <= 5.0, (role, hue, accent_hue)


# --- achromatic inputs stay achromatic ------------------------------------------

# The four status roles are the only palette entries allowed chroma in an
# all-grey theme: their hues are semantic anchors (red error etc.), not
# derived defaults, and an achromatic theme has no wheel to snap them to.
STATUS_ANCHORS = {
    "error": HUE_RED,
    "warning": HUE_YELLOW,
    "success": HUE_GREEN,
    "info": HUE_BLUE,
}


@st.composite
def _achromatic_theme_cases(draw: st.DrawFn) -> HarmonicCase:
    dark = draw(st.booleans())
    params = ThemeParams.from_strings(
        name="prop",
        background=draw(grey_hex(lightness=(0.05, 0.25) if dark else (0.90, 0.985))),
        foreground=draw(grey_hex(lightness=(0.60, 0.98) if dark else (0.05, 0.40))),
        accent=draw(grey_hex(lightness=(0.40, 0.90))),
        minimum_bg_contrast=draw(st.floats(min_value=7.0, max_value=11.0)),
        accent_mix=draw(st.floats(min_value=0.0, max_value=100.0)),
        surface_blend=draw(st.floats(min_value=0.0, max_value=100.0)),
        border_blend=draw(st.floats(min_value=0.0, max_value=100.0)),
    )
    if dark:
        return HarmonicCase(
            params,
            DARK_DIRECTION,
            AppearanceContent.dark,
            HarmonicPaletteThemeGenerator,
        )
    return HarmonicCase(
        params,
        LIGHT_DIRECTION,
        AppearanceContent.light,
        HarmonicLightPaletteThemeGenerator,
    )


@given(case=_achromatic_theme_cases())
def test_achromatic_inputs_make_a_grayscale_theme(case: HarmonicCase) -> None:
    """Grey inputs yield grey outputs: no default hue sneaks in anywhere."""
    palette = cast(
        "dict[str, Color]", select_colors(case.params, direction=case.direction)
    )
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


@st.composite
def _achromatic_accent_cases(draw: st.DrawFn) -> HarmonicCase:
    dark = draw(st.booleans())
    background = draw(
        oklch_hex(
            lightness=(0.05, 0.25) if dark else (0.90, 0.985), chroma=(0.03, 0.08)
        )
    )
    # Low-lightness gamut fitting can crush the drawn chroma to an exact grey;
    # the property needs a background whose hue survives serialisation.
    assume(not Color(background).convert("oklch").is_nan("hue"))
    params = ThemeParams.from_strings(
        name="prop",
        background=background,
        foreground=draw(
            oklch_hex(
                lightness=(0.60, 0.98) if dark else (0.05, 0.40), chroma=(0.0, 0.12)
            )
        ),
        accent=draw(grey_hex(lightness=(0.40, 0.90))),
        minimum_bg_contrast=draw(st.floats(min_value=7.0, max_value=11.0)),
        accent_mix=draw(st.floats(min_value=0.0, max_value=100.0)),
        surface_blend=draw(st.floats(min_value=0.0, max_value=100.0)),
        border_blend=draw(st.floats(min_value=0.0, max_value=100.0)),
    )
    if dark:
        return HarmonicCase(
            params,
            DARK_DIRECTION,
            AppearanceContent.dark,
            HarmonicPaletteThemeGenerator,
        )
    return HarmonicCase(
        params,
        LIGHT_DIRECTION,
        AppearanceContent.light,
        HarmonicLightPaletteThemeGenerator,
    )


@given(case=_achromatic_accent_cases())
def test_achromatic_accent_borrows_background_hue(case: HarmonicCase) -> None:
    """A hueless accent takes the background's hue, the mirror property.

    With the pink fallback gone the only input hue left is the background's,
    so the accent band and every accent-cast role sit on it exactly (the
    lean from bg hue towards accent hue is the identity here too). The 5.0
    tolerance matches the lean property above: near the black corner a
    low-chroma border's hue swings a couple of degrees per channel step.
    """
    palette = cast(
        "dict[str, Color]", select_colors(case.params, direction=case.direction)
    )
    bg_hue = palette["bg"].convert("oklch")["hue"] % 360
    for role in ("accent", *ACCENT_CAST_ROLES):
        hue = palette[role].convert("oklch")["hue"]
        assert not math.isnan(hue), role
        assert hue_distance(hue % 360, bg_hue) <= 5.0, (role, hue, bg_hue)


# --- input alpha is stripped ----------------------------------------------------


class _AlphaCase(NamedTuple):
    direction: float
    appearance: AppearanceContent
    background: str
    foreground: str
    accent: str
    alphas: tuple[int, int, int]


@st.composite
def _alpha_cases(draw: st.DrawFn) -> _AlphaCase:
    dark = draw(st.booleans())
    byte = st.integers(min_value=0, max_value=255)
    return _AlphaCase(
        direction=DARK_DIRECTION if dark else LIGHT_DIRECTION,
        appearance=AppearanceContent.dark if dark else AppearanceContent.light,
        background=draw(
            oklch_hex(
                lightness=(0.05, 0.25) if dark else (0.90, 0.985), chroma=(0.0, 0.08)
            )
        ),
        foreground=draw(
            oklch_hex(
                lightness=(0.60, 0.98) if dark else (0.05, 0.40), chroma=(0.0, 0.12)
            )
        ),
        accent=draw(oklch_hex(lightness=(0.40, 0.90), chroma=(0.02, 0.25))),
        alphas=(draw(byte), draw(byte), draw(byte)),
    )


@given(case=_alpha_cases())
def test_input_alpha_is_stripped(case: _AlphaCase) -> None:
    """#rrggbbaa inputs generate the identical theme to their #rrggbb parents."""

    def theme(background: str, foreground: str, accent: str) -> dict[str, object]:
        params = ThemeParams.from_strings(
            name="prop", background=background, foreground=foreground, accent=accent
        )
        return dump_style(
            build_style(
                select_colors(params, direction=case.direction),
                appearance=case.appearance,
            )
        )

    a_bg, a_fg, a_ac = (f"{value:02x}" for value in case.alphas)
    assert theme(
        case.background + a_bg, case.foreground + a_fg, case.accent + a_ac
    ) == theme(case.background, case.foreground, case.accent)


# --- unreachable contrast floors are refused up front ---------------------------


@given(
    dark=st.booleans(),
    lightness=st.floats(min_value=0.0, max_value=1.0),
    chroma=st.floats(min_value=0.0, max_value=0.08),
    hue=st.floats(min_value=0.0, max_value=360.0, exclude_max=True),
    offset=st.floats(min_value=0.01, max_value=5.0),
)
def test_unreachable_floor_rejected(
    dark: bool, lightness: float, chroma: float, hue: float, offset: float
) -> None:
    """A floor above the background's best-case contrast fails fast, loudly.

    Achievability is computed exactly the way `select_colors` does — cap the
    background's chroma, fit, then measure against the direction's gamut
    extreme — so the drawn floor sits just past whatever this background can
    actually reach, mid-lightness backgrounds included.
    """
    # Rescale the drawn lightness into the direction's side, staying clear of
    # the 0.5 boundary so hex rounding cannot flip the direction check.
    lo, hi = (0.05, 0.49) if dark else (0.51, 0.985)
    background = (
        Color("oklch", [lo + lightness * (hi - lo), chroma, hue])
        .fit("srgb")
        .convert("srgb")
        .to_string(hex=True)
    )
    params = ThemeParams.from_strings(
        name="prop", background=background, foreground="#888888", accent="#ee7ec6"
    )
    bg = Color(background).convert("oklch")
    bg["chroma"] = min(bg["chroma"], params.bg_chroma_cap)
    bg.fit("srgb")
    achievable = Color("white" if dark else "black").contrast(bg)
    params = replace(params, minimum_bg_contrast=achievable + offset)
    direction = DARK_DIRECTION if dark else LIGHT_DIRECTION
    with pytest.raises(ValueError, match="unreachable"):
        select_colors(params, direction=direction)


# --- monochromatic harmony stays monochromatic -----------------------------------

# The hue arc every mono syntax token must stay inside, around the accent band
# hue, derived from the code rather than guessed:
#   30  — MONO_FAMILIES = {0, 1, 11} on the 12-step wheel: seeds sit within
#         +/-30 degrees of the accent;
#   80 * syntax_cast — every family hue then leans towards the background hue
#         by at most TINT_ARC_MAX * syntax_cast degrees (a user knob);
#   72  — the text-role separation pass may fan a colliding token's hue by up
#         to the widest far step in `_place_role` (mono seeds collide on hue
#         by construction, so the fan is exercised routinely; capping it was
#         tried and reverted — the far fan is only reached when nothing
#         nearer clears `min_text_delta`, so a cap trades distinguishability
#         for hue purity);
#   2   — gamut-fit hue wobble on the serialised colour.
# The bound keeps teeth: wheel's family map seeds type 150 degrees from the
# accent, outside this arc for all but the largest surface_blend settings
# (and `test_mono_differs_from_wheel` pins the alias regression directly).
_MONO_FAMILY_ARC = 30.0
_SEPARATION_FAN_ARC = 72.0
_FIT_HUE_SLACK = 2.0


@st.composite
def _mono_cases(draw: st.DrawFn) -> HarmonicCase:
    dark = draw(st.booleans())
    background = draw(
        oklch_hex(lightness=(0.05, 0.25) if dark else (0.90, 0.985), chroma=(0.0, 0.08))
    )
    foreground = draw(
        oklch_hex(lightness=(0.60, 0.98) if dark else (0.05, 0.40), chroma=(0.0, 0.12))
    )
    # A chromatic accent keeps the band hue (the arc's centre) well-defined.
    accent = draw(oklch_hex(lightness=(0.40, 0.90), chroma=(0.05, 0.25)))
    params = ThemeParams.from_strings(
        name="prop",
        background=background,
        foreground=foreground,
        accent=accent,
        minimum_bg_contrast=draw(st.floats(min_value=7.0, max_value=11.0)),
        harmony_type="monochromatic",
        accent_mix=draw(st.floats(min_value=0.0, max_value=100.0)),
        surface_blend=draw(st.floats(min_value=0.0, max_value=100.0)),
        border_blend=draw(st.floats(min_value=0.0, max_value=100.0)),
    )
    if dark:
        return HarmonicCase(
            params,
            DARK_DIRECTION,
            AppearanceContent.dark,
            HarmonicPaletteThemeGenerator,
        )
    return HarmonicCase(
        params,
        LIGHT_DIRECTION,
        AppearanceContent.light,
        HarmonicLightPaletteThemeGenerator,
    )


@given(case=_mono_cases())
def test_mono_tokens_stay_in_the_accent_band(case: HarmonicCase) -> None:
    """Monochromatic themes keep every syntax token inside the accent's arc."""
    params = case.params
    palette = cast("dict[str, Color]", select_colors(params, direction=case.direction))
    accent_hue = palette["accent"].convert("oklch")["hue"] % 360
    bound = (
        _MONO_FAMILY_ARC
        + TINT_ARC_MAX * params.syntax_cast
        + _SEPARATION_FAN_ARC
        + _FIT_HUE_SLACK
    )
    for token in SYNTAX_FLOOR_TOKENS:
        hue = palette[token].convert("oklch")["hue"]
        if math.isnan(hue):  # the token was desaturated to grey by gamut fit
            continue
        assert hue_distance(hue % 360, accent_hue) <= bound, (token, hue, accent_hue)


def test_mono_differs_from_wheel() -> None:
    """Monochromatic output is a different theme, not an alias of wheel."""

    def style(harmony: HarmonyType) -> dict[str, object]:
        params = ThemeParams.from_strings(
            name="prop",
            background="#0a1022",
            foreground="#ffe3f3",
            accent="#ee7ec6",
            harmony_type=harmony,
        )
        return dump_style(build_style(select_colors(params)))

    assert style("monochromatic") != style("wheel")
