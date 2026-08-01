"""Shared Hypothesis strategies and behavioural assertion helpers.

Strategies draw in oklch space and serialise to sRGB hex before feeding the
real `from_strings` entry points, so every drawn colour is in gamut and the
bounds are perceptual. The lightness bounds define the supported input domain:
`ensure_contrast` is best-effort near the gamut edge, so contrast floors are
only promised while the background leaves headroom for text.
"""

import re
from collections.abc import Callable
from typing import NamedTuple

from coloraide import Color
from hypothesis import strategies as st

from zed_theme_generator import (
    DARK_DIRECTION,
    HARMONY_TO_COLORAIDE,
    LIGHT_DIRECTION,
    AppearanceContent,
    HarmonicPaletteThemeGenerator,
    ThemeGenerator,
    ThemeParams,
    hex_rgba,
)
from zed_theme_generator.gen.zed_theme import ThemeStyleContent
from zed_theme_generator.light import HarmonicLightPaletteThemeGenerator
from zed_theme_generator.rainbow import RainbowParams

HEX_RGBA = re.compile(r"^#[0-9a-f]{8}$")
# Absolute slack for floors re-measured from 8-bit hex output. Derivation:
# rendering to hex moves every sRGB channel by up to 1/510 (half an 8-bit
# step) on BOTH the text colour and the background. WCAG contrast is
# C = (Y_hi + 0.05) / (Y_lo + 0.05), so the worst-case drift is
#   dC <= (dY_text + C * dY_bg) / (Y_bg + 0.05)   [dark side], and the
# mirror with the roles swapped on the light side. With dY <= slope/510 and
# the sRGB linearisation slope (2.4/1.055) * ((v + 0.055)/1.055)^1.4 taken at
# the lightness where each floor binds, the strategy corners give ~0.11 on
# the dark side (bg L 0.25, C 10.5) and ~0.133 on the light side (bg L 0.985,
# where a *dark* text's small Y+0.05 denominator amplifies its own rounding).
# Sweeping 200k floor-landing pairs over the drawn domain measured 0.1065.
# 0.14 = the analytic worst case, rounded up.
HEX_ROUNDING_TOLERANCE = 0.14

# Every Color-valued palette role that renders as text on the editor surface.
TEXT_ROLES = [
    "fg_editor",
    "keyword",
    "string",
    "function",
    "type",
    "number",
    "property",
    "operator",
    "title",
    "punctuation",
    "comment",
    "hint",
    "predictive",
]


def _fit_to_hex(lightness: float, chroma: float, hue: float) -> str:
    color = Color("oklch", [lightness, chroma, hue]).fit("srgb").convert("srgb")
    return color.to_string(hex=True)


def _grey_hex(lightness: float) -> str:
    """A truly achromatic hex (r == g == b) at roughly the given oklch L.

    Fitting a chroma-0 oklch colour to hex can round the three channels
    apart, leaving a faint chroma; forcing one channel value keeps the drawn
    background exactly achromatic (oklch hue NaN), which is what the
    achromatic-affinity properties need.
    """
    value = round(Color("oklch", [lightness, 0.0, 0.0]).convert("srgb")["red"] * 255)
    value = min(255, max(0, value))
    return f"#{value:02x}{value:02x}{value:02x}"


def grey_hex(*, lightness: tuple[float, float]) -> st.SearchStrategy[str]:
    """An exactly-achromatic sRGB hex drawn by oklch lightness."""
    return st.builds(
        _grey_hex, st.floats(min_value=lightness[0], max_value=lightness[1])
    )


def oklch_hex(
    *,
    lightness: tuple[float, float],
    chroma: tuple[float, float] = (0.0, 0.25),
) -> st.SearchStrategy[str]:
    """A 6-digit sRGB hex drawn in oklch space."""
    return st.builds(
        _fit_to_hex,
        st.floats(min_value=lightness[0], max_value=lightness[1]),
        st.floats(min_value=chroma[0], max_value=chroma[1]),
        st.floats(min_value=0.0, max_value=360.0, exclude_max=True),
    )


class HarmonicCase(NamedTuple):
    params: ThemeParams
    direction: float
    appearance: AppearanceContent
    make_generator: Callable[[ThemeParams], ThemeGenerator]


@st.composite
def _harmonic_case(draw: st.DrawFn, *, direction: float) -> HarmonicCase:
    dark = direction > 0
    background = draw(
        oklch_hex(
            # Bounded away from mid-lightness so every drawn contrast floor is
            # achievable: L<=0.25 keeps >=15:1 headroom to white, L>=0.90 the
            # mirror to black.
            lightness=(0.05, 0.25) if dark else (0.90, 0.985),
            chroma=(0.0, 0.08),
        )
    )
    foreground = draw(
        oklch_hex(
            lightness=(0.60, 0.98) if dark else (0.05, 0.40),
            chroma=(0.0, 0.12),
        )
    )
    accent = draw(oklch_hex(lightness=(0.40, 0.90), chroma=(0.02, 0.25)))
    params = ThemeParams.from_strings(
        name="prop",
        background=background,
        foreground=foreground,
        accent=accent,
        # Capped at 11: ladder rungs interpolate from the contrast floor up
        # to the foreground, so floors much above the 10.5 default collapse
        # the rung span (and the legal lightness band) until 13 pairwise-
        # separated text roles no longer fit.
        minimum_bg_contrast=draw(st.floats(min_value=7.0, max_value=11.0)),
        harmony_type=draw(st.sampled_from(sorted(HARMONY_TO_COLORAIDE))),
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


def harmonic_cases() -> st.SearchStrategy[HarmonicCase]:
    return st.one_of(
        _harmonic_case(direction=DARK_DIRECTION),
        _harmonic_case(direction=LIGHT_DIRECTION),
    )


# The original fixed fixtures, pinned via @example for deterministic coverage.
PINKISH = HarmonicCase(
    ThemeParams.from_strings(
        name="prop", background="#0a1022", foreground="#ffe3f3", accent="#ee7ec6"
    ),
    DARK_DIRECTION,
    AppearanceContent.dark,
    HarmonicPaletteThemeGenerator,
)
ROSEWATER = HarmonicCase(
    ThemeParams.from_strings(
        name="prop", background="#fdf4f8", foreground="#2b1930", accent="#c02579"
    ),
    LIGHT_DIRECTION,
    AppearanceContent.light,
    HarmonicLightPaletteThemeGenerator,
)


class RainbowCase(NamedTuple):
    params: RainbowParams
    colors: tuple[str, ...]
    background: str | None
    status_colors: tuple[str, ...] | None


@st.composite
def rainbow_cases(
    draw: st.DrawFn, *, explicit_background: bool | None = None
) -> RainbowCase:
    # Input lightness runs right up to the MAX_L clamp (0.985): bases with no
    # lightness headroom separate their repeats via whole-sRGB steps, and that
    # path is exercised on purpose. The lower bound stays clear of the 8-bit
    # near-black collapse (hexes below oklch L~0.067 all quantise to black),
    # which would pin light-direction walks on the black gamut corner.
    colors = tuple(
        draw(
            st.lists(
                oklch_hex(lightness=(0.10, 0.985), chroma=(0.0, 0.30)),
                min_size=2,
                max_size=8,
            )
        )
    )
    background: str | None = None
    if draw(st.booleans()) if explicit_background is None else explicit_background:
        # Explicit backgrounds are used verbatim, so they must leave WCAG
        # headroom for the derived roles: a mid-lightness background cannot
        # host the contrast floors and collapses them to the gamut edge.
        side = draw(st.sampled_from([(0.05, 0.25), (0.90, 0.95)]))
        background = draw(oklch_hex(lightness=side, chroma=(0.0, 0.08)))
    status_colors: tuple[str, ...] | None = None
    if draw(st.booleans()):
        status_colors = tuple(
            draw(
                st.lists(
                    oklch_hex(lightness=(0.10, 0.92), chroma=(0.0, 0.30)),
                    min_size=4,
                    max_size=4,
                )
            )
        )
    params = RainbowParams.from_strings(
        name="prop",
        colors=colors,
        background=background,
        status_colors=status_colors,
    )
    return RainbowCase(params, colors, background, status_colors)


NEON = ("#ff004c", "#ffe600", "#00ffd5", "#b700ff", "#ff8c00")
MURK = ("#1a0033", "#003322", "#330011")

NEON_CASE = RainbowCase(
    RainbowParams.from_strings(name="prop", colors=NEON), NEON, None, None
)
MURK_CASE = RainbowCase(
    RainbowParams.from_strings(name="prop", colors=MURK), MURK, None, None
)


def dump_style(style: ThemeStyleContent) -> dict[str, object]:
    return style.model_dump(mode="json", by_alias=True, exclude_none=True)


def assert_achromatic_hex(color: Color, *, role: str = "") -> None:
    """The shipped hex is grey: channels within one 8-bit rounding step.

    Fitting an exactly chroma-0 oklch colour to sRGB can round adjacent
    channels apart by a single step, so a spread of 1 is still `grey`.
    """
    rendered = hex_rgba(color)
    channels = [int(rendered[i : i + 2], 16) for i in (1, 3, 5)]
    assert max(channels) - min(channels) <= 1, (role, rendered)


def assert_cursor_visible(style_json: dict[str, object]) -> None:
    """All eight players share one caret that reads against the background.

    Pinned behaviour: every player entry is identical; the caret doubles as
    the player background; the selection is the caret's RGB with a 0x47 alpha
    byte (semi-transparent, so selected text still reads); and the caret —
    the background's sRGB inversion, lightness-snapped when the inversion is
    muddy — clears 4.5:1 WCAG against the background it blinks on. The caret
    is deliberately NOT required to contrast with the text colours: inverting
    the background lands it on the text's side of the background by design.
    """
    background = Color(str(style_json["background"]))
    players = style_json["players"]
    assert isinstance(players, list)
    assert len(players) == 8
    assert all(player == players[0] for player in players)
    player = players[0]
    assert isinstance(player, dict)
    cursor = str(player["cursor"])
    assert player["background"] == cursor
    assert str(player["selection"]) == f"{cursor[:7]}47"
    assert cursor != str(style_json["background"])
    assert Color(cursor).contrast(background) >= 4.5


def assert_valid_colors(style_json: dict[str, object]) -> None:
    """Every colour value in the rendered style is a lowercase #rrggbbaa string."""
    for key, value in style_json.items():
        if key == "background.appearance":
            continue
        if key == "accents":
            assert isinstance(value, list)
            for entry in value:
                assert isinstance(entry, str) and HEX_RGBA.match(entry), key
        elif key == "players":
            assert isinstance(value, list)
            for player in value:
                assert isinstance(player, dict)
                for colour in player.values():
                    assert isinstance(colour, str) and HEX_RGBA.match(colour), key
        elif key == "syntax":
            assert isinstance(value, dict)
            for token, entry in value.items():
                assert isinstance(entry, dict)
                colour = entry["color"]
                assert isinstance(colour, str) and HEX_RGBA.match(colour), token
        else:
            assert isinstance(value, str) and HEX_RGBA.match(value), f"{key}: {value!r}"
