"""File-mode behaviour: template round-trips, loud failures, TOML rendering."""

import json
import pathlib
import re
import tomllib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from zed_theme_generator.cli import GENERATORS, app, run_profile_path
from zed_theme_generator.schemas import (
    ProfileError,
    RainbowInputs,
    render_profile,
    render_template,
    toml_value,
)


def _theme_payload(theme_path: pathlib.Path) -> dict[str, object]:
    lines = theme_path.read_text().splitlines()
    return json.loads("\n".join(line for line in lines if not line.startswith("//")))


@pytest.mark.parametrize("generator_name", list(GENERATORS))
def test_template_round_trip(generator_name: str, tmp_path: pathlib.Path) -> None:
    """Every generator's editor template is a valid, generating profile as-is."""
    cls = GENERATORS[generator_name]
    profile = tmp_path / "profile.toml"
    profile.write_text(render_template(generator_name, cls.inputs_spec, cls.summary))
    theme_path = run_profile_path(profile, directory=tmp_path)
    payload = _theme_payload(theme_path)
    assert theme_path.name == "my-theme.json"
    assert payload["name"] == "my-theme"


def test_full_rainbow_profile(tmp_path: pathlib.Path) -> None:
    """Explicit background and status colours land verbatim in the inputs."""
    profile = tmp_path / "vomit.toml"
    profile.write_text(
        'generator = "rainbow"\n'
        "[inputs]\n"
        'name = "vomit"\n'
        'colors = ["#ff004d", "#ffa300", "#00e436"]\n'
        'background = "#101018"\n'
        'status_colors = ["#ff3860", "#ffdd57", "#23d160", "#209cee"]\n'
    )
    theme_path = run_profile_path(profile, directory=tmp_path)
    comments = [
        line
        for line in theme_path.read_text().splitlines()
        if line.startswith("// inputs: ")
    ]
    inputs = json.loads(comments[0].removeprefix("// inputs: "))
    assert inputs["name"] == "vomit"
    assert inputs["background"] == "#101018ff"


def test_render_profile_round_trip(tmp_path: pathlib.Path) -> None:
    """A rendered profile parses back and generates the same-named theme."""
    inputs = RainbowInputs(name="probe", colors=("#ff004d", "#ffa300"))
    profile = tmp_path / "probe.toml"
    profile.write_text(render_profile("rainbow", inputs))
    theme_path = run_profile_path(profile, directory=tmp_path)
    assert theme_path.name == "probe.json"


def test_file_mode_dispatch(themes_sandbox: pathlib.Path) -> None:
    """`ztg -f profile.toml` runs the profile end-to-end through the app."""
    profile = themes_sandbox / "probe.toml"
    profile.write_text(
        'generator = "rainbow"\n[inputs]\nname = "probe"\ncolors = ["#ff004d", "#ffa300"]\n'
    )
    app(
        ["-f", str(profile)],
        result_action="return_value",
        exit_on_error=False,
    )
    assert (themes_sandbox / "themes" / "probe.json").is_file()


_RAINBOW_OK = 'name = "x"\ncolors = ["#ff004d", "#ffa300"]'
INVALID_PROFILES = [
    pytest.param("", "generator", id="missing-generator-key"),
    pytest.param(
        f'generator = "sparkle"\n[inputs]\n{_RAINBOW_OK}',
        "sparkle",
        id="unknown-generator",
    ),
    pytest.param(
        f'generator = "rainbow"\nregster = true\n[inputs]\n{_RAINBOW_OK}',
        "regster",
        id="unknown-envelope-key",
    ),
    pytest.param(
        f'generator = "rainbow"\n[inputs]\n{_RAINBOW_OK}\ncolrs = []',
        "colrs",
        id="unknown-inputs-key",
    ),
    pytest.param(
        'generator = "rainbow"\n[inputs]\nname = "x"\ncolors = ["#ff004d", "nope"]',
        "nope",
        id="bad-colour",
    ),
    pytest.param(
        'generator = "rainbow"\n[inputs]\nname = "x"\ncolors = ["#ff004d"]',
        "colors",
        id="too-few-colours",
    ),
    pytest.param(
        f'generator = "rainbow"\n[inputs]\n{_RAINBOW_OK}\nstatus_colors = ["#ff3860"]',
        "status_colors",
        id="wrong-status-arity",
    ),
    pytest.param(
        'generator = "harmonic"\n[inputs]\nname = "x"',
        "background",
        id="missing-required-input",
    ),
    pytest.param(
        'generator = "rainbow"\n[inputs]\nname = "../evil"\ncolors = ["#ff004d", "#ffa300"]',
        "name",
        id="unsafe-name",
    ),
    pytest.param(
        'generator = "rainbow"\ninputs = 3',
        "inputs",
        id="inputs-not-a-table",
    ),
]


@pytest.mark.parametrize(("document", "culprit"), INVALID_PROFILES)
def test_invalid_profiles_fail_loudly(
    document: str, culprit: str, tmp_path: pathlib.Path
) -> None:
    profile = tmp_path / "bad.toml"
    profile.write_text(document)
    with pytest.raises(ProfileError, match=re.escape(culprit)):
        run_profile_path(profile, directory=tmp_path)


def test_missing_profile_file(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ProfileError, match="not found"):
        run_profile_path(tmp_path / "absent.toml", directory=tmp_path)


def test_bad_toml_syntax(tmp_path: pathlib.Path) -> None:
    profile = tmp_path / "bad.toml"
    profile.write_text('generator = "rainbow\n')
    with pytest.raises(ProfileError, match="invalid TOML"):
        run_profile_path(profile, directory=tmp_path)


_toml_scalars = st.one_of(
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=40),
)


@given(st.one_of(_toml_scalars, st.lists(st.text(max_size=20), max_size=5)))
def test_toml_value_round_trips(value: object) -> None:
    parsed = tomllib.loads(f"x = {toml_value(value)}")["x"]
    if isinstance(value, list):
        assert list(parsed) == value
    else:
        assert parsed == value
