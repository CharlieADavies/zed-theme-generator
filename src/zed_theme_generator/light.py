"""Light-mode counterpart to the harmonic generator.

The same harmony-seeded palette selection, driven by `LIGHT_DIRECTION`: every
contrast floor walks lightness downward from a light background, chrome
elevates darker instead of lighter, and the emitted theme declares a light
appearance.
"""

from dataclasses import asdict
from functools import cached_property
from typing import ClassVar, Self, override

from zed_theme_generator.gen.zed_theme import AppearanceContent, ThemeStyleContent
from zed_theme_generator.generator import (
    ThemeGenerator,
    ThemeParams,
    palette_comment,
    params_comment,
    select_colors,
)
from zed_theme_generator.schemas import (
    LIGHT_DIRECTION,
    HarmonicInputs,
    Palette,
    build_style,
)


class HarmonicLightPaletteThemeGenerator(ThemeGenerator):
    """Builds a light theme from bg/fg/accent inputs via oklch harmonies."""

    generator_name: ClassVar[str] = "harmonic-light"
    summary: ClassVar[str] = (
        "Derives a full light-appearance palette from background, foreground "
        "and accent colours using a coloraide harmony and WCAG contrast floors"
    )
    inputs_spec: ClassVar[type] = HarmonicInputs

    def __init__(self, params: ThemeParams) -> None:
        self.params = params

    @classmethod
    @override
    def from_inputs(cls, inputs: HarmonicInputs) -> Self:
        """Build a generator from a validated inputs spec; background must be light."""
        return cls(ThemeParams.from_strings(**asdict(inputs)))

    @cached_property
    def palette(self) -> Palette:
        """The resolved palette, computed once; every hook below reads it."""
        return select_colors(self.params, direction=LIGHT_DIRECTION)

    @override
    def build_theme(self) -> ThemeStyleContent:
        return build_style(self.palette, appearance=AppearanceContent.light)

    @override
    def comment_lines(self) -> list[str]:
        return [
            params_comment(self.params),
            palette_comment(self.palette),
        ]

    @override
    def theme_appearance(self) -> AppearanceContent:
        return AppearanceContent.light
