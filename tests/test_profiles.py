"""File-mode behaviour: template round-trips, loud failures, TOML rendering."""

import json
import pathlib
import re
import tomllib
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from support import oklch_hex

from zed_theme_generator import HARMONY_TO_COLORAIDE
from zed_theme_generator.cli import (
    GENERATORS,
    app,
    parse_profile_document,
    run_profile_path,
)
from zed_theme_generator.schemas import (
    HarmonicInputs,
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
def test_template_round_trip(generator_name: str, themes_sandbox: pathlib.Path) -> None:
    """Every generator's editor template is a valid, generating profile as-is."""
    cls = GENERATORS[generator_name]
    profile = themes_sandbox / "profile.toml"
    profile.write_text(render_template(generator_name, cls.inputs_spec, cls.summary))
    theme_path = run_profile_path(profile, directory=themes_sandbox)
    payload = _theme_payload(theme_path)
    assert theme_path.name == "my-theme.json"
    assert payload["name"] == "my-theme"


def test_full_rainbow_profile(themes_sandbox: pathlib.Path) -> None:
    """Explicit background and status colours land verbatim in the inputs."""
    profile = themes_sandbox / "vomit.toml"
    profile.write_text(
        'generator = "rainbow"\n'
        "[inputs]\n"
        'name = "vomit"\n'
        'colors = ["#ff004d", "#ffa300", "#00e436"]\n'
        'background = "#101018"\n'
        'status_colors = ["#ff3860", "#ffdd57", "#23d160", "#209cee"]\n'
    )
    theme_path = run_profile_path(profile, directory=themes_sandbox)
    comments = [
        line
        for line in theme_path.read_text().splitlines()
        if line.startswith("// inputs: ")
    ]
    inputs = json.loads(comments[0].removeprefix("// inputs: "))
    assert inputs["name"] == "vomit"
    assert inputs["background"] == "#101018ff"


def test_render_profile_round_trip(themes_sandbox: pathlib.Path) -> None:
    """A rendered profile parses back and generates the same-named theme."""
    inputs = RainbowInputs(name="probe", colors=("#ff004d", "#ffa300"))
    profile = themes_sandbox / "probe.toml"
    profile.write_text(render_profile("rainbow", inputs))
    theme_path = run_profile_path(profile, directory=themes_sandbox)
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


_PROBE = 'generator = "rainbow"\n\n[inputs]\nname = "probe"\ncolors = ["#ff004d", "#ffa300"]\n'


def _dispatch(profile: pathlib.Path) -> None:
    app(["-f", str(profile)], result_action="return_value", exit_on_error=False)


def test_profile_rerun_raises_until_opt_in(
    themes_sandbox: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rerun collides cleanly once; the recorded opt-in makes reruns durable."""
    profile = themes_sandbox / "probe.toml"
    profile.write_text(_PROBE)
    _dispatch(profile)
    with pytest.raises(SystemExit):
        _dispatch(profile)
    err = capsys.readouterr().err
    assert "probe.json already exists" in err
    assert 'if_exists = "overwrite"' in err
    profile.write_text('if_exists = "overwrite"\n' + _PROBE)
    _dispatch(profile)
    _dispatch(profile)
    assert (themes_sandbox / "themes" / "probe.json").is_file()


def test_file_mode_copies_outside_source_verbatim(
    themes_sandbox: pathlib.Path,
) -> None:
    """A profile run from outside profiles/ is copied there byte-identically."""
    source = themes_sandbox / "elsewhere.toml"
    text = "# hand-written comment\n" + _PROBE
    source.write_text(text)
    run_profile_path(source)
    assert (themes_sandbox / "profiles" / "probe.toml").read_text() == text


def test_file_mode_inside_profiles_never_rewrites(
    themes_sandbox: pathlib.Path,
) -> None:
    """A profile already inside profiles/ is left byte-identical, not re-saved."""
    profiles = themes_sandbox / "profiles"
    profiles.mkdir()
    source = profiles / "probe.toml"
    text = '# hand-written comment\nif_exists = "overwrite"\n' + _PROBE
    source.write_text(text)
    run_profile_path(source)
    assert source.read_text() == text
    assert [p.name for p in profiles.iterdir()] == ["probe.toml"]


def test_register_collision_preflight_writes_nothing(
    themes_sandbox: pathlib.Path,
) -> None:
    """A register collision is caught before any file is written."""
    zed = themes_sandbox / "zed-themes"
    zed.mkdir()
    (zed / "probe.json").write_text("registered")
    profile = themes_sandbox / "probe.toml"
    profile.write_text("register = true\n" + _PROBE)
    with pytest.raises(FileExistsError, match="already exists"):
        run_profile_path(profile)
    assert (zed / "probe.json").read_text() == "registered"
    assert not (themes_sandbox / "themes").exists()
    assert not (themes_sandbox / "profiles").exists()


def test_render_profile_records_overwrite_only() -> None:
    """The sticky opt-in is emitted whenever set and omitted on the default."""
    inputs = RainbowInputs(name="probe", colors=("#ff004d", "#ffa300"))
    sticky = tomllib.loads(render_profile("rainbow", inputs, if_exists="overwrite"))
    assert sticky["if_exists"] == "overwrite"
    plain = tomllib.loads(render_profile("rainbow", inputs))
    assert "if_exists" not in plain


_RAINBOW_OK = 'name = "x"\ncolors = ["#ff004d", "#ffa300"]'
_HARMONIC_OK = (
    'name = "x"\nbackground = "#0a1022"\nforeground = "#ffe3f3"\naccent = "#ee7ec6"'
)
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
    pytest.param(
        f'generator = "harmonic"\n[inputs]\n{_HARMONIC_OK}\nui_accent_mix = 0.55',
        "ui_accent_mix",
        id="legacy-inputs-key",
    ),
    pytest.param(
        f'generator = "harmonic"\n[inputs]\n{_HARMONIC_OK}\naccent_mix = 250',
        "accent_mix",
        id="accent-mix-out-of-range",
    ),
    pytest.param(
        f'generator = "harmonic"\n[inputs]\n{_HARMONIC_OK}\nminimum_bg_contrast = 30',
        "minimum_bg_contrast",
        id="contrast-out-of-range",
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


def test_legacy_key_error_names_replacement(tmp_path: pathlib.Path) -> None:
    """An old-scale knob fails loudly with the new name and rescale named."""
    profile = tmp_path / "old.toml"
    profile.write_text(
        f'generator = "harmonic"\n[inputs]\n{_HARMONIC_OK}\nui_accent_mix = 0.55\n'
    )
    with pytest.raises(ProfileError) as excinfo:
        run_profile_path(profile, directory=tmp_path)
    message = str(excinfo.value)
    assert "ui_accent_mix" in message
    assert "renamed to accent_mix" in message
    assert "multiply the old 0-1 value by 100" in message


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


# --- auto-saved profiles reproduce their run -------------------------------------

_names = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,20}", fullmatch=True)
_css = oklch_hex(lightness=(0.05, 0.985))
_percent = st.floats(min_value=0.0, max_value=100.0)
_ratio = st.floats(min_value=1.0, max_value=21.0)


@st.composite
def _generator_inputs(draw: st.DrawFn) -> tuple[str, object]:
    """A generator name with a valid, fully-drawn inputs spec instance."""
    generator_name = draw(st.sampled_from(sorted(GENERATORS)))
    if generator_name == "rainbow":
        return generator_name, RainbowInputs(
            name=draw(_names),
            colors=tuple(draw(st.lists(_css, min_size=2, max_size=6))),
            background=draw(st.none() | _css),
            status_colors=draw(st.none() | st.tuples(_css, _css, _css, _css)),
        )
    return generator_name, HarmonicInputs(
        name=draw(_names),
        background=draw(_css),
        foreground=draw(_css),
        accent=draw(_css),
        minimum_bg_contrast=draw(_ratio),
        syntax_spread=draw(_percent),
        harmony_type=draw(st.sampled_from(sorted(HARMONY_TO_COLORAIDE))),
        accent_mix=draw(_percent),
        surface_blend=draw(_percent),
        border_blend=draw(_percent),
    )


@given(
    pair=_generator_inputs(),
    register=st.booleans(),
    if_exists=st.sampled_from(["raise", "overwrite"]),
)
def test_render_profile_round_trips_any_inputs(
    pair: tuple[str, object],
    register: bool,
    if_exists: Literal["raise", "overwrite"],
) -> None:
    """An auto-saved profile reproduces exactly the run that produced it.

    render_profile -> TOML -> parse_profile_document is the identity on the
    inputs spec and run options for every valid input, so a rerun of the
    recorded profile is the recorded run. The sticky "overwrite" opt-in
    survives the trip; the "raise" default is left out of the document.
    """
    generator_name, inputs = pair
    text = render_profile(
        generator_name, inputs, register=register, if_exists=if_exists
    )
    document = tomllib.loads(text)
    if if_exists == "overwrite":
        assert document["if_exists"] == "overwrite"
    else:
        assert "if_exists" not in document
    parsed = parse_profile_document(document, source="round-trip")
    assert parsed.generator_cls is GENERATORS[generator_name]
    assert parsed.inputs == inputs
    assert parsed.register is register
    assert parsed.if_exists == if_exists
