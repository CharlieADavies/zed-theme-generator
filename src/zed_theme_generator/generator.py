"""Core engine: generation parameters, palette selection, and theme generators.

Colour work happens in oklch (perceptually uniform lightness/chroma/hue) via
coloraide: harmonies seed the hue spread, WCAG 2.1 contrast floors keep every
foreground readable against the background, and everything is gamut-mapped back
into sRGB before serialisation.
"""

import json
import pathlib
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from functools import cached_property
from itertools import combinations
from typing import Any, ClassVar, Literal, Self, cast, override

from coloraide import Color

from zed_theme_generator.gen.zed_theme import (
    AppearanceContent,
    ThemeStyleContent,
)
from zed_theme_generator.schemas import (
    AUTHOR,
    DARK_DIRECTION,
    MAX_L,
    MIN_L,
    HarmonicInputs,
    HarmonyType,
    Palette,
    build_style,
    hex_rgba,
    render_theme_json,
    shift_l,
    theme_family_payload,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
THEMES_DIR = REPO_ROOT / "themes"
PROFILES_DIR = REPO_ROOT / "profiles"
ZED_THEMES_DIR = pathlib.Path.home() / ".config" / "zed" / "themes"
EXTENSION_ID = "clod-themes"
EXTENSION_REPOSITORY = "https://github.com/CharlieADavies/zed-theme-generator"

# --- structural constants (fixed semantics, not tuning knobs) -----------------

CONTRAST_STEP = 0.01

STATUS_ANCHOR_TOLERANCE = 15.0  # status hues must stay recognisable (red error etc.)

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

# Calibration ceiling for the user-facing syntax_spread knob (0-100): 100 maps
# to this mean pairwise OKLab token distance, so 50 lands exactly on the 0.19
# historical default.
SYNTAX_SPREAD_MAX = 0.38

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
# Mono is the exception: every role stays within MONO_FAMILIES, so seeds
# collide on hue and the text-role separation pass differentiates by lightness.
FAMILY_MAPS: dict[str, dict[str, int]] = {
    "wheel": {"number": 2, "type": 7, "function": 8, "title": 10, "property": 11},
    "complement": {"number": 6, "type": 7, "function": 5, "title": 10, "property": 1},
    "split": {"number": 5, "type": 7, "function": 6, "title": 10, "property": 1},
    "analogous": {"number": 1, "type": 2, "function": 11, "title": 10, "property": 3},
    "triad": {"number": 4, "type": 3, "function": 8, "title": 9, "property": 11},
    "square": {"number": 3, "type": 6, "function": 9, "title": 10, "property": 11},
    "rectangle": {"number": 2, "type": 6, "function": 8, "title": 10, "property": 4},
    "mono": {"number": 0, "type": 11, "function": 1, "title": 11, "property": 1},
}

# Monochromatic themes seed within +/-30 degrees of the accent: the accent's
# own family and its immediate wheel neighbours. The separation fan may still
# swing a colliding token further out when nothing nearer clears
# `min_text_delta` — distinguishability outranks hue purity.
MONO_FAMILIES = frozenset({0, 1, WHEEL_COUNT - 1})

# --- generation parameters ----------------------------------------------------


@dataclass(frozen=True)
class ThemeParams:
    """Everything a theme generation depends on.

    Every field is in internal units (0-1 mix fractions, OKLab distances);
    `from_strings` is the boundary where the user-scale inputs (0-100 knobs,
    CSS colour strings) are converted. The master knobs at the bottom stay on
    their defaults unless constructed directly. Every derived value the
    pipeline reads is a property computed from these few masters, so retuning
    the theme means moving one number, not eight.
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
        syntax_spread: float = 50.0,
        harmony_type: HarmonyType = "wheel",
        accent_mix: float = 55.0,
        surface_blend: float = 30.0,
        border_blend: float = 50.0,
    ) -> Self:
        """Resolve user-scale inputs (CSS strings, 0-100 knobs) into parameters.

        The 0-100 knobs become 0-1 mix fractions, and syntax_spread maps onto
        the calibrated OKLab distance range [0, SYNTAX_SPREAD_MAX].
        """
        # Input alpha is dropped: the theme declares itself opaque, so only
        # the RGB channels of each input colour participate.
        return cls(
            name=name,
            background=Color(background).set("alpha", 1),
            foreground=Color(foreground).set("alpha", 1),
            accent=Color(accent).set("alpha", 1),
            minimum_bg_contrast=minimum_bg_contrast,
            target_color_distance=syntax_spread / 100 * SYNTAX_SPREAD_MAX,
            harmony=HARMONY_TO_COLORAIDE[harmony_type],
            ui_accent_mix=accent_mix / 100,
            surface_tint=surface_blend / 100,
            border_tint=border_blend / 100,
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


# --- colour helpers ----------------------------------------------------------


def hue_distance(a: float, b: float) -> float:
    """Circular distance between two hues in degrees (0-180)."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


def hue_towards(from_hue: float, to_hue: float, amount: float) -> float:
    """Lean `from_hue` towards `to_hue` by `amount` (0-1) along the shorter arc.

    The arc is capped at `TINT_ARC_MAX` degrees: leaning is a cast, and a hue
    should keep its family even when the target is nearly complementary.
    `amount` is clamped to [0, 1] so an out-of-range knob cannot defeat the cap.
    """
    delta = ((to_hue - from_hue + 180) % 360) - 180
    delta = max(-TINT_ARC_MAX, min(TINT_ARC_MAX, delta))
    return (from_hue + delta * min(1.0, max(0.0, amount))) % 360


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
        if not MIN_L <= lightness <= MAX_L:
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
    # Backgrounds outside the [MIN_L, MAX_L] clamp (e.g. #ffffff) must still
    # start inside the walkable range, or the loop exits before its first probe.
    lightness = min(MAX_L, max(MIN_L, bg["lightness"]))
    while MIN_L <= lightness <= MAX_L:
        probe = Color("oklch", [lightness, chroma, hue]).fit("srgb")
        if probe.contrast(bg) >= floor:
            return lightness
        lightness += direction * CONTRAST_STEP
    return MAX_L if direction > 0 else MIN_L


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
    # Fit every rung: interpolation leaves the gamut, and guarantees must be
    # measured on the colour that ships.
    return [
        c.fit("srgb") for c in Color.steps([base, capped], steps=rungs, space="oklch")
    ]


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
        # Seeds are fitted on entry: clearance and contrast are only
        # meaningful when measured on the colour that ships.
        placed[name] = _place_role(
            seed.convert("oklch").fit("srgb"),
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
    for offset in hue_offsets:
        hue = (seed["hue"] + offset) % 360
        for candidate in _ramp_candidates(
            hue, seed["chroma"], bg, floor, seed["lightness"], prefer_up, direction
        ):
            score = clearance(candidate)
            if score >= min_delta:
                return candidate
            if score > best_clearance:
                best, best_clearance = candidate, score
    assert best is not None
    return best


# --- palette selection ---------------------------------------------------------


def select_colors(params: ThemeParams, *, direction: float = DARK_DIRECTION) -> Palette:
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

    # Contrast floors are promises; an unreachable floor would silently
    # collapse every text ramp onto the gamut edge, so refuse it up front.
    achievable = Color("white" if direction > 0 else "black").contrast(bg)
    if params.floor_primary > achievable:
        raise ValueError(
            f"minimum_bg_contrast {params.floor_primary:.2f} is unreachable "
            f"against this background (maximum achievable {achievable:.2f})"
        )

    # Every hue derives from the inputs. An achromatic accent borrows the
    # background's hue; when the background is achromatic too there is no
    # input hue at all, so the theme goes fully achromatic: every derived
    # non-status colour keeps chroma 0 (only the semantically-anchored status
    # colours stay chromatic). The wheel math still needs numbers, so hues
    # live in plain floats — coloraide nulls the hue channel of any chroma-0
    # colour, and NaN would otherwise leak through the arithmetic.
    accent = params.accent.convert("oklch")
    achromatic = accent.is_nan("hue") and bg.is_nan("hue")
    if achromatic:
        accent_hue = 0.0  # never visible: every derived chroma is zero
        accent_chroma = 0.0
    else:
        accent_hue = (bg["hue"] if accent.is_nan("hue") else accent["hue"]) % 360
        accent_chroma = min(
            max(accent["chroma"], params.syntax_chroma), params.accent_chroma_cap
        )
    accent = band(
        accent_hue, accent_chroma, bg, floor=params.floor_syntax, direction=direction
    )
    if not achromatic:
        accent_hue = accent["hue"]
    # An achromatic background leans on the accent's hue: chrome and borders
    # must carry the accent's cast.
    bg_hue = accent_hue if bg.is_nan("hue") else bg["hue"]

    def derived_chroma(value: float) -> float:
        """The chroma a derived role ships with; achromatic themes zero it."""
        return 0.0 if achromatic else value

    # The input foreground keeps its own lightness; minimum_bg_contrast is a
    # floor, not a target, so brightness beyond it comes from the input colour.
    fg_editor = params.foreground.convert("oklch")
    if direction > 0:
        fg_editor["lightness"] = min(MAX_L, fg_editor["lightness"])
    else:
        fg_editor["lightness"] = max(MIN_L, fg_editor["lightness"])
    fg_editor.fit("srgb")
    fg_editor = ensure_contrast(
        fg_editor, bg, params.floor_primary, direction=direction
    )

    # The accent's lightness is masked out of the mix: UI text takes the
    # accent's hue and chroma but keeps the editor foreground's lightness,
    # so UI text stays as bright as the editor instead of being dragged
    # down by the floor-anchored (dimmer) accent.
    text = fg_editor.mix(accent.mask("lightness"), params.ui_accent_mix, space="oklch")
    text = ensure_contrast(text, bg, params.floor_primary, direction=direction)

    # Harmony families: full wheel colours from the accent carry its chroma
    # into every family. A grey accent's harmony nulls every hue, so the
    # achromatic wheel is laid out numerically (its chroma is zero anyway).
    wheel = accent.harmony("wheel", space="oklch", count=WHEEL_COUNT)
    if achromatic:
        wheel_hues = [
            (accent_hue + i * 360 / WHEEL_COUNT) % 360 for i in range(WHEEL_COUNT)
        ]
    else:
        wheel_hues = [w["hue"] % 360 for w in wheel]

    # Ladder rungs interpolate towards the foreground; in an achromatic theme
    # the top must not re-introduce the input foreground's chroma at the
    # (numerically laid out, meaningless) family hues.
    ladder_top = fg_editor.clone()
    ladder_top["chroma"] = derived_chroma(ladder_top["chroma"])

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
            top=ladder_top,
            floor=params.floor_syntax,
            direction=direction,
        )[rung]

    def status_band(anchor: float) -> Color:
        # Status colours: hue-anchored tightly so semantics stay legible. An
        # achromatic theme has no meaningful wheel, so the pure anchors hold —
        # a grayscale theme still reads red errors and blue info.
        hue = (
            anchor
            if achromatic
            else nearest_wheel_hue(wheel_hues, anchor, STATUS_ANCHOR_TOLERANCE)
        )
        return band(
            hue,
            params.syntax_chroma,
            bg,
            floor=params.floor_syntax,
            direction=direction,
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
        (OKLab) from the keyword colour. Mono never leaves its near families,
        so strings pick the farthest of the accent's neighbours instead.
        """
        keyword = family(0, 0, multiplier)
        string_candidates = (
            sorted(MONO_FAMILIES - {0})
            if params.harmony == "mono"
            else [i for i in range(WHEEL_COUNT) if i not in {0, *families.values()}]
        )
        string_family = max(
            string_candidates,
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

    # Comment: a mono-harmony shade of the accent, muted and bg-cast. A grey
    # accent's shades have no hue to cast (and NaN must not enter the hue
    # arithmetic), so the achromatic branch leaves the grey shade alone.
    mono = accent.harmony("mono", space="oklch")
    comment = mono[2].clone()
    comment["chroma"] = min(comment["chroma"], params.comment_chroma_cap)
    if not achromatic:
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

    predictive_seed = info.mix(bg, 0.35, space="oklch")
    predictive_seed["chroma"] = derived_chroma(predictive_seed["chroma"])

    # Every text element must be distinguishable from every other; dict order
    # is seniority — separation only ever moves the junior role of a pair.
    seeds: dict[str, Color] = {
        "fg_editor": fg_editor,
        **toks,
        "operator": family(0, 1, multiplier),
        "title": family(families["title"], 1, multiplier),
        "punctuation": family(0, 2, multiplier),
        "comment": comment,
        # Hint and predictive lean on the info hue but are not status
        # colours themselves: in an achromatic theme they ship grey.
        "hint": band(
            info["hue"],
            derived_chroma(params.hint_chroma),
            bg,
            floor=params.floor_muted,
            direction=direction,
        ),
        "predictive": ensure_contrast(
            predictive_seed,
            bg,
            params.floor_subtle,
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
                derived_chroma(params.line_number_chroma),
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
            params.border_delta,
            tint=params.border_tint,
            chroma=derived_chroma(params.border_chroma),
        ),
        border_variant=chrome(
            params.border_variant_delta,
            tint=params.border_tint,
            chroma=derived_chroma(params.border_chroma),
        ),
        border_focused=Color(
            "oklch", [0.55, derived_chroma(params.border_focused_chroma), accent_hue]
        ).fit("srgb"),
        border_selected=Color(
            "oklch",
            [
                0.42 if direction > 0 else 0.68,
                derived_chroma(params.border_selected_chroma),
                accent_hue,
            ],
        ).fit("srgb"),
        border_disabled=chrome(
            params.border_disabled_delta,
            tint=params.border_tint,
            chroma=derived_chroma(params.border_chroma),
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


# --- provenance comments ------------------------------------------------------


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


def existing_theme_error(path: pathlib.Path) -> FileExistsError:
    """The uniform refusal for writing over an existing theme file."""
    return FileExistsError(
        f"{path} already exists (pass --if-exists overwrite, "
        'add if_exists = "overwrite" to the profile, or pick a new name)'
    )


class ThemeGenerator(ABC):
    """Base class for theme generators registered in `GENERATORS`.

    Subclasses supply the colour work through pure functions; this shell only
    names the generator and owns the file I/O.
    """

    generator_name: ClassVar[str]
    summary: ClassVar[str]
    # The frozen dataclass describing this generator's inputs; the CLI
    # commands, wizard prompts, and profile TOML all read it.
    inputs_spec: ClassVar[type]

    @classmethod
    @abstractmethod
    def from_inputs(cls, inputs: Any) -> Self:
        """Build a generator from a validated `inputs_spec` instance."""

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
        if_exists: Literal["overwrite", "raise"] = "raise",
    ) -> pathlib.Path:
        """Save the theme family JSON and refresh the extension.toml Zed reads.

        An existing file is never clobbered unless `if_exists` is "overwrite".
        """
        directory = THEMES_DIR if directory is None else directory
        path = directory / f"{name}.json"
        if path.exists() and if_exists == "raise":
            raise existing_theme_error(path)
        text = render_theme_json(
            theme_family_payload(style, name=name, appearance=self.theme_appearance()),
            self.comment_lines(),
        )
        directory.mkdir(parents=True, exist_ok=True)
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
    inputs_spec: ClassVar[type] = HarmonicInputs

    def __init__(self, params: ThemeParams) -> None:
        self.params = params

    @classmethod
    @override
    def from_inputs(cls, inputs: HarmonicInputs) -> Self:
        """Build a generator from a validated inputs spec."""
        return cls(ThemeParams.from_strings(**asdict(inputs)))

    @cached_property
    def palette(self) -> Palette:
        """The resolved palette, computed once; every hook below reads it."""
        return select_colors(self.params)

    @override
    def build_theme(self) -> ThemeStyleContent:
        return build_style(self.palette)

    @override
    def comment_lines(self) -> list[str]:
        return [
            params_comment(self.params),
            palette_comment(self.palette),
        ]


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
