"""Generate a Zed theme from a weight-ordered list of colours used verbatim.

The rainbow generator inverts the harmonic one: instead of deriving syntax
colours from a fixed background, the user's colours (most significant first)
land untouched in the most prominent roles — UI text, accent, editor
foreground, then the syntax ladder — and the shared UI/editor background
either comes verbatim from the user or is chosen for them. Auto-selection
draws candidates from a deterministic oklch grid; those clearing per-colour
WCAG floors are feasible, and the winner maximises the mean OKLab distance to
the inputs (falling back to the least-infeasible candidate when nothing
clears). User colours are never contrast-nudged; only cycle repeats and
derived roles (muted text, comments, chrome) move.
"""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from functools import cached_property
from typing import ClassVar, Self, override

from coloraide import Color

from zed_theme_generator.gen.zed_theme import AppearanceContent, ThemeStyleContent
from zed_theme_generator.generator import (
    HUE_BLUE,
    HUE_GREEN,
    HUE_RED,
    HUE_YELLOW,
    ThemeGenerator,
    band,
    elevate,
    ensure_contrast,
    hue_distance,
    palette_comment,
)
from zed_theme_generator.schemas import (
    DARK_DIRECTION,
    LIGHT_DIRECTION,
    MAX_L,
    MIN_L,
    Palette,
    RainbowInputs,
    build_style,
    hex_rgba,
    shift_l,
)

# Cycle repeats push lightness away from the background by up to this much per
# lap around the input list, so a repeat's contrast never drops below its
# base's; laps split the remaining headroom evenly when the clamp is nearer.
CYCLE_L_SHIFT = 0.08
# A status anchor snaps to the nearest input hue within this arc (degrees).
STATUS_SNAP_TOLERANCE = 30.0
# Background candidate grid: a lightness band per side, low chromas, and the
# input hues plus their complements (de-duplicated within HUE_DEDUP_ARC).
DARK_BAND_L: tuple[float, ...] = tuple(round(0.05 + 0.02 * i, 2) for i in range(8))
LIGHT_BAND_L: tuple[float, ...] = tuple(round(0.86 + 0.02 * i, 2) for i in range(7))
BG_CANDIDATE_CHROMAS: tuple[float, ...] = (0.015, 0.03)
HUE_DEDUP_ARC = 1.0

# Prominence order: input colour i lands verbatim in slot i, and slots beyond
# the input count cycle back through the list (lightness-shifted repeats).
ROLE_ORDER: tuple[str, ...] = (
    "text",
    "accent",
    "fg_editor",
    "keyword",
    "function",
    "string",
    "type",
    "number",
    "property",
    "operator",
    "title",
    "punctuation",
)
PRIMARY_ROLES = frozenset({"text", "fg_editor"})


# --- generation parameters ----------------------------------------------------


@dataclass(frozen=True)
class RainbowParams:
    """Everything a rainbow generation depends on.

    `colors` is weight-ordered, most significant first; the colours appear
    verbatim in the most prominent theme roles. The master knobs below the
    input block stay on their defaults unless constructed directly; every
    derived value the pipeline reads is a property computed from them,
    mirroring `ThemeParams`.
    """

    name: str
    colors: tuple[Color, ...]  # oklch-converted, weight order
    # Shared UI/editor background, used verbatim; auto-selected when omitted.
    background: Color | None = None
    # Exactly four colours (error, warning, success, info) used verbatim;
    # status colours derive from the hue anchors when omitted.
    status_colors: tuple[Color, ...] | None = None
    # Minimum WCAG contrast between the background and primary text. Here it
    # filters background *candidates* — user colours are never nudged.
    minimum_bg_contrast: float = 10.5
    chroma: float = 0.14  # base derived-role chroma; every chroma cap derives
    chrome_lift: float = 0.04  # one elevation step of chrome lightness (oklch L)
    min_text_delta: float = 0.05  # kept for parity; no separation pass runs
    surface_tint: float = 0.3  # chrome hue-lean towards the lead colour
    border_tint: float = 0.5  # as surface_tint, for borders

    @classmethod
    def from_strings(
        cls,
        *,
        name: str,
        colors: Sequence[str],
        background: str | None = None,
        status_colors: Sequence[str] | None = None,
    ) -> Self:
        """Resolve raw CLI strings into generation parameters."""
        if len(colors) < 2:
            raise ValueError(f"rainbow needs at least 2 colours; got {len(colors)}")
        if status_colors is not None and len(status_colors) != 4:
            raise ValueError(
                "status_colors must be exactly 4 colours "
                f"(error, warning, success, info); got {len(status_colors)}"
            )
        # Input alpha is dropped: "verbatim" means the RGB channels, and the
        # theme declares itself opaque.
        return cls(
            name=name,
            colors=tuple(Color(c).set("alpha", 1).convert("oklch") for c in colors),
            background=(
                None
                if background is None
                else Color(background).set("alpha", 1).convert("oklch")
            ),
            status_colors=(
                None
                if status_colors is None
                else tuple(
                    Color(c).set("alpha", 1).convert("oklch") for c in status_colors
                )
            ),
        )

    # WCAG 2.1 contrast floors against the background. Unlike the harmonic
    # generator they never move a foreground colour: they grade background
    # candidates (via `required_floor`) and anchor the derived text ramp.
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
    def border_chroma(self) -> float:
        return self.chroma / 3

    @property
    def comment_chroma_cap(self) -> float:
        return self.chroma / 2

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


# --- background selection -----------------------------------------------------


def required_floor(index: int, n_colors: int, params: RainbowParams) -> float:
    """The WCAG floor input colour `index` must clear against the background.

    The max over every `ROLE_ORDER` slot the colour occupies (`i % n_colors ==
    index`): `floor_primary` for the primary text roles, `floor_syntax`
    otherwise. Cycled repeats are lightness-shifted *away* from the background,
    so the base colour's floor is the binding one. Colours beyond the last
    slot are unused and constrain nothing.
    """
    floors = (
        params.floor_primary if ROLE_ORDER[i] in PRIMARY_ROLES else params.floor_syntax
        for i in range(len(ROLE_ORDER))
        if i % n_colors == index
    )
    return max(floors, default=0.0)


def candidate_backgrounds(
    colors: Sequence[Color], params: RainbowParams
) -> list[Color]:
    """The deterministic background candidate grid for `colors`.

    Lightness runs over a dark and a light band; each lightness contributes
    one neutral (chroma 0, undefined hue) plus every de-duplicated input hue
    and its complement at each low chroma. Candidates are gamut-fitted before
    scoring so the scored colour equals the serialised one. Generation order
    is significant: `select_background` breaks ties towards it, and hue
    de-duplication keeps the first occurrence (input order is weight order).
    """
    hues: list[float] = []
    for color in colors:
        ok = color.convert("oklch")
        if ok.is_nan("hue"):
            continue
        for hue in (ok["hue"] % 360, (ok["hue"] + 180) % 360):
            if all(hue_distance(hue, seen) > HUE_DEDUP_ARC for seen in hues):
                hues.append(hue)

    candidates: list[Color] = []
    for lightness in DARK_BAND_L + LIGHT_BAND_L:
        candidates.append(Color("oklch", [lightness, 0.0, float("nan")]).fit("srgb"))
        for chroma in BG_CANDIDATE_CHROMAS:
            for hue in hues:
                candidates.append(Color("oklch", [lightness, chroma, hue]).fit("srgb"))
    return candidates


def mean_bg_distance(bg: Color, colors: Sequence[Color]) -> float:
    """Mean OKLab distance from `bg` to the input colours — the vividness score."""
    return sum(bg.delta_e(c, method="ok") for c in colors) / len(colors)


def select_background(colors: Sequence[Color], params: RainbowParams) -> Color:
    """Choose the background: feasible first, then maximally distant.

    A candidate is feasible when every input colour clears its
    `required_floor` against it; among feasible candidates the mean OKLab
    distance to the inputs is maximised (strict improvement only, so the
    earliest-generated candidate wins ties). When no candidate is feasible —
    mid-luminance saturated inputs cannot reach high WCAG ratios against
    anything — the fallback maximises the worst contrast-to-floor ratio,
    ties broken by mean distance and then generation order. Deterministic
    throughout.
    """
    floors = [required_floor(i, len(colors), params) for i in range(len(colors))]
    candidates = candidate_backgrounds(colors, params)

    best: Color | None = None
    best_distance = -1.0
    for candidate in candidates:
        feasible = all(
            candidate.contrast(color) >= floor
            for color, floor in zip(colors, floors, strict=True)
        )
        if not feasible:
            continue
        distance = mean_bg_distance(candidate, colors)
        if distance > best_distance:
            best, best_distance = candidate, distance
    if best is not None:
        return best

    best_score = -1.0
    best_distance = -1.0
    for candidate in candidates:
        score = min(
            (
                candidate.contrast(color) / floor
                for color, floor in zip(colors, floors, strict=True)
                if floor > 0
            ),
            default=float("inf"),
        )
        distance = mean_bg_distance(candidate, colors)
        if score > best_score or (score == best_score and distance > best_distance):
            best, best_score, best_distance = candidate, score, distance
    assert best is not None
    return best


# --- palette construction -----------------------------------------------------


def build_rainbow_palette(params: RainbowParams) -> Palette:
    """Fill every palette role around the verbatim input colours.

    Input colours occupy the `ROLE_ORDER` slots untouched (cycle repeats are
    lightness-shifted away from the background); everything else — status,
    chrome, borders, the muted text ramp and comments — is derived, and only
    derived colours are ever contrast-nudged. The background is the user's
    when given, auto-selected otherwise; either way its lightness decides the
    dark/light side.
    """
    colors = [c.convert("oklch") for c in params.colors]
    n = len(colors)
    if params.background is not None:
        bg = params.background.convert("oklch").fit("srgb")
    else:
        bg = select_background(colors, params)
    direction = DARK_DIRECTION if bg["lightness"] < 0.5 else LIGHT_DIRECTION

    if params.background is not None:
        # An explicit background is used verbatim, so an unreachable floor
        # would silently collapse the derived text ramp; refuse it up front.
        # Auto-selected backgrounds sit at extreme lightness and always clear.
        achievable = Color("white" if direction > 0 else "black").contrast(bg)
        if params.floor_primary > achievable:
            raise ValueError(
                f"minimum_bg_contrast {params.floor_primary:.2f} is unreachable "
                f"against this background (maximum achievable {achievable:.2f})"
            )

    # Every derived hue comes from the inputs: the lead hue is the first
    # chromatic input's, or the background's when no input has one. With no
    # chromatic colour anywhere the derived chrome goes fully achromatic —
    # chroma 0 throughout, so the numeric hue is never visible — and only
    # the semantically-anchored status colours stay chromatic.
    lead_hue = next((c["hue"] % 360 for c in colors if not c.is_nan("hue")), None)
    achromatic = lead_hue is None and bg.is_nan("hue")
    if lead_hue is not None:
        accent_hue = lead_hue
    else:
        accent_hue = 0.0 if achromatic else bg["hue"] % 360
    bg_hue = accent_hue if bg.is_nan("hue") else bg["hue"] % 360

    def derived_chroma(value: float) -> float:
        """The chroma a derived role ships with; achromatic themes zero it."""
        return 0.0 if achromatic else value

    def repeats(base: Color, laps: int) -> list[Color]:
        """`laps` lightness-shifted repeats of `base`, pairwise hex-distinct.

        Each base's repeats split its remaining lightness headroom evenly
        (capped at `CYCLE_L_SHIFT` per lap) instead of saturating on the
        clamp; whenever the headroom cannot separate two repeats at 8-bit
        precision, the repeat walks whole sRGB steps further from the
        background. A base already on the background's own side must cross
        it, so a repeat's contrast can dip below its base's — the dip is
        bounded (about 0.45 WCAG per crossing, up to ~0.6 when crushed
        against a gamut corner) and cannot compound across laps. A walk
        pinned on the corner turns around and takes the corner-adjacent
        hexes.
        """
        headroom = (
            MAX_L - base["lightness"] if direction > 0 else base["lightness"] - MIN_L
        )
        step = min(CYCLE_L_SHIFT, max(0.0, headroom) / laps)
        seen = {hex_rgba(base)}
        out: list[Color] = []
        for lap in range(1, laps + 1):
            repeat = shift_l(base, direction * step * lap)
            away = direction
            while hex_rgba(repeat) in seen:
                srgb = repeat.convert("srgb")
                for name in ("red", "green", "blue"):
                    srgb[name] = min(1.0, max(0.0, srgb[name] + away / 255))
                bumped = srgb.convert("oklch")
                if hex_rgba(bumped) == hex_rgba(repeat):
                    away = -away  # pinned on the gamut corner; turn around
                    continue
                repeat = bumped
            seen.add(hex_rgba(repeat))
            out.append(repeat)
        return out

    last = len(ROLE_ORDER) - 1
    cycled = {
        i: repeats(base, (last - i) // n)
        for i, base in enumerate(colors)
        if (last - i) // n > 0
    }

    def slot(index: int) -> Color:
        """The colour for prominence slot `index`; verbatim on the first lap."""
        cycle, base_index = divmod(index, n)
        if cycle == 0:
            return colors[base_index].clone()
        return cycled[base_index][cycle - 1].clone()

    slots = {role: slot(i) for i, role in enumerate(ROLE_ORDER)}

    if params.status_colors is not None:
        error = params.status_colors[0].clone()
        warning = params.status_colors[1].clone()
        success = params.status_colors[2].clone()
        info = params.status_colors[3].clone()
        info_hue = HUE_BLUE if info.is_nan("hue") else info["hue"] % 360
    else:
        input_hues = [c["hue"] % 360 for c in colors if not c.is_nan("hue")]

        def status_hue(anchor: float) -> float:
            """The anchor hue, snapped to a near input hue when one exists."""
            nearest = min(
                input_hues, key=lambda h: hue_distance(h, anchor), default=anchor
            )
            if hue_distance(nearest, anchor) <= STATUS_SNAP_TOLERANCE:
                return nearest
            return anchor

        def status_band(anchor: float) -> Color:
            return band(
                status_hue(anchor),
                params.chroma,
                bg,
                floor=params.floor_syntax,
                direction=direction,
            )

        error = status_band(HUE_RED)
        warning = status_band(HUE_YELLOW)
        success = status_band(HUE_GREEN)
        info = status_band(HUE_BLUE)
        info_hue = status_hue(HUE_BLUE)

    hint = band(
        info_hue,
        derived_chroma(params.hint_chroma),
        bg,
        floor=params.floor_subtle,
        direction=direction,
    )
    predictive_seed = info.mix(bg, 0.35, space="oklch")
    predictive_seed["chroma"] = derived_chroma(predictive_seed["chroma"])
    predictive = ensure_contrast(
        predictive_seed, bg, params.floor_subtle, direction=direction
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

    text = slots["text"]
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
        Color("oklch", [0.5, derived_chroma(params.line_number_chroma), bg_hue]).fit(
            "srgb"
        ),
        bg,
        params.floor_line_number,
        direction=direction,
    )

    # Comments must recede: the lowest-weight input, chroma-capped and pushed
    # only as far as the muted floor demands.
    comment = colors[-1].clone()
    comment["chroma"] = min(comment["chroma"], params.comment_chroma_cap)
    comment.fit("srgb")
    comment = ensure_contrast(comment, bg, params.floor_muted, direction=direction)

    return Palette(
        bg=bg,
        fg_editor=slots["fg_editor"],
        text=text,
        accent=slots["accent"],
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
        hint=hint,
        predictive=predictive,
        keyword=slots["keyword"],
        function=slots["function"],
        string=slots["string"],
        type=slots["type"],
        number=slots["number"],
        property=slots["property"],
        operator=slots["operator"],
        comment=comment,
        punctuation=slots["punctuation"],
        title=slots["title"],
        emphasis_strong=slots["number"].clone(),  # weight-700 is its differentiator
        accents=[slot(i) for i in range(6)],
    )


# --- generator ----------------------------------------------------------------


class RainbowThemeGenerator(ThemeGenerator):
    """Builds a theme around a weight-ordered colour list used verbatim."""

    generator_name: ClassVar[str] = "rainbow"
    summary: ClassVar[str] = (
        "Builds a theme around a weight-ordered colour list used verbatim; "
        "the background is taken as given or chosen to clear contrast floors "
        "while maximising OKLab distance from the colours"
    )
    inputs_spec: ClassVar[type] = RainbowInputs

    def __init__(self, params: RainbowParams) -> None:
        self.params = params

    @classmethod
    @override
    def from_inputs(cls, inputs: RainbowInputs) -> Self:
        """Build a generator from a validated inputs spec."""
        return cls(RainbowParams.from_strings(**asdict(inputs)))

    @cached_property
    def palette(self) -> Palette:
        """The resolved palette, computed once; every hook below reads it."""
        return build_rainbow_palette(self.params)

    @override
    def build_theme(self) -> ThemeStyleContent:
        return build_style(self.palette, appearance=self.theme_appearance())

    @override
    def theme_appearance(self) -> AppearanceContent:
        # The background (given or chosen) decides the side, not a CLI flag.
        return (
            AppearanceContent.dark
            if self.palette["bg"]["lightness"] < 0.5
            else AppearanceContent.light
        )

    @override
    def comment_lines(self) -> list[str]:
        inputs: dict[str, object] = {
            "name": self.params.name,
            "colors": [hex_rgba(c) for c in self.params.colors],
        }
        if self.params.background is not None:
            inputs["background"] = hex_rgba(self.params.background)
        if self.params.status_colors is not None:
            inputs["status_colors"] = [hex_rgba(c) for c in self.params.status_colors]
        return [
            "inputs: " + json.dumps(inputs, separators=(",", ":")),
            palette_comment(self.palette),
        ]
