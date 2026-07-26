"""End-to-end invariants for the rainbow generator's neon fixture."""

import json
import pathlib
import re
from typing import cast

import pytest
from coloraide import Color

from zed_theme_generator import (
    HUE_BLUE,
    HUE_GREEN,
    HUE_RED,
    HUE_YELLOW,
    hex_rgba,
    hue_distance,
    render_theme_json,
    theme_family_payload,
)
from zed_theme_generator.gen.zed_theme import ThemeStyleContent
from zed_theme_generator.rainbow import (
    ROLE_ORDER,
    RainbowParams,
    RainbowThemeGenerator,
    build_rainbow_palette,
    candidate_backgrounds,
    mean_bg_distance,
    required_floor,
    select_background,
)

HEX_RGBA = re.compile(r"^#[0-9a-f]{8}$")
STYLE_KEYS = {
    field.alias or name for name, field in ThemeStyleContent.model_fields.items()
}

# Weight-ordered neon inputs; slots 0..4 of ROLE_ORDER take them verbatim.
INPUTS = ("#ff004c", "#ffe600", "#00ffd5", "#b700ff", "#ff8c00")
# Murky near-black inputs: infeasible against every dark candidate but
# comfortably feasible on the light side (all clear 14:1 against white), so
# they exercise the feasible branch and the light-appearance path.
DARK_INPUTS = ("#1a0033", "#003322", "#330011")
STATUS_INPUTS = ("#ff3322", "#ffbb00", "#22cc55", "#3399ff")


@pytest.fixture(scope="module")
def params() -> RainbowParams:
    return RainbowParams.from_strings(name="vomit", colors=INPUTS)


@pytest.fixture(scope="module")
def generator(params: RainbowParams) -> RainbowThemeGenerator:
    return RainbowThemeGenerator(params)


@pytest.fixture(scope="module")
def style_json(generator: RainbowThemeGenerator) -> dict[str, object]:
    return generator.build_theme().model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def _floors(params: RainbowParams) -> list[float]:
    return [
        required_floor(i, len(params.colors), params) for i in range(len(params.colors))
    ]


def _is_feasible(candidate: Color, params: RainbowParams) -> bool:
    return all(
        candidate.contrast(color) >= floor
        for color, floor in zip(params.colors, _floors(params), strict=True)
    )


def _fallback_score(candidate: Color, params: RainbowParams) -> float:
    return min(
        candidate.contrast(color) / floor
        for color, floor in zip(params.colors, _floors(params), strict=True)
        if floor > 0
    )


def _saved_theme(
    generator: RainbowThemeGenerator, name: str, directory: pathlib.Path
) -> dict[str, object]:
    path = generator.save_theme(generator.build_theme(), name=name, directory=directory)
    lines = path.read_text().splitlines()
    payload = json.loads("\n".join(line for line in lines if not line.startswith("//")))
    (theme,) = payload["themes"]
    return cast("dict[str, object]", theme)


def test_all_schema_keys_emitted(style_json: dict[str, object]) -> None:
    assert set(style_json) == STYLE_KEYS


def test_colour_format(style_json: dict[str, object]) -> None:
    for key, value in style_json.items():
        if key in {"accents", "players", "syntax", "background.appearance"}:
            continue
        assert isinstance(value, str) and HEX_RGBA.match(value), f"{key}: {value!r}"
    assert style_json["background.appearance"] == "opaque"
    assert style_json["border.transparent"] == "#00000000"


def test_collections_shapes(style_json: dict[str, object]) -> None:
    accents = style_json["accents"]
    players = style_json["players"]
    syntax = style_json["syntax"]
    assert isinstance(accents, list) and len(accents) == 6
    assert isinstance(players, list) and len(players) == 8
    assert isinstance(syntax, dict) and len(syntax) == 43
    for value in accents:
        assert isinstance(value, str) and HEX_RGBA.match(value)
    for token_entry in syntax.values():
        assert isinstance(token_entry, dict)
        assert "color" in token_entry


def test_backgrounds_unified(style_json: dict[str, object]) -> None:
    background = style_json["background"]
    for key in (
        "editor.background",
        "terminal.background",
        "status_bar.background",
        "title_bar.background",
        "toolbar.background",
    ):
        assert style_json[key] == background, key


def test_required_floor_takes_max_over_cycled_slots(params: RainbowParams) -> None:
    n = len(INPUTS)
    for index in range(n):
        roles = [ROLE_ORDER[i] for i in range(len(ROLE_ORDER)) if i % n == index]
        expected = max(
            params.floor_primary
            if role in {"text", "fg_editor"}
            else params.floor_syntax
            for role in roles
        )
        assert required_floor(index, n, params) == expected


def test_background_optimality_fallback(params: RainbowParams) -> None:
    # The neon inputs cannot clear their floors against ANY background:
    # #ff004c tops out at WCAG 5.4:1 (vs pure black), far short of its 10.5
    # primary floor, so the feasible set is empty and selection takes the
    # documented fallback — maximise the worst contrast-to-floor ratio, ties
    # by mean OKLab distance, then generation order.
    colors = list(params.colors)
    candidates = candidate_backgrounds(colors, params)
    assert not any(_is_feasible(candidate, params) for candidate in candidates)
    chosen = select_background(colors, params)
    chosen_score = _fallback_score(chosen, params)
    chosen_distance = mean_bg_distance(chosen, colors)
    for candidate in candidates:
        score = _fallback_score(candidate, params)
        assert chosen_score >= score
        if score == chosen_score:
            assert chosen_distance >= mean_bg_distance(candidate, colors)


def test_background_optimality_feasible() -> None:
    # The murky inputs exercise the primary rule: the chosen background is
    # exactly feasible (pre-serialisation, no tolerance) and maximises the
    # mean OKLab distance over every feasible candidate.
    params = RainbowParams.from_strings(name="murk", colors=DARK_INPUTS)
    colors = list(params.colors)
    chosen = select_background(colors, params)
    feasible = [
        candidate
        for candidate in candidate_backgrounds(colors, params)
        if _is_feasible(candidate, params)
    ]
    assert feasible
    assert _is_feasible(chosen, params)
    chosen_distance = mean_bg_distance(chosen, colors)
    for candidate in feasible:
        assert chosen_distance >= mean_bg_distance(candidate, colors)


def test_side_selection_dark(
    generator: RainbowThemeGenerator, tmp_path: pathlib.Path
) -> None:
    assert generator.palette["bg"]["lightness"] < 0.5
    theme = _saved_theme(generator, "vomit", tmp_path)
    assert theme["appearance"] == "dark"
    assert theme["name"] == "vomit-dark"


def test_side_selection_light(tmp_path: pathlib.Path) -> None:
    murk = RainbowThemeGenerator(
        RainbowParams.from_strings(name="murk", colors=DARK_INPUTS)
    )
    assert murk.palette["bg"]["lightness"] > 0.5
    theme = _saved_theme(murk, "murk", tmp_path)
    assert theme["appearance"] == "light"
    assert theme["name"] == "murk-light"


def test_verbatim_prominent_roles(params: RainbowParams) -> None:
    # Only the Color-valued roles are read from this, so the cast is safe.
    palette = cast("dict[str, Color]", build_rainbow_palette(params))
    for role, given in zip(
        ("text", "accent", "fg_editor", "keyword", "function"), INPUTS, strict=True
    ):
        assert hex_rgba(palette[role]) == hex_rgba(Color(given)), role


def test_cycling_shifts_repeats() -> None:
    two = RainbowParams.from_strings(name="two", colors=("#ff004c", "#ffe600"))
    palette = build_rainbow_palette(two)
    # Slot 2 (fg_editor) reuses colour 0 on cycle 1: same source, shifted
    # away from the background, so it must not collide with verbatim slot 0.
    assert hex_rgba(palette["text"]) == hex_rgba(Color("#ff004c"))
    assert hex_rgba(palette["fg_editor"]) != hex_rgba(palette["text"])


def test_status_defaults_hue_anchored(params: RainbowParams) -> None:
    palette = cast("dict[str, Color]", build_rainbow_palette(params))
    anchors = {
        "error": HUE_RED,
        "warning": HUE_YELLOW,
        "success": HUE_GREEN,
        "info": HUE_BLUE,
    }
    for role, anchor in anchors.items():
        hue = palette[role].convert("oklch")["hue"] % 360
        # 30 degrees of input-hue snapping plus wheel/gamut slack.
        assert hue_distance(hue, anchor) <= 35.0, (role, hue)


def test_explicit_background_verbatim() -> None:
    given = RainbowParams.from_strings(
        name="vomit", colors=INPUTS, background="#0a1022"
    )
    dump = (
        RainbowThemeGenerator(given)
        .build_theme()
        .model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    assert dump["background"] == hex_rgba(Color("#0a1022"))
    assert dump["editor.background"] == dump["background"]


def test_explicit_background_decides_side(tmp_path: pathlib.Path) -> None:
    # A light explicit background flips the neon fixture to the light side,
    # overriding what auto-selection would have picked.
    lightside = RainbowThemeGenerator(
        RainbowParams.from_strings(name="glare", colors=INPUTS, background="#fdf4f8")
    )
    assert lightside.palette["bg"]["lightness"] > 0.5
    theme = _saved_theme(lightside, "glare", tmp_path)
    assert theme["appearance"] == "light"
    assert theme["name"] == "glare-light"


def test_status_verbatim() -> None:
    given = RainbowParams.from_strings(
        name="vomit", colors=INPUTS, status_colors=STATUS_INPUTS
    )
    dump = (
        RainbowThemeGenerator(given)
        .build_theme()
        .model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    for key, source in zip(
        ("error", "warning", "success", "info"), STATUS_INPUTS, strict=True
    ):
        assert dump[key] == hex_rgba(Color(source)), key


def test_determinism() -> None:
    def render() -> str:
        fresh = RainbowThemeGenerator(
            RainbowParams.from_strings(name="vomit", colors=INPUTS)
        )
        return render_theme_json(
            theme_family_payload(
                fresh.build_theme(), name="vomit", appearance=fresh.theme_appearance()
            ),
            fresh.comment_lines(),
        )

    assert render() == render()


def test_validation() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        RainbowParams.from_strings(name="x", colors=("#ff004c",))
    with pytest.raises(ValueError, match="exactly 4"):
        RainbowParams.from_strings(
            name="x", colors=INPUTS, status_colors=("#ff0000", "#00ff00", "#0000ff")
        )


def test_save_theme_round_trip(
    generator: RainbowThemeGenerator, tmp_path: pathlib.Path
) -> None:
    style = generator.build_theme()
    path = generator.save_theme(style, name="vomit", directory=tmp_path)
    lines = path.read_text().splitlines()
    comments = [line for line in lines if line.startswith("//")]
    assert len(comments) == 2
    assert comments[0].startswith("// inputs: ")
    assert comments[1].startswith("// palette: ")
    inputs = json.loads(comments[0].removeprefix("// inputs: "))
    assert inputs["name"] == "vomit"
    assert inputs["colors"] == [hex_rgba(Color(c)) for c in INPUTS]
    assert "status_colors" not in inputs
    palette = json.loads(comments[1].removeprefix("// palette: "))
    assert HEX_RGBA.match(palette["keyword"])
    payload = json.loads("\n".join(line for line in lines if not line.startswith("//")))
    assert set(payload) == {"$schema", "name", "author", "themes"}
    assert payload["name"] == "vomit"
    (theme,) = payload["themes"]
    appearance = generator.theme_appearance().value
    assert theme["appearance"] == appearance
    assert theme["name"] == f"vomit-{appearance}"
    assert set(theme["style"]) == STYLE_KEYS
