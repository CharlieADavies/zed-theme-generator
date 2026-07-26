"""Light-mode counterpart to the harmonic generator.

The same harmony-seeded palette selection, driven by `LIGHT_DIRECTION`: every
contrast floor walks lightness downward from a light background, chrome
elevates darker instead of lighter, and the emitted theme declares a light
appearance.
"""

from typing import ClassVar, Literal, Self, override

from zed_theme_generator import (
    GENERATORS,
    LIGHT_DIRECTION,
    HarmonyType,
    ThemeGenerator,
    ThemeParams,
    app,
    build_style,
    palette_comment,
    params_comment,
    register_themes,
    select_colors,
)
from zed_theme_generator.gen.zed_theme import AppearanceContent, ThemeStyleContent


class HarmonicLightPaletteThemeGenerator(ThemeGenerator):
    """Builds a light theme from bg/fg/accent inputs via oklch harmonies."""

    generator_name: ClassVar[str] = "harmonic-light"
    summary: ClassVar[str] = (
        "Derives a full light-appearance palette from background, foreground "
        "and accent colours using a coloraide harmony and WCAG contrast floors"
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
        return build_style(
            select_colors(self.params, direction=LIGHT_DIRECTION),
            appearance=AppearanceContent.light,
        )

    @override
    def comment_lines(self) -> list[str]:
        return [
            params_comment(self.params),
            palette_comment(select_colors(self.params, direction=LIGHT_DIRECTION)),
        ]

    @override
    def theme_appearance(self) -> AppearanceContent:
        return AppearanceContent.light


GENERATORS[HarmonicLightPaletteThemeGenerator.generator_name] = (
    HarmonicLightPaletteThemeGenerator
)


@app.command
def generate_light(
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
    """Generate a light-appearance Zed theme using a harmonic colour palette.

    Parameters
    ----------
    name
        Theme (and file) name; the light variant appears in Zed as `<name>-light`.
    background
        Background colour for both editor and UI (any CSS colour string); must
        be light (oklch lightness >= 0.5).
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
    generator = HarmonicLightPaletteThemeGenerator.from_cli(
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
