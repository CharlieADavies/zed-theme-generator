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
from dataclasses import dataclass
from typing import ClassVar, Literal, Self, TypedDict, Unpack, override

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

# --- tuning constants (oklch unless noted) -----------------------------------
MAX_L = 0.985
SYNTAX_CHROMA = 0.14
ACCENT_MAX_CHROMA = 0.18
MAX_BG_CHROMA = 0.06
UI_TEXT_ACCENT_MIX = 0.55  # how far UI text is pulled towards the accent
CONTRAST_STEP = 0.01
DARK_DIRECTION = 1.0  # raise lightness away from dark backgrounds; flip for light mode

STATUS_ANCHOR_TOLERANCE = 15.0  # status hues must stay recognisable (red error etc.)
FALLBACK_ACCENT_HUE = 343.0  # pink, for achromatic accent inputs

# oklch hue anchors for colours with fixed semantics (status + strings)
HUE_RED = 25.0
HUE_GOLD = 85.0
HUE_YELLOW = 95.0
HUE_GREEN = 140.0
HUE_BLUE = 225.0

# Strings are gold, not harmony-picked: gold needs a lightness lift over the
# floor band to read as gold rather than mustard.
STRING_CHROMA = 0.13
STRING_L_LIFT = 0.06
COMMENT_CHROMA_MAX = 0.07

# WCAG 2.1 contrast floors against the background. FLOOR_SYNTAX is generative:
# it *selects* each band colour's lightness per hue and background, so
# different inputs are forced to different lightnesses, not clamped upward
# from a shared band.
FLOOR_PRIMARY = 10.5
FLOOR_SYNTAX = 9.5
FLOOR_MUTED = 6.5
FLOOR_SUBTLE = 5.0
FLOOR_LINE_NUMBER = 4.5

# Harmony-family ladders: each syntax role lives on (wheel family x rung).
WHEEL_COUNT = 12
LADDER_RUNGS = 4
SYNTAX_CAST_GAIN = 0.5  # syntax hue-cast towards the bg = gain * surface_tint
SYNTAX_CHROMA_SCALE_BASE = 0.7  # family chroma scale = base + gain * ui_accent_mix
SYNTAX_CHROMA_SCALE_GAIN = 0.6

# Pairwise separation between text elements (OKLab delta E) and its repair pass.
MIN_TEXT_DELTA = 0.05
REPAIR_MAX_SWEEPS = 8
REPAIR_HUE_STEP = 6.0

# Chrome leans from the background hue towards the accent so the whole UI
# carries the accent's cast; borders lean further and keep a little chroma.
# The lean is capped in degrees: a near-complementary bg/accent pair must tint,
# not drag chrome through the muddy brown/olive midpoint of the wheel.
TINT_ARC_MAX = 80.0
SURFACE_ACCENT_TINT = 0.3
BORDER_ACCENT_TINT = 0.5
BORDER_CHROMA = 0.05
LINE_NUMBER_ACCENT_TINT = 0.6
LINE_NUMBER_CHROMA = 0.04

# background-relative lightness offsets for chrome surfaces
SURFACE_DELTA = 0.035
ELEMENT_DELTA = 0.04
ELEMENT_DISABLED_DELTA = 0.02
HOVER_DELTA = 0.08
ACTIVE_DELTA = 0.12
BORDER_DISABLED_DELTA = 0.06
BORDER_VARIANT_DELTA = 0.09
BORDER_DELTA = 0.17

STATUS_BORDER_BG_MIX = 0.65  # status borders sit most of the way back to the bg

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


class HarmonicPaletteInputs(TypedDict):
    """CLI inputs for generating a Zed theme from a harmonic colour palette."""

    name: str
    background: str
    foreground: str
    accent: str
    # Target oklch lightness delta between the background and the editor foreground
    target_contrast: float
    # A coloraide harmony used as a hint for hue selection, topped up with wheel
    # hues whenever it yields too few distinct ones.
    harmony_type: HarmonyType
    # Pinkness knobs: accent mix into UI text, and how far chrome surfaces and
    # borders lean from the background hue towards the accent.
    ui_accent_mix: float
    surface_tint: float
    border_tint: float


class HarmonicPalette(TypedDict):
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
    players: list[Color]


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
    lightness = bg["lightness"]
    while 0.0 <= lightness <= MAX_L:
        probe = Color("oklch", [lightness, chroma, hue]).fit("srgb")
        if probe.contrast(bg) >= floor:
            return lightness
        lightness += direction * CONTRAST_STEP
    return MAX_L if direction > 0 else 0.0


def band(hue: float, chroma: float, bg: Color, *, floor: float = FLOOR_SYNTAX) -> Color:
    """The darkest colour at (chroma, hue) meeting `floor` — the floor selects L."""
    lightness = floor_lightness(hue, chroma, bg, floor)
    return Color("oklch", [lightness, chroma, hue]).fit("srgb")


def _enforce_text_separation(
    roles: dict[str, Color], floors: dict[str, float], bg: Color
) -> None:
    """Push text colours apart until every pair is >= MIN_TEXT_DELTA in OKLab.

    Deterministic: roles are visited in dict (seniority) order and only the
    junior colour of a violating pair moves. Lightness is the first axis — the
    junior's direction comes from its index parity (evens climb, odds sink) and
    bounces off its WCAG floor and MAX_L — with hue rotation away from the
    senior colour as the fallback axis. Every move keeps the junior above its
    contrast floor.
    """
    names = list(roles)
    for _ in range(REPAIR_MAX_SWEEPS):
        moved = False
        for i, senior in enumerate(names):
            for j in range(i + 1, len(names)):
                junior = names[j]
                if roles[senior].delta_e(roles[junior], method="ok") >= MIN_TEXT_DELTA:
                    continue
                moved = True
                floor = floors.get(junior, FLOOR_SYNTAX)
                direction = 1.0 if j % 2 == 0 else -1.0
                c = roles[junior]
                for _ in range(30):
                    candidate = c.clone()
                    candidate["lightness"] = min(
                        MAX_L,
                        max(0.0, candidate["lightness"] + direction * CONTRAST_STEP),
                    )
                    candidate.fit("srgb")
                    if (
                        candidate.contrast(bg) < floor
                        or candidate["lightness"] == c["lightness"]
                    ):
                        direction = -direction
                        continue
                    c = candidate
                    if roles[senior].delta_e(c, method="ok") >= MIN_TEXT_DELTA:
                        break
                else:
                    away_arc = ((c["hue"] - roles[senior]["hue"] + 180) % 360) - 180
                    away = 1.0 if away_arc >= 0 else -1.0
                    for _ in range(10):
                        c["hue"] = (c["hue"] + away * REPAIR_HUE_STEP) % 360
                        c.fit("srgb")
                        if roles[senior].delta_e(c, method="ok") >= MIN_TEXT_DELTA:
                            break
                roles[junior] = c
        if not moved:
            return


# --- generators --------------------------------------------------------------


@dataclass(frozen=True)
class HarmonicGenerationParams:
    """Resolved generation parameters: parsed colours plus the coloraide harmony name."""

    name: str
    background: Color
    foreground: Color
    accent: Color
    target_contrast: float
    harmony: str
    ui_accent_mix: float
    surface_tint: float
    border_tint: float


class ThemeGenerator(ABC):
    """Base class for theme generators registered in `GENERATORS`."""

    generator_name: ClassVar[str]
    summary: ClassVar[str]

    @abstractmethod
    def build_theme(self) -> ThemeStyleContent:
        """Produce a fully-populated Zed theme style."""

    def save_theme(
        self,
        style: ThemeStyleContent,
        *,
        name: str,
        directory: pathlib.Path | None = None,
    ) -> pathlib.Path:
        """Save the theme family JSON and refresh the extension.toml Zed reads."""
        directory = THEMES_DIR if directory is None else directory
        family = ThemeFamilyContent(
            author=AUTHOR,
            name=name,
            themes=[
                ThemeContent(
                    appearance=AppearanceContent.dark,
                    name=f"{name}-dark",
                    style=style,
                )
            ],
        )
        missing = [
            field.alias or field_name
            for field_name, field in ThemeStyleContent.model_fields.items()
            if getattr(style, field_name) is None
        ]
        if missing:
            raise ValueError(f"Theme style is missing values for: {missing}")
        payload = {
            "$schema": SCHEMA_URL,
            **family.model_dump(mode="json", by_alias=True, exclude_none=True),
        }
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n")
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

    def __init__(self, params: HarmonicGenerationParams) -> None:
        self.params = params

    @classmethod
    def from_cli(cls, **kwargs: Unpack[HarmonicPaletteInputs]) -> Self:
        """Resolve raw CLI strings into generation parameters."""
        return cls(
            HarmonicGenerationParams(
                name=kwargs["name"],
                background=Color(kwargs["background"]),
                foreground=Color(kwargs["foreground"]),
                accent=Color(kwargs["accent"]),
                target_contrast=kwargs["target_contrast"],
                harmony=HARMONY_TO_COLORAIDE[kwargs["harmony_type"]],
                ui_accent_mix=kwargs["ui_accent_mix"],
                surface_tint=kwargs["surface_tint"],
                border_tint=kwargs["border_tint"],
            )
        )

    def select_colors(self) -> HarmonicPalette:
        """Fill every palette role: harmony-seeded hues, contrast-floored lightness."""
        params = self.params

        bg = params.background.convert("oklch")
        if bg["lightness"] >= 0.5:
            raise ValueError(
                "Only dark backgrounds are supported for now "
                f"(oklch lightness {bg['lightness']:.2f} >= 0.5)"
            )
        bg["chroma"] = min(bg["chroma"], MAX_BG_CHROMA)
        bg.fit("srgb")
        bg_hue = FALLBACK_ACCENT_HUE if bg.is_nan("hue") else bg["hue"]

        accent = params.accent.convert("oklch")
        if accent.is_nan("hue"):
            accent["hue"] = FALLBACK_ACCENT_HUE
        accent_chroma = min(max(accent["chroma"], SYNTAX_CHROMA), ACCENT_MAX_CHROMA)
        accent = band(accent["hue"], accent_chroma, bg)
        accent_hue = accent["hue"]

        fg_editor = params.foreground.convert("oklch")
        fg_editor["lightness"] = min(MAX_L, bg["lightness"] + params.target_contrast)
        fg_editor.fit("srgb")
        fg_editor = ensure_contrast(fg_editor, bg, FLOOR_PRIMARY)

        text = fg_editor.mix(accent, params.ui_accent_mix, space="oklch")
        text = ensure_contrast(text, bg, FLOOR_PRIMARY)

        # Harmony families: full wheel colours from the accent carry its chroma
        # into every family. The variant knobs perturb syntax too (user choice):
        # surface_tint scales the hue-cast towards the bg, ui_accent_mix scales
        # family chroma — so variants sharing bg+accent still diverge.
        syntax_cast = SYNTAX_CAST_GAIN * params.surface_tint
        chroma_scale = (
            SYNTAX_CHROMA_SCALE_BASE + SYNTAX_CHROMA_SCALE_GAIN * params.ui_accent_mix
        )
        wheel = accent.harmony("wheel", space="oklch", count=WHEEL_COUNT)
        wheel_hues = [w["hue"] % 360 for w in wheel]

        def ladder(hue: float, chroma: float) -> list[Color]:
            """Floor-anchored rungs interpolated from the band colour to fg.

            The top endpoint's hue is pinned to the family hue: interpolating
            towards fg's own hue flips direction at the antipode and drifts
            every high rung towards the fg hue.
            """
            base = band(hue, chroma, bg)
            top = fg_editor.clone()
            top["hue"] = hue
            return Color.steps([base, top], steps=LADDER_RUNGS, space="oklch")

        def family(index: int, rung: int) -> Color:
            hue = hue_towards(wheel_hues[index], bg_hue, syntax_cast)
            chroma = min(wheel[index]["chroma"] * chroma_scale, ACCENT_MAX_CHROMA)
            return ladder(hue, chroma)[rung]

        def pick(anchor: float, tolerance: float) -> float:
            best = min(wheel_hues, key=lambda h: hue_distance(h, anchor))
            return best if hue_distance(best, anchor) <= tolerance else anchor

        # Status colours: hue-anchored tightly so semantics stay legible.
        error = band(pick(HUE_RED, STATUS_ANCHOR_TOLERANCE), SYNTAX_CHROMA, bg)
        warning = band(pick(HUE_YELLOW, STATUS_ANCHOR_TOLERANCE), SYNTAX_CHROMA, bg)
        success = band(pick(HUE_GREEN, STATUS_ANCHOR_TOLERANCE), SYNTAX_CHROMA, bg)
        info = band(pick(HUE_BLUE, STATUS_ANCHOR_TOLERANCE), SYNTAX_CHROMA, bg)

        # Syntax roles on the (family x rung) grid; family 0 is the accent.
        families = FAMILY_MAPS[params.harmony]
        string_hue = hue_towards(HUE_GOLD, bg_hue, syntax_cast / 2)
        string_l = min(
            MAX_L,
            floor_lightness(string_hue, STRING_CHROMA, bg, FLOOR_SYNTAX) + STRING_L_LIFT,
        )
        string = Color("oklch", [string_l, STRING_CHROMA, string_hue]).fit("srgb")

        # Comment: a mono-harmony shade of the accent, muted and bg-cast.
        mono = accent.harmony("mono", space="oklch")
        comment = mono[2].clone()
        comment["chroma"] = min(comment["chroma"], COMMENT_CHROMA_MAX)
        comment["hue"] = hue_towards(comment["hue"], bg_hue, syntax_cast)
        comment["lightness"] = max(
            comment["lightness"],
            floor_lightness(comment["hue"], comment["chroma"], bg, FLOOR_MUTED),
        )
        comment.fit("srgb")

        # Every text element must be distinguishable from every other; dict
        # order is seniority — the repair pass only ever moves the junior role.
        roles: dict[str, Color] = {
            "fg_editor": fg_editor,
            "keyword": family(0, 0),
            "string": string,
            "function": family(families["function"], 0),
            "type": family(families["type"], 0),
            "number": family(families["number"], 0),
            "property": family(families["property"], 0),
            "operator": family(0, 1),
            "title": family(families["title"], 1),
            "punctuation": family(0, 2),
            "comment": comment,
            "hint": band(info["hue"], 0.06, bg, floor=FLOOR_MUTED),
            "predictive": ensure_contrast(
                info.mix(bg, 0.35, space="oklch"), bg, FLOOR_SUBTLE
            ),
        }
        role_floors = {
            "fg_editor": FLOOR_PRIMARY,
            "comment": FLOOR_MUTED,
            "hint": FLOOR_SUBTLE,
            "predictive": FLOOR_SUBTLE,
        }
        _enforce_text_separation(roles, role_floors, bg)
        keyword = roles["keyword"]
        string = roles["string"]
        function = roles["function"]
        type_ = roles["type"]
        number = roles["number"]
        property_ = roles["property"]
        operator = roles["operator"]
        title = roles["title"]
        punctuation = roles["punctuation"]
        comment = roles["comment"]
        hint = roles["hint"]
        predictive = roles["predictive"]
        emphasis_strong = number.clone()  # weight-700 is its differentiator

        # Chrome: the background raised by fixed lightness offsets, leaning
        # towards the accent hue.
        def raise_bg(
            delta: float,
            *,
            tint: float = params.surface_tint,
            chroma: float | None = None,
        ) -> Color:
            c = bg.clone()
            c["lightness"] = c["lightness"] + delta
            c["hue"] = hue_towards(bg_hue, accent_hue, tint)
            if chroma is not None:
                c["chroma"] = chroma
            return c.fit("srgb")

        border_focused = Color("oklch", [0.55, 0.12, accent_hue]).fit("srgb")
        border_selected = Color("oklch", [0.42, 0.10, accent_hue]).fit("srgb")

        text_muted = ensure_contrast(shift_l(text, -0.20), bg, FLOOR_MUTED)
        text_disabled = text.clone()
        text_disabled["lightness"] -= 0.32
        text_disabled["chroma"] /= 2
        text_disabled.fit("srgb")
        text_disabled = ensure_contrast(text_disabled, bg, FLOOR_SUBTLE)
        line_number = ensure_contrast(
            Color(
                "oklch",
                [
                    0.5,
                    LINE_NUMBER_CHROMA,
                    hue_towards(bg_hue, accent_hue, LINE_NUMBER_ACCENT_TINT),
                ],
            ).fit("srgb"),
            bg,
            FLOOR_LINE_NUMBER,
        )

        # Accents sample alternate wheel families so they span the wheel.
        accents = [keyword.clone()] + [family(i, 0) for i in (2, 4, 6, 8, 10)]
        players = [
            keyword.clone(),
            function.clone(),
            string.clone(),
            number.clone(),
            family(10, 0),
            error.clone(),
            warning.clone(),
            type_.clone(),
        ]

        return HarmonicPalette(
            bg=bg,
            fg_editor=fg_editor,
            text=text,
            accent=accent,
            surface=raise_bg(SURFACE_DELTA),
            element=raise_bg(ELEMENT_DELTA),
            element_hover=raise_bg(HOVER_DELTA),
            element_active=raise_bg(ACTIVE_DELTA),
            border=raise_bg(
                BORDER_DELTA, tint=params.border_tint, chroma=BORDER_CHROMA
            ),
            border_variant=raise_bg(
                BORDER_VARIANT_DELTA, tint=params.border_tint, chroma=BORDER_CHROMA
            ),
            border_focused=border_focused,
            border_selected=border_selected,
            border_disabled=raise_bg(
                BORDER_DISABLED_DELTA, tint=params.border_tint, chroma=BORDER_CHROMA
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
            keyword=keyword,
            function=function,
            string=string,
            type=type_,
            number=number,
            property=property_,
            operator=operator,
            comment=comment,
            punctuation=punctuation,
            title=title,
            emphasis_strong=emphasis_strong,
            accents=accents,
            players=players,
        )

    @override
    def build_theme(self) -> ThemeStyleContent:
        """Map the filled palette onto every key of the Zed theme schema."""
        palette = self.select_colors()
        bg = palette["bg"]
        accent = palette["accent"]
        fg_editor = palette["fg_editor"]
        text = palette["text"]
        surface = palette["surface"]
        element = palette["element"]
        element_hover = palette["element_hover"]
        element_active = palette["element_active"]
        element_disabled = shift_l(bg, ELEMENT_DISABLED_DELTA)
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
        comment_doc = shift_l(comment, 0.06)

        def status(role: Color) -> tuple[str, str, str]:
            """(foreground, subtle background, border) for a status colour."""
            return (
                hex_rgba(role),
                hex_rgba(role, 0x26),
                hex_rgba(role.mix(bg, STATUS_BORDER_BG_MIX, space="oklab")),
            )

        def bright(c: Color) -> Color:
            b = c.clone()
            b["lightness"] += 0.06
            b["chroma"] += 0.01
            return b.fit("srgb")

        def dim(c: Color) -> Color:
            d = c.clone()
            d["lightness"] -= 0.18
            d["chroma"] = max(0.0, d["chroma"] - 0.03)
            return d.fit("srgb")

        def entry(
            color: Color, *, italic: bool = False, weight: int | None = None
        ) -> HighlightStyleContent:
            return HighlightStyleContent(
                color=hex_rgba(color),
                font_style=FontStyleContent.italic if italic else None,
                font_weight=FontWeight(weight) if weight is not None else None,
            )

        ansi_white = fg_editor.clone()
        ansi_white["lightness"] -= 0.10
        ansi_white["chroma"] = min(ansi_white["chroma"], 0.02)
        ansi_white.fit("srgb")

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
            "attribute": entry(function),
            "boolean": entry(number),
            "comment": entry(comment),
            "comment.doc": entry(comment_doc),
            "constant": entry(number),
            "constructor": entry(function),
            "embedded": entry(fg_editor),
            "emphasis": entry(accent, italic=True),
            "emphasis.strong": entry(palette["emphasis_strong"], weight=700),
            "enum": entry(palette["property"]),
            "function": entry(function),
            "hint": entry(palette["hint"]),
            "keyword": entry(keyword),
            "label": entry(function),
            "link_text": entry(function),
            "link_uri": entry(type_),
            "namespace": entry(fg_editor),
            "number": entry(number),
            "operator": entry(palette["operator"]),
            "predictive": entry(palette["predictive"], italic=True),
            "preproc": entry(fg_editor),
            "primary": entry(fg_editor),
            "property": entry(palette["property"]),
            "punctuation": entry(palette["punctuation"]),
            "punctuation.bracket": entry(palette["punctuation"]),
            "punctuation.delimiter": entry(palette["punctuation"]),
            "punctuation.list_marker": entry(palette["property"]),
            "punctuation.markup": entry(palette["property"]),
            "punctuation.special": entry(palette["emphasis_strong"]),
            "selector": entry(number),
            "selector.pseudo": entry(function),
            "string": entry(string),
            "string.escape": entry(comment_doc),
            "string.regex": entry(number),
            "string.special": entry(number),
            "string.special.symbol": entry(number),
            "tag": entry(function),
            "text.literal": entry(string),
            "title": entry(palette["title"], weight=600),
            "type": entry(type_),
            "variable": entry(fg_editor),
            "variable.special": entry(number),
            "variant": entry(function),
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
                    background=hex_rgba(c),
                    cursor=hex_rgba(c),
                    selection=hex_rgba(c, 0x47),
                )
                for c in palette["players"]
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
            terminal_ansi_black=hex_rgba(element),
            terminal_ansi_blue=hex_rgba(function),
            terminal_ansi_bright_black=hex_rgba(text_disabled),
            terminal_ansi_bright_blue=hex_rgba(bright(function)),
            terminal_ansi_bright_cyan=hex_rgba(bright(type_)),
            terminal_ansi_bright_green=hex_rgba(bright(success)),
            terminal_ansi_bright_magenta=hex_rgba(bright(keyword)),
            terminal_ansi_bright_red=hex_rgba(bright(error)),
            terminal_ansi_bright_white=hex_rgba(fg_editor),
            terminal_ansi_bright_yellow=hex_rgba(bright(warning)),
            terminal_ansi_cyan=hex_rgba(type_),
            terminal_ansi_dim_black=hex_rgba(surface),
            terminal_ansi_dim_blue=hex_rgba(dim(function)),
            terminal_ansi_dim_cyan=hex_rgba(dim(type_)),
            terminal_ansi_dim_green=hex_rgba(dim(success)),
            terminal_ansi_dim_magenta=hex_rgba(dim(keyword)),
            terminal_ansi_dim_red=hex_rgba(dim(error)),
            terminal_ansi_dim_white=hex_rgba(text_muted),
            terminal_ansi_dim_yellow=hex_rgba(dim(warning)),
            terminal_ansi_green=hex_rgba(success),
            terminal_ansi_magenta=hex_rgba(keyword),
            terminal_ansi_red=hex_rgba(error),
            terminal_ansi_white=hex_rgba(ansi_white),
            terminal_ansi_yellow=hex_rgba(warning),
            terminal_background=hex_rgba(bg),
            terminal_bright_foreground=hex_rgba(shift_l(fg_editor, 0.03)),
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
    *,
    background: str,
    foreground: str,
    accent: str,
    target_contrast: float = 0.76,
    harmony_type: HarmonyType = "wheel",
    ui_accent_mix: float = UI_TEXT_ACCENT_MIX,
    surface_tint: float = SURFACE_ACCENT_TINT,
    border_tint: float = BORDER_ACCENT_TINT,
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
    target_contrast
        Target oklch lightness delta between background and editor foreground.
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
        target_contrast=target_contrast,
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


def main() -> None:
    app()
