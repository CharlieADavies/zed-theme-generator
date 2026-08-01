"""Pin the user-scale -> internal-unit mapping at the from_strings boundary."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from zed_theme_generator import ThemeParams
from zed_theme_generator.generator import SYNTAX_SPREAD_MAX


def test_from_strings_rescales_knobs() -> None:
    """0-100 knobs become 0-1 fractions; syntax_spread 50 lands on 0.19."""
    params = ThemeParams.from_strings(
        name="pin",
        background="#0a1022",
        foreground="#ffe3f3",
        accent="#ee7ec6",
        minimum_bg_contrast=10.5,
        syntax_spread=50.0,
        accent_mix=55.0,
        surface_blend=30.0,
        border_blend=50.0,
    )
    assert params.ui_accent_mix == 0.55
    assert params.surface_tint == 0.3
    assert params.border_tint == 0.5
    assert params.target_color_distance == pytest.approx(0.19)
    assert params.minimum_bg_contrast == 10.5


@given(
    accent_mix=st.floats(min_value=0.0, max_value=100.0),
    surface_blend=st.floats(min_value=0.0, max_value=100.0),
    border_blend=st.floats(min_value=0.0, max_value=100.0),
    syntax_spread=st.floats(min_value=0.0, max_value=100.0),
    minimum_bg_contrast=st.floats(min_value=1.0, max_value=21.0),
)
def test_knob_scale_mapping_is_linear(
    accent_mix: float,
    surface_blend: float,
    border_blend: float,
    syntax_spread: float,
    minimum_bg_contrast: float,
) -> None:
    """Every 0-100 knob maps to x/100 internally; syntax_spread runs linearly
    through [0, SYNTAX_SPREAD_MAX]; the contrast floor passes through as-is."""
    params = ThemeParams.from_strings(
        name="prop",
        background="#0a1022",
        foreground="#ffe3f3",
        accent="#ee7ec6",
        minimum_bg_contrast=minimum_bg_contrast,
        syntax_spread=syntax_spread,
        accent_mix=accent_mix,
        surface_blend=surface_blend,
        border_blend=border_blend,
    )
    assert params.ui_accent_mix == pytest.approx(accent_mix / 100, abs=1e-12)
    assert params.surface_tint == pytest.approx(surface_blend / 100, abs=1e-12)
    assert params.border_tint == pytest.approx(border_blend / 100, abs=1e-12)
    assert params.target_color_distance == pytest.approx(
        syntax_spread / 100 * SYNTAX_SPREAD_MAX, abs=1e-12
    )
    assert params.minimum_bg_contrast == minimum_bg_contrast
