"""Generate and register colourful, high-contrast Zed themes.

Colour work happens in oklch (perceptually uniform lightness/chroma/hue) via
coloraide: harmonies seed the hue spread, WCAG 2.1 contrast floors keep every
foreground readable against the background, and everything is gamut-mapped back
into sRGB before serialisation.
"""

import json
import pathlib
import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from itertools import combinations
from typing import ClassVar, Literal, Self, TypedDict, cast, override

from coloraide import Color
from cyclopts import App

from zed_theme_generator.gen.zed_theme import (
    AppearanceContent,
    FontStyleContent,
    FontWeight,
    HighlightStyleContent,
    PlayerColorContent,
    ThemeContent,
    ThemeFamilyContent,
    ThemeStyleContent,
    WindowBackgroundContent,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
THEMES_DIR = REPO_ROOT / "themes"
ZED_THEMES_DIR = pathlib.Path.home() / ".config" / "zed" / "themes"
AUTHOR = "cd"
SCHEMA_URL = "https://zed.dev/schema/themes/v0.2.0.json"
TRANSPARENT = "#00000000"
EXTENSION_ID = "clod-themes"
EXTENSION_REPOSITORY = "https://github.com/CharlieADavies/zed-theme-generator"

# --- structural constants (fixed semantics, not tuning knobs) -----------------

MAX_L = 0.985
MIN_L = 0.015  # light-mode ramps stop short of pure black, mirroring MAX_L
CONTRAST_STEP = 0.01
DARK_DIRECTION = 1.0  # raise lightness away from dark backgrounds
LIGHT_DIRECTION = -1.0  # lower lightness away from light backgrounds

STATUS_ANCHOR_TOLERANCE = 15.0  # status hues must stay recognisable (red error etc.)
FALLBACK_ACCENT_HUE = 343.0  # pink, for achromatic accent inputs

# oklch hue anchors for status colours with fixed semantics (red error etc.)
HUE_RED = 25.0
HUE_YELLOW = 95.0
HUE_GREEN = 140.0
HUE_BLUE = 225.0

# Harmony-family ladders: each syntax role lives on (wheel family x rung).
WHEEL_COUNT = 12
LADDER_RUNGS = 4

# Chrome leans from the background hue towards the accent so the whole UI
# carries the accent's cast. The lean is capped in degrees: a
# near-complementary bg/accent pair must tint, not drag chrome through the
# muddy brown/olive midpoint of the wheel.
TINT_ARC_MAX = 80.0

type HarmonyType = Literal[
    "monochromatic",
    "complementary",
    "split",
    "analogous",
    "triadic",
    "square",
    "rectangular",
    "wheel",
]

HARMONY_TO_COLORAIDE: dict[HarmonyType, str] = {
    "monochromatic": "mono",
    "complementary": "complement",
    "split": "split",
    "analogous": "analogous",
    "triadic": "triad",
    "square": "square",
    "rectangular": "rectangle",
    "wheel": "wheel",
}

# Which wheel family (index on the WHEEL_COUNT-step wheel from the accent) each
# movable syntax role draws from, per harmony hint. Family 0 is the accent
# itself (keyword/operator/punctuation). Rung>=1 roles (title) must sit >=2
# families from 0: ladder chroma fades up-rung, so hue-only separation weakens.
FAMILY_MAPS: dict[str, dict[str, int]] = {
    "wheel": {"number": 2, "type": 7, "function": 8, "title": 10, "property": 11},
    "complement": {"number": 6, "type": 7, "function": 5, "title": 10, "property": 1},
    "split": {"number": 5, "type": 7, "function": 6, "title": 10, "property": 1},
    "analogous": {"number": 1, "type": 2, "function": 11, "title": 10, "property": 3},
    "triad": {"number": 4, "type": 3, "function": 8, "title": 9, "property": 11},
    "square": {"number": 3, "type": 6, "function": 9, "title": 10, "property": 11},
    "rectangle": {"number": 2, "type": 6, "function": 8, "title": 10, "property": 4},
    "mono": {"number": 2, "type": 7, "function": 8, "title": 10, "property": 11},
}


# --- generation parameters ----------------------------------------------------


@dataclass(frozen=True)
class ThemeParams:
    """Everything a theme generation depends on.

    The first block mirrors the CLI; the master knobs below it stay on their
    defaults unless constructed directly. Every derived value the pipeline
    reads is a property computed from these few masters, so retuning the theme
    means moving one number, not eight.
    """

    name: str
    background: Color
    foreground: Color
    accent: Color
    # Minimum WCAG contrast between the background and primary text (editor
    # foreground, UI text); every other text floor derives from it. A floor,
    # not a target: brightness beyond it comes from the input foreground.
    minimum_bg_contrast: float = 10.5
    # Target mean pairwise OKLab distance between the syntax token colours;
    # family chroma is calibrated until the tokens hit it.
    target_color_distance: float = 0.19
    # A coloraide harmony used as a hint for hue selection, topped up with wheel
    # hues whenever it yields too few distinct ones.
    harmony: str = "wheel"
    # Pinkness knobs: accent hue/chroma mix into UI text (lightness stays at
    # the editor foreground's), and how far chrome surfaces and borders lean
    # from the background hue towards the accent.
    ui_accent_mix: float = 0.55
    surface_tint: float = 0.3
    border_tint: float = 0.5
    # master knobs — not exposed via the CLI
    chroma: float = 0.14  # base syntax chroma; every chroma cap derives
    chrome_lift: float = 0.04  # one elevation step of chrome lightness (oklch L)
    min_text_delta: float = 0.05  # pairwise OKLab delta E floor between text roles

    @classmethod
    def from_strings(
        cls,
        *,
        name: str,
        background: str,
        foreground: str,
        accent: str,
        minimum_bg_contrast: float = 10.5,
        target_color_distance: float = 0.19,
        harmony_type: HarmonyType = "wheel",
        ui_accent_mix: float = 0.55,
        surface_tint: float = 0.3,
        border_tint: float = 0.5,
    ) -> Self:
        """Resolve raw CLI strings into generation parameters."""
        return cls(
            name=name,
            background=Color(background),
            foreground=Color(foreground),
            accent=Color(accent),
            minimum_bg_contrast=minimum_bg_contrast,
            target_color_distance=target_color_distance,
            harmony=HARMONY_TO_COLORAIDE[harmony_type],
            ui_accent_mix=ui_accent_mix,
            surface_tint=surface_tint,
            border_tint=border_tint,
        )

    # WCAG 2.1 contrast floors against the background. floor_syntax is
    # generative: it *selects* each band colour's lightness per hue and
    # background, so different inputs are forced to different lightnesses,
    # not clamped upward from a shared band.
    @property
    def floor_primary(self) -> float:
        return self.minimum_bg_contrast

    @property
    def floor_syntax(self) -> float:
        return 0.90 * self.minimum_bg_contrast

    @property
    def floor_muted(self) -> float:
        return 0.62 * self.minimum_bg_contrast

    @property
    def floor_subtle(self) -> float:
        return 0.48 * self.minimum_bg_contrast

    @property
    def floor_line_number(self) -> float:
        return 0.43 * self.minimum_bg_contrast

    # chroma caps, all fractions of the one base chroma
    @property
    def syntax_chroma(self) -> float:
        return self.chroma

    @property
    def accent_chroma_cap(self) -> float:
        return 1.25 * self.chroma

    @property
    def comment_chroma_cap(self) -> float:
        return self.chroma / 2

    @property
    def bg_chroma_cap(self) -> float:
        return 0.4 * self.chroma

    @property
    def border_chroma(self) -> float:
        return self.chroma / 3

    @property
    def line_number_chroma(self) -> float:
        return self.chroma / 4

    @property
    def hint_chroma(self) -> float:
        return 0.4 * self.chroma

    @property
    def border_focused_chroma(self) -> float:
        return 0.85 * self.chroma

    @property
    def border_selected_chroma(self) -> float:
        return 0.7 * self.chroma

    # background-relative lightness offsets for chrome surfaces, in half-steps
    # of the one chrome_lift unit
    @property
    def element_disabled_delta(self) -> float:
        return 0.5 * self.chrome_lift

    @property
    def surface_delta(self) -> float:
        return 0.75 * self.chrome_lift

    @property
    def element_delta(self) -> float:
        return self.chrome_lift

    @property
    def border_disabled_delta(self) -> float:
        return 1.5 * self.chrome_lift

    @property
    def hover_delta(self) -> float:
        return 2.0 * self.chrome_lift

    @property
    def border_variant_delta(self) -> float:
        return 2.5 * self.chrome_lift

    @property
    def active_delta(self) -> float:
        return 3.0 * self.chrome_lift

    @property
    def border_delta(self) -> float:
        return 4.0 * self.chrome_lift

    # The variant knobs perturb syntax too (user choice): surface_tint scales
    # the hue-cast towards the bg, ui_accent_mix seeds the family chroma base
    # (before the target_color_distance calibration) — so variants sharing
    # bg+accent still diverge.
    @property
    def syntax_cast(self) -> float:
        return self.surface_tint / 2

    @property
    def syntax_chroma_scale(self) -> float:
        return 0.5 + self.ui_accent_mix

    @property
    def line_number_tint(self) -> float:
        return min(1.0, 1.2 * self.border_tint)


class Palette(TypedDict):
    """Every colour role the theme is built from; all values are in oklch."""

    # anchors
    bg: Color
    fg_editor: Color
    text: Color  # UI text, pulled towards the accent
    accent: Color
    # chrome surfaces
    surface: Color
    element: Color
    element_hover: Color
    element_active: Color
    element_disabled: Color
    # borders
    border: Color
    border_variant: Color
    border_focused: Color
    border_selected: Color
    border_disabled: Color
    # text ramp
    text_muted: Color
    text_disabled: Color
    line_number: Color
    # status
    error: Color
    warning: Color
    success: Color
    info: Color
    hint: Color
    predictive: Color
    # syntax roles
    keyword: Color
    function: Color
    string: Color
    type: Color
    number: Color
    property: Color
    operator: Color
    comment: Color
    punctuation: Color
    title: Color
    emphasis_strong: Color
    # collections
    accents: list[Color]


# --- colour helpers ----------------------------------------------------------


def hex_rgba(color: Color, alpha: int | None = None) -> str:
    """Serialise a colour as the lowercase `#rrggbbaa` hex string Zed uses.

    `alpha` replaces the colour's own alpha with a 0-255 byte value.
    """
    c = color.convert("srgb")
    if alpha is not None:
        c["alpha"] = alpha / 255
    return c.to_string(hex=True, alpha=True).lower()


def hue_distance(a: float, b: float) -> float:
    """Circular distance between two hues in degrees (0-180)."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


def hue_towards(from_hue: float, to_hue: float, amount: float) -> float:
    """Lean `from_hue` towards `to_hue` by `amount` (0-1) along the shorter arc.

    The arc is capped at `TINT_ARC_MAX` degrees: leaning is a cast, and a hue
    should keep its family even when the target is nearly complementary.
    """
    delta = ((to_hue - from_hue + 180) % 360) - 180
    delta = max(-TINT_ARC_MAX, min(TINT_ARC_MAX, delta))
    return (from_hue + delta * amount) % 360


def shift_l(color: Color, delta: float) -> Color:
    """Return an oklch copy of `color` with its lightness shifted by `delta`."""
    c = color.convert("oklch")
    c["lightness"] = min(MAX_L, max(0.0, c["lightness"] + delta))
    return c.fit("srgb")


def ensure_contrast(
    color: Color, bg: Color, floor: float, *, direction: float = DARK_DIRECTION
) -> Color:
    """Return an oklch copy of `color` meeting `floor` WCAG contrast against `bg`.

    Walks lightness away from the background in `CONTRAST_STEP` increments;
    `direction` is +1 for dark backgrounds and -1 for light ones.
    """
    c = color.convert("oklch")
    for _ in range(100):
        if c.contrast(bg) >= floor:
            break
        lightness = c["lightness"] + direction * CONTRAST_STEP
        if not 0.0 <= lightness <= MAX_L:
            break
        c["lightness"] = lightness
        c.fit("srgb")
    return c


def floor_lightness(
    hue: float,
    chroma: float,
    bg: Color,
    floor: float,
    *,
    direction: float = DARK_DIRECTION,
) -> float:
    """Lowest oklch lightness at which (chroma, hue) clears `floor` WCAG vs `bg`.

    Probes with a fresh colour each step: repeatedly fitting one mutated colour
    cumulatively crushes its chroma, so every probe is rebuilt from the seed
    coordinates.
    """
    # Backgrounds lighter than MAX_L (e.g. #ffffff) must still start inside
    # the walkable range, or the loop exits before its first probe.
    lightness = min(MAX_L, max(0.0, bg["lightness"]))
    while 0.0 <= lightness <= MAX_L:
        probe = Color("oklch", [lightness, chroma, hue]).fit("srgb")
        if probe.contrast(bg) >= floor:
            return lightness
        lightness += direction * CONTRAST_STEP
    return MAX_L if direction > 0 else 0.0


def band(
    hue: float,
    chroma: float,
    bg: Color,
    *,
    floor: float,
    direction: float = DARK_DIRECTION,
) -> Color:
    """The bg-nearest colour at (chroma, hue) meeting `floor` — the floor selects L.

    Darkest on a dark background, lightest on a light one.
    """
    lightness = floor_lightness(hue, chroma, bg, floor, direction=direction)
    return Color("oklch", [lightness, chroma, hue]).fit("srgb")


def ladder(
    hue: float,
    chroma: float,
    *,
    bg: Color,
    top: Color,
    floor: float,
    rungs: int = LADDER_RUNGS,
    direction: float = DARK_DIRECTION,
) -> list[Color]:
    """Floor-anchored rungs interpolated from the band colour towards `top`.

    The top endpoint's hue is pinned to the family hue: interpolating towards
    `top`'s own hue flips direction at the antipode and drifts every high rung
    towards it.
    """
    base = band(hue, chroma, bg, floor=floor, direction=direction)
    capped = top.convert("oklch")
    capped["hue"] = hue
    return Color.steps([base, capped], steps=rungs, space="oklch")


def nearest_wheel_hue(
    wheel_hues: Sequence[float], anchor: float, tolerance: float
) -> float:
    """The wheel hue closest to `anchor`, or `anchor` itself when none is near."""
    best = min(wheel_hues, key=lambda h: hue_distance(h, anchor))
    return best if hue_distance(best, anchor) <= tolerance else anchor


def elevate(
    bg: Color,
    delta: float,
    *,
    from_hue: float,
    toward_hue: float,
    tint: float,
    chroma: float | None = None,
) -> Color:
    """Raise `bg` by `delta` lightness, leaning its hue towards `toward_hue`."""
    c = bg.clone()
    c["lightness"] = c["lightness"] + delta
    c["hue"] = hue_towards(from_hue, toward_hue, tint)
    if chroma is not None:
        c["chroma"] = chroma
    return c.fit("srgb")


def highlight(
    color: Color, *, italic: bool = False, weight: int | None = None
) -> HighlightStyleContent:
    """A syntax highlight entry for `color`."""
    return HighlightStyleContent(
        color=hex_rgba(color),
        font_style=FontStyleContent.italic if italic else None,
        font_weight=FontWeight(weight) if weight is not None else None,
    )


# --- text-role separation ------------------------------------------------------


def _ramp_candidates(
    hue: float,
    chroma: float,
    bg: Color,
    floor: float,
    seed_l: float,
    prefer_up: bool,
    direction: float = DARK_DIRECTION,
) -> list[Color]:
    """Colours sampled along the legal lightness ramp at (chroma, hue).

    The ramp interpolates the lightness band clearing `floor` — floor to
    `MAX_L` on dark backgrounds, `MIN_L` to floor on light ones — so every
    candidate satisfies WCAG by construction (contrast is monotonic in
    lightness on the far side of the floor). Candidates come back
    nearest-to-`seed_l` first, ties broken towards the preferred direction.
    """
    floor_l = floor_lightness(hue, chroma, bg, floor, direction=direction)
    lo, hi = (floor_l, MAX_L) if direction > 0 else (MIN_L, floor_l)
    ramp = Color.interpolate(
        [Color("oklch", [lo, chroma, hue]), Color("oklch", [hi, chroma, hue])],
        space="oklch",
    )
    span = hi - lo
    seed_t = 0.0 if span <= 0 else min(1.0, max(0.0, (seed_l - lo) / span))
    steps = [i / 40 for i in range(41)]

    def order(t: float) -> tuple[float, int]:
        preferred = (t >= seed_t) == prefer_up
        return (abs(t - seed_t), 0 if preferred else 1)

    return [ramp(t).fit("srgb") for t in sorted(steps, key=order)]


def separate_text_roles(
    roles: Mapping[str, Color],
    floors: Mapping[str, float],
    bg: Color,
    *,
    min_delta: float,
    default_floor: float,
    direction: float = DARK_DIRECTION,
) -> dict[str, Color]:
    """Place text colours so every pair is >= `min_delta` apart in OKLab.

    Greedy and deterministic: roles are placed in mapping (seniority) order,
    each checked only against already-placed seniors, which guarantees the
    pairwise invariant in a single pass. A conflicting colour slides along its
    legal lightness ramp — preferred direction from index parity (evens climb,
    odds sink) — then fans its hue away from the nearest conflicting senior,
    and as a last resort takes the sampled candidate furthest from all seniors.
    Every candidate keeps the role's WCAG floor by construction.
    """
    placed: dict[str, Color] = {}
    for index, (name, seed) in enumerate(roles.items()):
        placed[name] = _place_role(
            seed.convert("oklch"),
            placed.values(),
            bg,
            floor=floors.get(name, default_floor),
            min_delta=min_delta,
            prefer_up=index % 2 == 0,
            direction=direction,
        )
    return placed


def _place_role(
    seed: Color,
    seniors: Iterable[Color],
    bg: Color,
    *,
    floor: float,
    min_delta: float,
    prefer_up: bool,
    direction: float = DARK_DIRECTION,
) -> Color:
    placed = list(seniors)

    def clearance(c: Color) -> float:
        return min((c.delta_e(s, method="ok") for s in placed), default=float("inf"))

    if clearance(seed) >= min_delta:
        return seed

    nearest = min(placed, key=lambda s: seed.delta_e(s, method="ok"))
    away_arc = ((seed["hue"] - nearest.convert("oklch")["hue"] + 180) % 360) - 180
    away = 1.0 if away_arc >= 0 else -1.0
    # Fan away from the nearest senior first; mirror the arc, then widen it,
    # only once the preferred side is exhausted — a low-chroma role needs a
    # wider hue swing for the same OKLab distance.
    near_steps = (6.0, 12.0, 18.0, 24.0, 30.0, 36.0)
    far_steps = (48.0, 60.0, 72.0)
    hue_offsets = (
        [0.0]
        + [away * step for step in near_steps]
        + [-away * step for step in near_steps]
        + [away * step for step in far_steps]
        + [-away * step for step in far_steps]
    )

    best: Color | None = None
    best_clearance = -1.0
    # Chroma escalates only after the seed-chroma search is exhausted: hue and
    # lightness cannot separate a near-achromatic role from an achromatic
    # senior in a crowded band, but a small chroma lift always can.
    for chroma_boost in (0.0, 0.02, 0.04):
        chroma = seed["chroma"] + chroma_boost
        for offset in hue_offsets:
            hue = (seed["hue"] + offset) % 360
            for candidate in _ramp_candidates(
                hue, chroma, bg, floor, seed["lightness"], prefer_up, direction
            ):
                score = clearance(candidate)
                if score >= min_delta:
                    return candidate
                if score > best_clearance:
                    best, best_clearance = candidate, score
    assert best is not None
    return best


# --- palette selection ---------------------------------------------------------


def select_colors(
    params: ThemeParams, *, direction: float = DARK_DIRECTION
) -> Palette:
    """Fill every palette role: harmony-seeded hues, contrast-floored lightness.

    `direction` is `DARK_DIRECTION` for dark backgrounds and `LIGHT_DIRECTION`
    for light ones; every floor walk and chrome elevation follows it.
    """
    bg = params.background.convert("oklch")
    if direction > 0 and bg["lightness"] >= 0.5:
        raise ValueError(
            "Dark generation needs a dark background "
            f"(oklch lightness {bg['lightness']:.2f} >= 0.5)"
        )
    if direction < 0 and bg["lightness"] < 0.5:
        raise ValueError(
            "Light generation needs a light background "
            f"(oklch lightness {bg['lightness']:.2f} < 0.5)"
        )
    bg["chroma"] = min(bg["chroma"], params.bg_chroma_cap)
    bg.fit("srgb")
    bg_hue = FALLBACK_ACCENT_HUE if bg.is_nan("hue") else bg["hue"]

    accent = params.accent.convert("oklch")
    if accent.is_nan("hue"):
        accent["hue"] = FALLBACK_ACCENT_HUE
    accent_chroma = min(
        max(accent["chroma"], params.syntax_chroma), params.accent_chroma_cap
    )
    accent = band(
        accent["hue"], accent_chroma, bg, floor=params.floor_syntax, direction=direction
    )
    accent_hue = accent["hue"]

    # The input foreground keeps its own lightness; minimum_bg_contrast is a
    # floor, not a target, so brightness beyond it comes from the input colour.
    fg_editor = params.foreground.convert("oklch")
    if direction > 0:
        fg_editor["lightness"] = min(MAX_L, fg_editor["lightness"])
    else:
        fg_editor["lightness"] = max(MIN_L, fg_editor["lightness"])
    fg_editor.fit("srgb")
    fg_editor = ensure_contrast(fg_editor, bg, params.floor_primary, direction=direction)

    # The accent's lightness is masked out of the mix: UI text takes the
    # accent's hue and chroma but keeps the editor foreground's lightness,
    # so UI text stays as bright as the editor instead of being dragged
    # down by the floor-anchored (dimmer) accent.
    text = fg_editor.mix(
        accent.mask("lightness"), params.ui_accent_mix, space="oklch"
    )
    text = ensure_contrast(text, bg, params.floor_primary, direction=direction)

    # Harmony families: full wheel colours from the accent carry its chroma
    # into every family.
    wheel = accent.harmony("wheel", space="oklch", count=WHEEL_COUNT)
    wheel_hues = [w["hue"] % 360 for w in wheel]

    def family(index: int, rung: int, multiplier: float = 1.0) -> Color:
        hue = hue_towards(wheel_hues[index], bg_hue, params.syntax_cast)
        chroma = (
            min(
                wheel[index]["chroma"] * params.syntax_chroma_scale,
                params.accent_chroma_cap,
            )
            * multiplier
        )
        return ladder(
            hue,
            chroma,
            bg=bg,
            top=fg_editor,
            floor=params.floor_syntax,
            direction=direction,
        )[rung]

    def status_band(anchor: float) -> Color:
        # Status colours: hue-anchored tightly so semantics stay legible.
        hue = nearest_wheel_hue(wheel_hues, anchor, STATUS_ANCHOR_TOLERANCE)
        return band(
            hue, params.syntax_chroma, bg, floor=params.floor_syntax, direction=direction
        )

    error = status_band(HUE_RED)
    warning = status_band(HUE_YELLOW)
    success = status_band(HUE_GREEN)
    info = status_band(HUE_BLUE)

    # Syntax roles on the (family x rung) grid; family 0 is the accent.
    families = FAMILY_MAPS[params.harmony]

    def tokens(multiplier: float) -> dict[str, Color]:
        """The six hue-bearing token roles at a given chroma multiplier.

        Strings arise from the harmony like every other role: of the wheel
        families no other role draws from, the one whose colour sits furthest
        (OKLab) from the keyword colour.
        """
        keyword = family(0, 0, multiplier)
        string_family = max(
            (i for i in range(WHEEL_COUNT) if i not in {0, *families.values()}),
            key=lambda i: family(i, 0, multiplier).delta_e(keyword, method="ok"),
        )
        return {
            "keyword": keyword,
            "string": family(string_family, 0, multiplier),
            "function": family(families["function"], 0, multiplier),
            "type": family(families["type"], 0, multiplier),
            "number": family(families["number"], 0, multiplier),
            "property": family(families["property"], 0, multiplier),
        }

    def mean_token_distance(cols: dict[str, Color]) -> float:
        pairs = list(combinations(cols.values(), 2))
        return sum(a.delta_e(b, method="ok") for a, b in pairs) / len(pairs)

    # Calibrate family chroma until the tokens' mean pairwise OKLab distance
    # hits target_color_distance. Distance grows near-linearly with chroma, so
    # two proportional corrections converge; gamut fitting is the ceiling.
    multiplier = 1.0
    toks = tokens(multiplier)
    for _ in range(2):
        measured = mean_token_distance(toks)
        if measured <= 0:
            break
        multiplier = min(
            4.0, max(0.25, multiplier * params.target_color_distance / measured)
        )
        toks = tokens(multiplier)

    # Comment: a mono-harmony shade of the accent, muted and bg-cast.
    mono = accent.harmony("mono", space="oklch")
    comment = mono[2].clone()
    comment["chroma"] = min(comment["chroma"], params.comment_chroma_cap)
    comment["hue"] = hue_towards(comment["hue"], bg_hue, params.syntax_cast)
    # On a light background the floor lightness is a ceiling, not a floor.
    comment_floor_l = floor_lightness(
        comment["hue"], comment["chroma"], bg, params.floor_muted, direction=direction
    )
    if direction > 0:
        comment["lightness"] = max(comment["lightness"], comment_floor_l)
    else:
        comment["lightness"] = min(comment["lightness"], comment_floor_l)
    comment.fit("srgb")

    # Every text element must be distinguishable from every other; dict order
    # is seniority — separation only ever moves the junior role of a pair.
    seeds: dict[str, Color] = {
        "fg_editor": fg_editor,
        **toks,
        "operator": family(0, 1, multiplier),
        "title": family(families["title"], 1, multiplier),
        "punctuation": family(0, 2, multiplier),
        "comment": comment,
        "hint": band(
            info["hue"],
            params.hint_chroma,
            bg,
            floor=params.floor_muted,
            direction=direction,
        ),
        "predictive": ensure_contrast(
            info.mix(bg, 0.35, space="oklch"), bg, params.floor_subtle,
            direction=direction,
        ),
    }
    role_floors = {
        "fg_editor": params.floor_primary,
        "comment": params.floor_muted,
        "hint": params.floor_subtle,
        "predictive": params.floor_subtle,
    }
    roles = separate_text_roles(
        seeds,
        role_floors,
        bg,
        min_delta=params.min_text_delta,
        default_floor=params.floor_syntax,
        direction=direction,
    )
    keyword = roles["keyword"]

    text_muted = ensure_contrast(
        shift_l(text, -0.20 * direction), bg, params.floor_muted, direction=direction
    )
    text_disabled = text.clone()
    text_disabled["lightness"] -= 0.32 * direction
    text_disabled["chroma"] /= 2
    text_disabled.fit("srgb")
    text_disabled = ensure_contrast(
        text_disabled, bg, params.floor_subtle, direction=direction
    )
    line_number = ensure_contrast(
        Color(
            "oklch",
            [
                0.5,
                params.line_number_chroma,
                hue_towards(bg_hue, accent_hue, params.line_number_tint),
            ],
        ).fit("srgb"),
        bg,
        params.floor_line_number,
        direction=direction,
    )

    def chrome(delta: float, *, tint: float, chroma: float | None = None) -> Color:
        # Chrome elevates away from the background: up in dark, down in light.
        return elevate(
            bg,
            direction * delta,
            from_hue=bg_hue,
            toward_hue=accent_hue,
            tint=tint,
            chroma=chroma,
        )

    return Palette(
        bg=bg,
        fg_editor=roles["fg_editor"],
        text=text,
        accent=accent,
        surface=chrome(params.surface_delta, tint=params.surface_tint),
        element=chrome(params.element_delta, tint=params.surface_tint),
        element_hover=chrome(params.hover_delta, tint=params.surface_tint),
        element_active=chrome(params.active_delta, tint=params.surface_tint),
        element_disabled=shift_l(bg, direction * params.element_disabled_delta),
        border=chrome(
            params.border_delta, tint=params.border_tint, chroma=params.border_chroma
        ),
        border_variant=chrome(
            params.border_variant_delta,
            tint=params.border_tint,
            chroma=params.border_chroma,
        ),
        border_focused=Color(
            "oklch", [0.55, params.border_focused_chroma, accent_hue]
        ).fit("srgb"),
        border_selected=Color(
            "oklch",
            [0.42 if direction > 0 else 0.68, params.border_selected_chroma, accent_hue],
        ).fit("srgb"),
        border_disabled=chrome(
            params.border_disabled_delta,
            tint=params.border_tint,
            chroma=params.border_chroma,
        ),
        text_muted=text_muted,
        text_disabled=text_disabled,
        line_number=line_number,
        error=error,
        warning=warning,
        success=success,
        info=info,
        hint=roles["hint"],
        predictive=roles["predictive"],
        keyword=keyword,
        function=roles["function"],
        string=roles["string"],
        type=roles["type"],
        number=roles["number"],
        property=roles["property"],
        operator=roles["operator"],
        comment=roles["comment"],
        punctuation=roles["punctuation"],
        title=roles["title"],
        emphasis_strong=roles["number"].clone(),  # weight-700 is its differentiator
        # Accents sample alternate wheel families so they span the wheel.
        accents=[keyword.clone()]
        + [family(i, 0, multiplier) for i in (2, 4, 6, 8, 10)],
    )


# --- style construction ---------------------------------------------------------


def build_style(
    palette: Palette, *, appearance: AppearanceContent = AppearanceContent.dark
) -> ThemeStyleContent:
    """Map a filled palette onto every key of the Zed theme schema.

    `appearance` flips the handful of lightness shifts (bright/dim terminal
    variants, doc comments) that must move away from the background, and the
    name-semantic terminal ANSI black/white slots.
    """
    direction = (
        DARK_DIRECTION if appearance is AppearanceContent.dark else LIGHT_DIRECTION
    )
    bg = palette["bg"]
    accent = palette["accent"]
    fg_editor = palette["fg_editor"]
    text = palette["text"]
    surface = palette["surface"]
    element = palette["element"]
    element_hover = palette["element_hover"]
    element_active = palette["element_active"]
    element_disabled = palette["element_disabled"]
    text_muted = palette["text_muted"]
    text_disabled = palette["text_disabled"]
    error = palette["error"]
    warning = palette["warning"]
    success = palette["success"]
    info = palette["info"]
    keyword = palette["keyword"]
    function = palette["function"]
    string = palette["string"]
    type_ = palette["type"]
    number = palette["number"]
    comment = palette["comment"]
    comment_doc = shift_l(comment, 0.06 * direction)

    def status(role: Color) -> tuple[str, str, str]:
        """(foreground, subtle background, border) for a status colour.

        Status borders sit most of the way back to the bg.
        """
        return (
            hex_rgba(role),
            hex_rgba(role, 0x26),
            hex_rgba(role.mix(bg, 0.65, space="oklab")),
        )

    def bright(c: Color) -> Color:
        b = c.clone()
        b["lightness"] += 0.06 * direction
        b["chroma"] += 0.01
        return b.fit("srgb")

    def dim(c: Color) -> Color:
        d = c.clone()
        d["lightness"] -= 0.18 * direction
        d["chroma"] = max(0.0, d["chroma"] - 0.03)
        return d.fit("srgb")

    # A low-chroma neutral one step back from the editor foreground; feeds the
    # terminal ANSI slot that shares the foreground's side of the bg (white on
    # dark, black on light).
    ansi_near_fg = fg_editor.clone()
    ansi_near_fg["lightness"] -= 0.10 * direction
    ansi_near_fg["chroma"] = min(ansi_near_fg["chroma"], 0.02)
    ansi_near_fg.fit("srgb")

    # ANSI black/white slots are name-semantic: black stays on the dark side
    # and white on the light side regardless of which one the bg occupies.
    if appearance is AppearanceContent.dark:
        ansi_black = element
        ansi_dim_black = surface
        ansi_white = ansi_near_fg
        ansi_bright_white = fg_editor
        ansi_dim_white = text_muted
    else:
        ansi_black = ansi_near_fg
        ansi_dim_black = text_muted
        ansi_white = element
        ansi_bright_white = surface
        ansi_dim_white = element_hover
    ansi_bright_black = text_disabled

    error_hex, error_bg_hex, error_border_hex = status(error)
    warning_hex, warning_bg_hex, warning_border_hex = status(warning)
    success_hex, success_bg_hex, success_border_hex = status(success)
    info_hex, info_bg_hex, info_border_hex = status(info)
    hint_hex, hint_bg_hex, hint_border_hex = status(palette["hint"])
    predictive_hex, predictive_bg_hex, predictive_border_hex = status(
        palette["predictive"]
    )
    disabled_hex, disabled_bg_hex, disabled_border_hex = status(text_disabled)
    muted_hex, muted_bg_hex, muted_border_hex = status(text_muted)

    syntax: dict[str, HighlightStyleContent] = {
        "attribute": highlight(function),
        "boolean": highlight(number),
        "comment": highlight(comment),
        "comment.doc": highlight(comment_doc),
        "constant": highlight(number),
        "constructor": highlight(function),
        "embedded": highlight(fg_editor),
        "emphasis": highlight(accent, italic=True),
        "emphasis.strong": highlight(palette["emphasis_strong"], weight=700),
        "enum": highlight(palette["property"]),
        "function": highlight(function),
        "hint": highlight(palette["hint"]),
        "keyword": highlight(keyword),
        "label": highlight(function),
        "link_text": highlight(function),
        "link_uri": highlight(type_),
        "namespace": highlight(fg_editor),
        "number": highlight(number),
        "operator": highlight(palette["operator"]),
        "predictive": highlight(palette["predictive"], italic=True),
        "preproc": highlight(fg_editor),
        "primary": highlight(fg_editor),
        "property": highlight(palette["property"]),
        "punctuation": highlight(palette["punctuation"]),
        "punctuation.bracket": highlight(palette["punctuation"]),
        "punctuation.delimiter": highlight(palette["punctuation"]),
        "punctuation.list_marker": highlight(palette["property"]),
        "punctuation.markup": highlight(palette["property"]),
        "punctuation.special": highlight(palette["emphasis_strong"]),
        "selector": highlight(number),
        "selector.pseudo": highlight(function),
        "string": highlight(string),
        "string.escape": highlight(comment_doc),
        "string.regex": highlight(number),
        "string.special": highlight(number),
        "string.special.symbol": highlight(number),
        "tag": highlight(function),
        "text.literal": highlight(string),
        "title": highlight(palette["title"], weight=600),
        "type": highlight(type_),
        "variable": highlight(fg_editor),
        "variable.special": highlight(number),
        "variant": highlight(function),
    }

    return ThemeStyleContent(
        accents=[hex_rgba(c) for c in palette["accents"]],
        background=hex_rgba(bg),
        background_appearance=WindowBackgroundContent.opaque,
        border=hex_rgba(palette["border"]),
        border_disabled=hex_rgba(palette["border_disabled"]),
        border_focused=hex_rgba(palette["border_focused"]),
        border_selected=hex_rgba(palette["border_selected"]),
        border_transparent=TRANSPARENT,
        border_variant=hex_rgba(palette["border_variant"]),
        conflict=warning_hex,
        conflict_background=warning_bg_hex,
        conflict_border=warning_border_hex,
        created=success_hex,
        created_background=success_bg_hex,
        created_border=success_border_hex,
        deleted=error_hex,
        deleted_background=error_bg_hex,
        deleted_border=error_border_hex,
        drop_target_background=hex_rgba(accent, 0x40),
        editor_active_line_background=hex_rgba(element, 0xCC),
        editor_active_line_number=hex_rgba(accent),
        editor_active_wrap_guide=hex_rgba(accent, 0x38),
        editor_background=hex_rgba(bg),
        editor_document_highlight_bracket_background=hex_rgba(accent, 0x2E),
        editor_document_highlight_read_background=hex_rgba(accent, 0x1F),
        editor_document_highlight_write_background=hex_rgba(accent, 0x40),
        editor_foreground=hex_rgba(fg_editor),
        editor_gutter_background=hex_rgba(bg),
        editor_highlighted_line_background=hex_rgba(element_hover, 0xCC),
        editor_indent_guide=hex_rgba(palette["border_variant"]),
        editor_indent_guide_active=hex_rgba(palette["border"]),
        editor_invisible=hex_rgba(text_disabled),
        editor_line_number=hex_rgba(palette["line_number"]),
        editor_subheader_background=hex_rgba(surface),
        editor_wrap_guide=hex_rgba(accent, 0x1A),
        element_active=hex_rgba(element_active),
        element_background=hex_rgba(element),
        element_disabled=hex_rgba(element_disabled),
        element_hover=hex_rgba(element_hover),
        element_selected=hex_rgba(element_active),
        elevated_surface_background=hex_rgba(surface),
        error=error_hex,
        error_background=error_bg_hex,
        error_border=error_border_hex,
        ghost_element_active=hex_rgba(element_active),
        ghost_element_background=TRANSPARENT,
        ghost_element_disabled=hex_rgba(element_disabled),
        ghost_element_hover=hex_rgba(element_hover),
        ghost_element_selected=hex_rgba(element_active),
        hidden=disabled_hex,
        hidden_background=disabled_bg_hex,
        hidden_border=disabled_border_hex,
        hint=hint_hex,
        hint_background=hint_bg_hex,
        hint_border=hint_border_hex,
        icon=hex_rgba(text),
        icon_accent=hex_rgba(accent),
        icon_disabled=hex_rgba(text_disabled),
        icon_muted=hex_rgba(text_muted),
        icon_placeholder=hex_rgba(text_muted),
        ignored=disabled_hex,
        ignored_background=disabled_bg_hex,
        ignored_border=disabled_border_hex,
        info=info_hex,
        info_background=info_bg_hex,
        info_border=info_border_hex,
        link_text_hover=hex_rgba(accent),
        modified=warning_hex,
        modified_background=warning_bg_hex,
        modified_border=warning_border_hex,
        pane_focused_border=hex_rgba(palette["border_focused"]),
        pane_group_border=hex_rgba(palette["border_variant"]),
        panel_background=hex_rgba(surface),
        panel_focused_border=hex_rgba(palette["border_focused"]),
        panel_indent_guide=hex_rgba(palette["border_variant"]),
        panel_indent_guide_active=hex_rgba(palette["border"]),
        panel_indent_guide_hover=hex_rgba(palette["border"]),
        players=[
            PlayerColorContent(
                background="#000000ff",
                cursor="#000000ff",
                selection="#00000047",
            )
            for _ in range(8)
        ],
        predictive=predictive_hex,
        predictive_background=predictive_bg_hex,
        predictive_border=predictive_border_hex,
        renamed=info_hex,
        renamed_background=info_bg_hex,
        renamed_border=info_border_hex,
        scrollbar_thumb_background=hex_rgba(accent, 0x4D),
        scrollbar_thumb_border=hex_rgba(accent, 0x4D),
        scrollbar_thumb_hover_background=hex_rgba(accent, 0x66),
        scrollbar_track_background=TRANSPARENT,
        scrollbar_track_border=hex_rgba(surface),
        search_match_background=hex_rgba(warning, 0x47),
        status_bar_background=hex_rgba(bg),
        success=success_hex,
        success_background=success_bg_hex,
        success_border=success_border_hex,
        surface_background=hex_rgba(surface),
        syntax=syntax,
        tab_active_background=hex_rgba(bg),
        tab_inactive_background=hex_rgba(surface),
        tab_bar_background=hex_rgba(surface),
        terminal_ansi_background=hex_rgba(bg),
        terminal_ansi_black=hex_rgba(ansi_black),
        terminal_ansi_blue=hex_rgba(function),
        terminal_ansi_bright_black=hex_rgba(ansi_bright_black),
        terminal_ansi_bright_blue=hex_rgba(bright(function)),
        terminal_ansi_bright_cyan=hex_rgba(bright(type_)),
        terminal_ansi_bright_green=hex_rgba(bright(success)),
        terminal_ansi_bright_magenta=hex_rgba(bright(keyword)),
        terminal_ansi_bright_red=hex_rgba(bright(error)),
        terminal_ansi_bright_white=hex_rgba(ansi_bright_white),
        terminal_ansi_bright_yellow=hex_rgba(bright(warning)),
        terminal_ansi_cyan=hex_rgba(type_),
        terminal_ansi_dim_black=hex_rgba(ansi_dim_black),
        terminal_ansi_dim_blue=hex_rgba(dim(function)),
        terminal_ansi_dim_cyan=hex_rgba(dim(type_)),
        terminal_ansi_dim_green=hex_rgba(dim(success)),
        terminal_ansi_dim_magenta=hex_rgba(dim(keyword)),
        terminal_ansi_dim_red=hex_rgba(dim(error)),
        terminal_ansi_dim_white=hex_rgba(ansi_dim_white),
        terminal_ansi_dim_yellow=hex_rgba(dim(warning)),
        terminal_ansi_green=hex_rgba(success),
        terminal_ansi_magenta=hex_rgba(keyword),
        terminal_ansi_red=hex_rgba(error),
        terminal_ansi_white=hex_rgba(ansi_white),
        terminal_ansi_yellow=hex_rgba(warning),
        terminal_background=hex_rgba(bg),
        terminal_bright_foreground=hex_rgba(shift_l(fg_editor, 0.03 * direction)),
        terminal_dim_foreground=hex_rgba(text_muted),
        terminal_foreground=hex_rgba(fg_editor),
        text=hex_rgba(text),
        text_accent=hex_rgba(accent),
        text_disabled=hex_rgba(text_disabled),
        text_muted=hex_rgba(text_muted),
        text_placeholder=hex_rgba(text_disabled),
        title_bar_background=hex_rgba(bg),
        title_bar_inactive_background=hex_rgba(surface),
        toolbar_background=hex_rgba(bg),
        unreachable=muted_hex,
        unreachable_background=muted_bg_hex,
        unreachable_border=muted_border_hex,
        warning=warning_hex,
        warning_background=warning_bg_hex,
        warning_border=warning_border_hex,
    )


# --- serialisation ---------------------------------------------------------------


def theme_family_payload(
    style: ThemeStyleContent,
    *,
    name: str,
    appearance: AppearanceContent = AppearanceContent.dark,
) -> dict[str, object]:
    """Wrap a style in a single-variant theme family, schema pointer included."""
    missing = [
        field.alias or field_name
        for field_name, field in ThemeStyleContent.model_fields.items()
        if getattr(style, field_name) is None
    ]
    if missing:
        raise ValueError(f"Theme style is missing values for: {missing}")
    family = ThemeFamilyContent(
        author=AUTHOR,
        name=name,
        themes=[
            ThemeContent(
                appearance=appearance,
                name=f"{name}-{appearance.value}",
                style=style,
            )
        ],
    )
    return {
        "$schema": SCHEMA_URL,
        **family.model_dump(mode="json", by_alias=True, exclude_none=True),
    }


def render_theme_json(payload: dict[str, object], comments: Sequence[str] = ()) -> str:
    """Render a theme payload with leading `//` comment lines.

    Zed reads theme files with a lenient JSON parser (JSONC), so the comments
    carry generation provenance without breaking anything.
    """
    lines = [f"// {comment}" for comment in comments]
    lines.append(json.dumps(payload, indent=2))
    return "\n".join(lines) + "\n"


def params_comment(params: ThemeParams) -> str:
    """The generation inputs as a single-line `inputs: {...}` comment body."""
    payload: dict[str, object] = {}
    for f in fields(params):
        value = getattr(params, f.name)
        payload[f.name] = hex_rgba(value) if isinstance(value, Color) else value
    return "inputs: " + json.dumps(payload, separators=(",", ":"))


def palette_comment(palette: Palette) -> str:
    """The resolved palette as a single-line `palette: {...}` comment body."""
    payload: dict[str, object] = {}
    for role, value in palette.items():
        if isinstance(value, Color):
            payload[role] = hex_rgba(value)
        else:
            payload[role] = [hex_rgba(c) for c in cast("list[Color]", value)]
    return "palette: " + json.dumps(payload, separators=(",", ":"))


# --- generators --------------------------------------------------------------


class ThemeGenerator(ABC):
    """Base class for theme generators registered in `GENERATORS`.

    Subclasses supply the colour work through pure functions; this shell only
    names the generator and owns the file I/O.
    """

    generator_name: ClassVar[str]
    summary: ClassVar[str]

    @abstractmethod
    def build_theme(self) -> ThemeStyleContent:
        """Produce a fully-populated Zed theme style."""

    def comment_lines(self) -> list[str]:
        """Single-line provenance comments embedded at the top of the saved JSON."""
        return []

    def theme_appearance(self) -> AppearanceContent:
        """Which appearance the theme declares; an instance method because some
        generators compute their side from their inputs."""
        return AppearanceContent.dark

    def save_theme(
        self,
        style: ThemeStyleContent,
        *,
        name: str,
        directory: pathlib.Path | None = None,
    ) -> pathlib.Path:
        """Save the theme family JSON and refresh the extension.toml Zed reads."""
        directory = THEMES_DIR if directory is None else directory
        text = render_theme_json(
            theme_family_payload(style, name=name, appearance=self.theme_appearance()),
            self.comment_lines(),
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        path.write_text(text)
        if directory == THEMES_DIR:
            write_extension_toml()
        return path


class HarmonicPaletteThemeGenerator(ThemeGenerator):
    """Builds a theme from bg/fg/accent inputs via oklch harmonies."""

    generator_name: ClassVar[str] = "harmonic"
    summary: ClassVar[str] = (
        "Derives a full palette from background, foreground and accent colours "
        "using a coloraide harmony and WCAG contrast floors"
    )

    def __init__(self, params: ThemeParams) -> None:
        self.params = params

    @classmethod
    def from_cli(
        cls,
        *,
        name: str,
        background: str,
        foreground: str,
        accent: str,
        minimum_bg_contrast: float = 10.5,
        target_color_distance: float = 0.19,
        harmony_type: HarmonyType = "wheel",
        ui_accent_mix: float = 0.55,
        surface_tint: float = 0.3,
        border_tint: float = 0.5,
    ) -> Self:
        """Resolve raw CLI strings into generation parameters."""
        return cls(
            ThemeParams.from_strings(
                name=name,
                background=background,
                foreground=foreground,
                accent=accent,
                minimum_bg_contrast=minimum_bg_contrast,
                target_color_distance=target_color_distance,
                harmony_type=harmony_type,
                ui_accent_mix=ui_accent_mix,
                surface_tint=surface_tint,
                border_tint=border_tint,
            )
        )

    @override
    def build_theme(self) -> ThemeStyleContent:
        return build_style(select_colors(self.params))

    @override
    def comment_lines(self) -> list[str]:
        return [
            params_comment(self.params),
            palette_comment(select_colors(self.params)),
        ]


GENERATORS: dict[str, type[ThemeGenerator]] = {
    HarmonicPaletteThemeGenerator.generator_name: HarmonicPaletteThemeGenerator,
}


def write_extension_toml() -> None:
    """Regenerate the extension.toml Zed uses to learn about this theme extension."""
    theme_files = sorted(p.name for p in THEMES_DIR.glob("*.json"))
    themes_list = ", ".join(f'"themes/{f}"' for f in theme_files)
    content = "\n".join(
        [
            f'id = "{EXTENSION_ID}"',
            'name = "Clod Themes"',
            'version = "0.1.0"',
            "schema_version = 1",
            f'authors = ["{AUTHOR}"]',
            'description = "Themes generated by zed-theme-generator"',
            f'repository = "{EXTENSION_REPOSITORY}"',
            f"themes = [{themes_list}]",
        ]
    )
    (REPO_ROOT / "extension.toml").write_text(content + "\n")


# --- CLI ---------------------------------------------------------------------

app = App()


@app.command
def generate(
    name: str,
    background: str,
    foreground: str,
    accent: str,
    *,
    minimum_bg_contrast: float = 10.5,
    target_color_distance: float = 0.19,
    harmony_type: HarmonyType = "wheel",
    ui_accent_mix: float = 0.55,
    surface_tint: float = 0.3,
    border_tint: float = 0.5,
    register: bool = False,
    if_exists: Literal["overwrite", "raise"] = "overwrite",
) -> None:
    """Generate a Zed theme using a harmonic colour palette.

    Parameters
    ----------
    name
        Theme (and file) name; the dark variant appears in Zed as `<name>-dark`.
    background
        Background colour for both editor and UI (any CSS colour string).
    foreground
        Default editor text colour.
    accent
        Accent colour; tints UI text and seeds the hue harmony.
    minimum_bg_contrast
        Minimum WCAG contrast between the background and primary text; every
        other text floor (syntax, muted, subtle, line numbers) derives from it.
    target_color_distance
        Target mean pairwise OKLab distance between syntax token colours.
    harmony_type
        coloraide harmony used as the hue-selection hint.
    ui_accent_mix
        How far UI text is mixed towards the accent (0-1).
    surface_tint
        How far chrome surfaces lean from the background hue towards the accent (0-1).
    border_tint
        As surface_tint, for borders.
    register
        Also copy the generated theme into ~/.config/zed/themes.
    if_exists
        What to do when registering over an existing theme file.
    """
    generator = HarmonicPaletteThemeGenerator.from_cli(
        name=name,
        background=background,
        foreground=foreground,
        accent=accent,
        minimum_bg_contrast=minimum_bg_contrast,
        target_color_distance=target_color_distance,
        harmony_type=harmony_type,
        ui_accent_mix=ui_accent_mix,
        surface_tint=surface_tint,
        border_tint=border_tint,
    )
    style = generator.build_theme()
    path = generator.save_theme(style, name=name)
    print(f"Wrote {path}")
    if register:
        register_themes(name, if_exists)


@app.command
def register_themes(
    name: str, if_exists: Literal["overwrite", "raise"] = "raise"
) -> None:
    """Registers the theme in ~/.config/zed/themes"""
    source = THEMES_DIR / f"{name}.json"
    if not source.exists():
        raise FileNotFoundError(f"No generated theme at {source}; run generate first")
    ZED_THEMES_DIR.mkdir(parents=True, exist_ok=True)
    destination = ZED_THEMES_DIR / f"{name}.json"
    if destination.exists() and if_exists == "raise":
        raise FileExistsError(f"{destination} already exists (use overwrite)")
    shutil.copyfile(source, destination)
    print(f"Registered {destination}")


@app.command
def list_generators() -> None:
    """List all available theme generators"""
    for generator_name, generator in GENERATORS.items():
        print(f"{generator_name}: {generator.summary}")


# Generator modules imported for their side effects (GENERATORS entries and CLI
# commands). Deliberately last: they import back from this package, which only
# works once every name above is bound.
from zed_theme_generator import light as _light  # noqa: F401
from zed_theme_generator import rainbow as _rainbow  # noqa: F401


def main() -> None:
    app()
