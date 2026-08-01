"""The saved theme file format: provenance comments plus the family payload."""

import json
import pathlib
from collections.abc import Callable

import pytest
from support import NEON

from zed_theme_generator import HarmonicPaletteThemeGenerator, ThemeGenerator
from zed_theme_generator.light import HarmonicLightPaletteThemeGenerator
from zed_theme_generator.rainbow import RainbowThemeGenerator
from zed_theme_generator.schemas import HarmonicInputs, RainbowInputs


def _pinkish() -> ThemeGenerator:
    return HarmonicPaletteThemeGenerator.from_inputs(
        HarmonicInputs(
            name="pinkish",
            background="#0a1022",
            foreground="#ffe3f3",
            accent="#ee7ec6",
        )
    )


def _rosewater() -> ThemeGenerator:
    return HarmonicLightPaletteThemeGenerator.from_inputs(
        HarmonicInputs(
            name="rosewater",
            background="#fdf4f8",
            foreground="#2b1930",
            accent="#c02579",
        )
    )


def _vomit() -> ThemeGenerator:
    return RainbowThemeGenerator.from_inputs(RainbowInputs(name="vomit", colors=NEON))


@pytest.mark.parametrize(
    ("name", "make"),
    [("pinkish", _pinkish), ("rosewater", _rosewater), ("vomit", _vomit)],
)
def test_save_theme_round_trip(
    name: str, make: Callable[[], ThemeGenerator], tmp_path: pathlib.Path
) -> None:
    generator = make()
    # Always save to tmp_path: the default directory is the repo themes/ dir
    # and rewrites extension.toml.
    path = generator.save_theme(generator.build_theme(), name=name, directory=tmp_path)
    lines = path.read_text().splitlines()
    comments = [line for line in lines if line.startswith("//")]
    assert len(comments) == 2
    assert comments[0].startswith("// inputs: ")
    assert comments[1].startswith("// palette: ")
    inputs = json.loads(comments[0].removeprefix("// inputs: "))
    assert inputs["name"] == name
    payload = json.loads("\n".join(line for line in lines if not line.startswith("//")))
    assert set(payload) == {"$schema", "name", "author", "themes"}
    assert payload["name"] == name
    (theme,) = payload["themes"]
    appearance = generator.theme_appearance().value
    assert theme["appearance"] == appearance
    assert theme["name"] == name


def test_save_theme_refuses_existing_by_default(tmp_path: pathlib.Path) -> None:
    """A second save of the same name raises instead of clobbering."""
    generator = _pinkish()
    style = generator.build_theme()
    path = generator.save_theme(style, name="pinkish", directory=tmp_path)
    before = path.read_text()
    with pytest.raises(FileExistsError, match="already exists"):
        generator.save_theme(style, name="pinkish", directory=tmp_path)
    assert path.read_text() == before


def test_save_theme_overwrite_opt_in(tmp_path: pathlib.Path) -> None:
    """if_exists="overwrite" replaces the existing file."""
    generator = _pinkish()
    style = generator.build_theme()
    first = generator.save_theme(style, name="pinkish", directory=tmp_path)
    second = generator.save_theme(
        style, name="pinkish", directory=tmp_path, if_exists="overwrite"
    )
    assert first == second
    assert second.is_file()
