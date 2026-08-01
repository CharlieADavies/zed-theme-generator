"""Wizard behaviour, driven by scripted answers through the prompt shims.

ENTER stands for "press Enter" (accept the default / skip / finish); every
test asserts its script is fully consumed, so a vanished re-prompt or an
extra question fails loudly.
"""

import pathlib
import tomllib
from collections import deque

import pytest

from zed_theme_generator import cli

ENTER = None


class _Script:
    """Feeds scripted answers to _ask/_choose and booleans to _confirm."""

    def __init__(self, answers: list[str | None], confirms: list[bool]) -> None:
        self.answers = deque(answers)
        self.confirms = deque(confirms)

    def ask(self, prompt: str, *, default: str | None = None) -> str:
        value = self.answers.popleft()
        if value is ENTER:
            return default or ""
        return value

    def choose(
        self, prompt: str, choices: list[str], *, default: str | None = None
    ) -> str:
        value = self.answers.popleft()
        if value is ENTER:
            return default or ""
        return value

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        return self.confirms.popleft()

    def assert_consumed(self) -> None:
        assert not self.answers, f"unused answers: {list(self.answers)}"
        assert not self.confirms, f"unused confirms: {list(self.confirms)}"


def _run_wizard(
    monkeypatch: pytest.MonkeyPatch, script: _Script, tokens: list[str] | None = None
) -> None:
    monkeypatch.setattr(cli, "_ask", script.ask)
    monkeypatch.setattr(cli, "_choose", script.choose)
    monkeypatch.setattr(cli, "_confirm", script.confirm)
    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    cli.app(tokens or [], result_action="return_value", exit_on_error=False)


def test_harmonic_all_defaults(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting harmonic and accepting every default generates a theme."""
    script = _Script(
        answers=[
            "harmonic",  # generator selection
            "pinkish",  # name
            "#0a1022",  # background
            "#ffe3f3",  # foreground
            "#ee7ec6",  # accent
            ENTER,  # minimum_bg_contrast
            ENTER,  # syntax_spread
            ENTER,  # harmony_type (choice)
            ENTER,  # accent_mix
            ENTER,  # surface_blend
            ENTER,  # border_blend
        ],
        confirms=[True, False],  # generate, register
    )
    _run_wizard(monkeypatch, script)
    script.assert_consumed()
    assert (themes_sandbox / "themes" / "pinkish.json").is_file()
    # Every run auto-saves its resolved inputs; the default run records no
    # if_exists opt-in.
    profile = tomllib.loads((themes_sandbox / "profiles" / "pinkish.toml").read_text())
    assert profile["generator"] == "harmonic"
    assert "if_exists" not in profile


def test_rainbow_full_run_with_profile(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preselected rainbow: variadic colours, status colours, auto-saved profile."""
    script = _Script(
        answers=[
            "probe",  # name (generator preselected via --generator)
            "#ff004d",  # colors 1
            "#ffa300",  # colors 2
            ENTER,  # colors: finish
            ENTER,  # background: skip
            "#ff3860",  # status 1/4
            "#ffdd57",  # status 2/4
            "#23d160",  # status 3/4
            "#209cee",  # status 4/4
        ],
        confirms=[
            True,  # provide status colours
            True,  # generate
            False,  # register
        ],
    )
    _run_wizard(monkeypatch, script, ["--generator", "rainbow"])
    script.assert_consumed()
    assert (themes_sandbox / "themes" / "probe.json").is_file()
    profile = tomllib.loads((themes_sandbox / "profiles" / "probe.toml").read_text())
    assert profile["generator"] == "rainbow"
    assert profile["inputs"]["colors"] == ["#ff004d", "#ffa300"]
    assert profile["inputs"]["status_colors"] == [
        "#ff3860",
        "#ffdd57",
        "#23d160",
        "#209cee",
    ]


def test_invalid_answer_reprompts(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad colour is rejected and the corrected answer is consumed."""
    script = _Script(
        answers=[
            "corrected",  # name
            "oops",  # background: invalid, re-prompted
            "#0a1022",  # background: corrected
            "#ffe3f3",  # foreground
            "#ee7ec6",  # accent
            ENTER,
            ENTER,
            ENTER,
            ENTER,
            ENTER,
            ENTER,
        ],
        confirms=[True, False],
    )
    _run_wizard(monkeypatch, script, ["--generator", "harmonic"])
    script.assert_consumed()
    assert (themes_sandbox / "themes" / "corrected.json").is_file()


def test_out_of_range_answer_reprompts(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-range knob is rejected by the cyclopts validator and re-asked."""
    script = _Script(
        answers=[
            "ranged",  # name
            "#0a1022",  # background
            "#ffe3f3",  # foreground
            "#ee7ec6",  # accent
            ENTER,  # minimum_bg_contrast
            ENTER,  # syntax_spread
            ENTER,  # harmony_type (choice)
            "250",  # accent_mix: out of range, re-prompted
            "55",  # accent_mix: corrected
            ENTER,  # surface_blend
            ENTER,  # border_blend
        ],
        confirms=[True, False],
    )
    _run_wizard(monkeypatch, script, ["--generator", "harmonic"])
    script.assert_consumed()
    assert (themes_sandbox / "themes" / "ranged.json").is_file()


def test_variadic_too_short_restarts(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finishing the colour list below the minimum restarts the list."""
    script = _Script(
        answers=[
            "shorty",  # name
            "#ff004d",  # colors 1
            ENTER,  # finish: too short, restarts
            "#ff004d",  # colors 1 again
            "#ffa300",  # colors 2
            ENTER,  # finish: valid
            ENTER,  # background: skip
        ],
        confirms=[False, True, False],  # status, generate, register
    )
    _run_wizard(monkeypatch, script, ["--generator", "rainbow"])
    script.assert_consumed()
    assert (themes_sandbox / "themes" / "shorty.json").is_file()


def test_generation_constraint_fails_cleanly(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A light background handed to harmonic exits non-zero, no traceback."""
    script = _Script(
        answers=[
            "bright",
            "#ffffff",  # valid colour, wrong side for dark generation
            "#000000",
            "#ff0000",
            ENTER,
            ENTER,
            ENTER,
            ENTER,
            ENTER,
            ENTER,
        ],
        confirms=[True, False],  # generate, register
    )
    with pytest.raises(SystemExit):
        _run_wizard(monkeypatch, script, ["--generator", "harmonic"])
    assert not (themes_sandbox / "themes").exists()
    # The profile is saved only after a successful generation.
    assert not (themes_sandbox / "profiles").exists()


_HARMONIC_ANSWERS: list[str | None] = [
    "pinkish",  # name
    "#0a1022",  # background
    "#ffe3f3",  # foreground
    "#ee7ec6",  # accent
    ENTER,  # minimum_bg_contrast
    ENTER,  # syntax_spread
    ENTER,  # harmony_type (choice)
    ENTER,  # accent_mix
    ENTER,  # surface_blend
    ENTER,  # border_blend
]


def test_collision_confirm_overwrites_and_records_opt_in(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirming the overwrite regenerates and records the sticky opt-in."""
    themes = themes_sandbox / "themes"
    themes.mkdir()
    (themes / "pinkish.json").write_text("{}")
    script = _Script(
        answers=list(_HARMONIC_ANSWERS),
        confirms=[True, False, True],  # generate, register, overwrite collision
    )
    _run_wizard(monkeypatch, script, ["--generator", "harmonic"])
    script.assert_consumed()
    assert (themes / "pinkish.json").read_text() != "{}"
    profile = tomllib.loads((themes_sandbox / "profiles" / "pinkish.toml").read_text())
    assert profile["if_exists"] == "overwrite"


def test_collision_decline_aborts_untouched(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declining the overwrite aborts cleanly with the original theme intact."""
    themes = themes_sandbox / "themes"
    themes.mkdir()
    (themes / "pinkish.json").write_text("{}")
    script = _Script(
        answers=list(_HARMONIC_ANSWERS),
        confirms=[True, False, False],  # generate, register, overwrite collision
    )
    _run_wizard(monkeypatch, script, ["--generator", "harmonic"])
    script.assert_consumed()
    assert (themes / "pinkish.json").read_text() == "{}"
    assert not (themes_sandbox / "profiles").exists()


def test_no_save_profile_flag_suppresses_write(themes_sandbox: pathlib.Path) -> None:
    """--no-save-profile on a typed command skips the profile auto-save."""
    cli.app(
        [
            "harmonic",
            "--name",
            "quiet",
            "--background",
            "#0a1022",
            "--foreground",
            "#ffe3f3",
            "--accent",
            "#ee7ec6",
            "--no-save-profile",
        ],
        result_action="return_value",
        exit_on_error=False,
    )
    assert (themes_sandbox / "themes" / "quiet.json").is_file()
    assert not (themes_sandbox / "profiles").exists()


def test_color_swatch_renders(capsys: pytest.CaptureFixture[str]) -> None:
    """The wizard's colour reference strip prints its heading and chart link."""
    cli._print_color_swatch()
    captured = capsys.readouterr()
    assert "Colour reference" in captured.out
    assert "https://rich.readthedocs.io/en/stable/appendix/colors.html" in captured.out


def test_no_tty_prints_help(
    themes_sandbox: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a TTY the wizard never starts; help is shown instead."""
    cli.app([], result_action="return_value", exit_on_error=False)
    captured = capsys.readouterr()
    assert "Usage" in captured.out
    assert not (themes_sandbox / "themes").exists()
