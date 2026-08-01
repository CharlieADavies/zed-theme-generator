"""Generate and register colourful, high-contrast Zed themes.

The work lives in focused submodules: `schemas` (palette structure and
palette->Zed-schema transforms), `generator` (parameters, colour pipeline, and
generator classes), one module per generator (`light`, `rainbow`), and `cli`
(the cyclopts app and entry modes). This root module only re-exports the
library surface; importing it never builds the CLI app.
"""

from zed_theme_generator.gen.zed_theme import AppearanceContent
from zed_theme_generator.generator import (
    HARMONY_TO_COLORAIDE,
    HUE_BLUE,
    HUE_GREEN,
    HUE_RED,
    HUE_YELLOW,
    PROFILES_DIR,
    REPO_ROOT,
    THEMES_DIR,
    ZED_THEMES_DIR,
    HarmonicPaletteThemeGenerator,
    ThemeGenerator,
    ThemeParams,
    band,
    elevate,
    ensure_contrast,
    hue_distance,
    palette_comment,
    params_comment,
    select_colors,
    write_extension_toml,
)
from zed_theme_generator.schemas import (
    AUTHOR,
    DARK_DIRECTION,
    LIGHT_DIRECTION,
    SCHEMA_URL,
    HarmonyType,
    Palette,
    build_style,
    hex_rgba,
    render_theme_json,
    shift_l,
    theme_family_payload,
)

__all__ = [
    "AUTHOR",
    "DARK_DIRECTION",
    "HARMONY_TO_COLORAIDE",
    "HUE_BLUE",
    "HUE_GREEN",
    "HUE_RED",
    "HUE_YELLOW",
    "LIGHT_DIRECTION",
    "PROFILES_DIR",
    "REPO_ROOT",
    "SCHEMA_URL",
    "THEMES_DIR",
    "ZED_THEMES_DIR",
    "AppearanceContent",
    "HarmonicPaletteThemeGenerator",
    "HarmonyType",
    "Palette",
    "ThemeGenerator",
    "ThemeParams",
    "band",
    "build_style",
    "elevate",
    "ensure_contrast",
    "hex_rgba",
    "hue_distance",
    "palette_comment",
    "params_comment",
    "render_theme_json",
    "select_colors",
    "shift_l",
    "theme_family_payload",
    "write_extension_toml",
]
