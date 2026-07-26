"""Common schemas: per-generator inputs and palette->Zed-theme-schema transforms.

Everything here is either a schema (per-generator input specs, the profile
TOML envelope, colour role structure, serialisation shapes) or a pure
transformation from a filled palette onto the Zed theme JSON schema. It is an
import leaf: no other package module is imported.

The input specs are frozen dataclasses whose `Annotated` field metadata feeds
two consumers: cyclopts (CLI flattening, per-field help, validators) and
pydantic `TypeAdapter` (profile TOML validation at the boundary). Field values
stay as plain strings — conversion to `Color` happens in the params
constructors — so every spec round-trips through TOML unchanged.
"""

import json
import re
import textwrap
from collections.abc import Sequence
from dataclasses import MISSING, dataclass, field, fields
from typing import (
    Annotated,
    Any,
    Literal,
    TypeAliasType,
    TypedDict,
    get_args,
    get_origin,
    get_type_hints,
)

from annotated_types import MinLen
from coloraide import Color
from cyclopts import Parameter
from pydantic import AfterValidator, ConfigDict, ValidationError

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

AUTHOR = "cd"
SCHEMA_URL = "https://zed.dev/schema/themes/v0.2.0.json"
TRANSPARENT = "#00000000"

# --- structural constants shared across the pipeline --------------------------

MAX_L = 0.985
MIN_L = 0.015  # light-mode ramps stop short of pure black, mirroring MAX_L
DARK_DIRECTION = 1.0  # raise lightness away from dark backgrounds
LIGHT_DIRECTION = -1.0  # lower lightness away from light backgrounds

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


# --- per-generator input specs -------------------------------------------------

THEME_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_css(value: str) -> str:
    """Validate a CSS colour string, re-raising with the offending value named."""
    try:
        Color(value)
    except ValueError as err:
        raise ValueError(f"{value!r} is not a CSS colour: {err}") from None
    return value


def _check_name(value: str) -> str:
    """Validate a theme name; it becomes a file name in themes/ and profiles/."""
    if not THEME_NAME_PATTERN.match(value):
        raise ValueError(
            f"{value!r} is not a valid theme name "
            "(letters, digits, '.', '_' or '-'; must not start with punctuation)"
        )
    return value


def _css_cyclopts(type_: object, value: object) -> None:
    if isinstance(value, str):
        _check_css(value)


def _name_cyclopts(type_: object, value: object) -> None:
    if isinstance(value, str):
        _check_name(value)


# One alias, two consumers: the cyclopts validator fires on the CLI path, the
# pydantic AfterValidator at the profile-TOML boundary.
type CssColor = Annotated[
    str, Parameter(validator=_css_cyclopts), AfterValidator(_check_css)
]
type ThemeName = Annotated[
    str,
    Parameter(help="Theme (and file) name.", validator=_name_cyclopts),
    AfterValidator(_check_name),
]


@dataclass(frozen=True)
class HarmonicInputs:
    """Inputs shared by the harmonic and harmonic-light generators."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    name: ThemeName
    background: Annotated[
        CssColor,
        Parameter(
            help="Background colour for both editor and UI (any CSS colour string)."
        ),
    ]
    foreground: Annotated[CssColor, Parameter(help="Default editor text colour.")]
    accent: Annotated[
        CssColor,
        Parameter(help="Accent colour; tints UI text and seeds the hue harmony."),
    ]
    minimum_bg_contrast: Annotated[
        float,
        Parameter(
            help="Minimum WCAG contrast between the background and primary text; "
            "every other text floor (syntax, muted, subtle, line numbers) "
            "derives from it."
        ),
    ] = 10.5
    target_color_distance: Annotated[
        float,
        Parameter(
            help="Target mean pairwise OKLab distance between syntax token colours."
        ),
    ] = 0.19
    harmony_type: Annotated[
        HarmonyType, Parameter(help="coloraide harmony used as the hue-selection hint.")
    ] = "wheel"
    ui_accent_mix: Annotated[
        float, Parameter(help="How far UI text is mixed towards the accent (0-1).")
    ] = 0.55
    surface_tint: Annotated[
        float,
        Parameter(
            help="How far chrome surfaces lean from the background hue towards "
            "the accent (0-1)."
        ),
    ] = 0.3
    border_tint: Annotated[float, Parameter(help="As surface_tint, for borders.")] = 0.5


@dataclass(frozen=True)
class RainbowInputs:
    """Inputs for the rainbow generator."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    name: ThemeName
    colors: Annotated[
        tuple[CssColor, ...],
        MinLen(2),
        Parameter(
            help="Two or more CSS colour strings, most significant first; they "
            "appear verbatim in the most prominent theme roles."
        ),
    ]
    background: Annotated[
        CssColor | None,
        Parameter(
            help="Background colour for both editor and UI, used verbatim; when "
            "omitted the background is chosen to clear contrast floors and "
            "maximise OKLab distance to the colours."
        ),
    ] = None
    status_colors: Annotated[
        tuple[CssColor, CssColor, CssColor, CssColor] | None,
        Parameter(
            help="Exactly four CSS colour strings (error, warning, success, info) "
            "used verbatim; derived from the hue anchors when omitted."
        ),
    ] = None


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


# --- colour serialisation helpers ---------------------------------------------


def hex_rgba(color: Color, alpha: int | None = None) -> str:
    """Serialise a colour as the lowercase `#rrggbbaa` hex string Zed uses.

    `alpha` replaces the colour's own alpha with a 0-255 byte value.
    """
    c = color.convert("srgb")
    if alpha is not None:
        c["alpha"] = alpha / 255
    return c.to_string(hex=True, alpha=True).lower()


def shift_l(color: Color, delta: float) -> Color:
    """Return an oklch copy of `color` with its lightness shifted by `delta`."""
    c = color.convert("oklch")
    c["lightness"] = min(MAX_L, max(0.0, c["lightness"] + delta))
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
                name=name,
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


# --- profile TOML --------------------------------------------------------------


class ProfileError(Exception):
    """A profile document failed to load or validate; str() is user-facing."""


@dataclass(frozen=True)
class Profile:
    """The profile TOML envelope: which generator to run, plus run options.

    The generator's own inputs live in the `[inputs]` table and are validated
    separately against that generator's input spec.
    """

    __pydantic_config__ = ConfigDict(extra="forbid")

    generator: str
    register: bool = False
    if_exists: Literal["overwrite", "raise"] = "overwrite"
    inputs: dict[str, object] = field(default_factory=dict)


_TOML_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _toml_string(value: str) -> str:
    """A TOML basic string literal for any Python string."""
    out = ['"']
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def toml_value(value: object) -> str:
    """Render a scalar or list-of-scalars as a TOML value literal."""
    # bool first: it is an int subclass.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, Sequence):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"no TOML rendering for {type(value).__name__}")


def format_validation_error(
    err: ValidationError, *, source: str, prefix: str = ""
) -> str:
    """Flatten a pydantic error into `loc: message` lines naming the culprits."""
    lines = [f"invalid profile ({source}):"]
    for detail in err.errors():
        loc = ".".join(str(part) for part in detail["loc"])
        lines.append(f"  {prefix}{loc}: {detail['msg']}")
    return "\n".join(lines)


# Placeholder values for template fields that have no default. Keyed by field
# name, with per-generator overrides (e.g. harmonic-light needs a light
# background for its template to generate as-is).
_TEMPLATE_EXAMPLES: dict[str, object] = {
    "name": "my-theme",
    "background": "#0a1022",
    "foreground": "#ffe3f3",
    "accent": "#ee7ec6",
    "colors": ["#ff004d", "#ffa300", "#00e436", "#29adff", "#ff77a8"],
    "status_colors": ["#ff3860", "#ffdd57", "#23d160", "#209cee"],
}
_TEMPLATE_OVERRIDES: dict[str, dict[str, object]] = {
    "harmonic-light": {
        "background": "#fdf4f8",
        "foreground": "#2b1930",
        "accent": "#c02579",
    },
}


def _field_help(hint: object) -> str | None:
    """The cyclopts `Parameter(help=...)` attached to a field annotation."""
    if isinstance(hint, TypeAliasType):
        return _field_help(hint.__value__)
    if get_origin(hint) is Annotated:
        for meta in get_args(hint)[1:]:
            if isinstance(meta, Parameter) and meta.help:
                return str(meta.help)
    return None


def _comment_lines(text: str) -> list[str]:
    return [f"# {line}" for line in textwrap.wrap(text, width=76)]


def render_template(generator_name: str, spec: type, summary: str) -> str:
    """A profile TOML for `spec`, defaults prefilled and every field explained.

    Required fields carry example values, defaulted fields their defaults, and
    optional-when-omitted fields a commented-out example. Everything is read
    from the spec's own fields and annotations, so the template cannot drift
    from the CLI.
    """
    examples = {**_TEMPLATE_EXAMPLES, **_TEMPLATE_OVERRIDES.get(generator_name, {})}
    hints = get_type_hints(spec, include_extras=True)
    lines = [
        f"# ztg profile: {generator_name} generator",
        *_comment_lines(summary),
        "#",
        "# Edit, save and quit to generate. Quit without saving to abort.",
        "",
        f'generator = "{generator_name}"',
        "",
        "[inputs]",
    ]
    for spec_field in fields(spec):
        lines.append("")
        help_text = _field_help(hints[spec_field.name])
        if help_text:
            lines.extend(_comment_lines(help_text))
        if spec_field.default is MISSING:
            lines.append(f"{spec_field.name} = {toml_value(examples[spec_field.name])}")
        elif spec_field.default is None:
            lines.append(
                f"# {spec_field.name} = {toml_value(examples[spec_field.name])}"
            )
        else:
            lines.append(f"{spec_field.name} = {toml_value(spec_field.default)}")
    lines += [
        "",
        "# --- run options ---",
        "# register = false          # also copy the theme into ~/.config/zed/themes",
        '# if_exists = "overwrite"   # overwrite | raise, when registering',
        "",
    ]
    return "\n".join(lines)


def render_profile(
    generator_name: str,
    inputs: Any,
    *,
    register: bool = False,
    if_exists: str = "overwrite",
) -> str:
    """Serialise an inputs spec instance as a reusable profile TOML.

    Every non-None value is written explicitly — including defaults — so a
    saved profile reproduces its theme even if defaults change later.
    """
    lines = [f'generator = "{generator_name}"']
    if register:
        lines.append("register = true")
        if if_exists != "overwrite":
            lines.append(f"if_exists = {toml_value(if_exists)}")
    lines += ["", "[inputs]"]
    for spec_field in fields(inputs):
        value = getattr(inputs, spec_field.name)
        if value is None:
            continue
        lines.append(f"{spec_field.name} = {toml_value(value)}")
    return "\n".join(lines) + "\n"
