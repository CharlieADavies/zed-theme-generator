"""Hypothesis profiles and shared fixtures."""

import os
import pathlib

import pytest
from hypothesis import HealthCheck, Phase, settings

# A full theme build costs ~80ms, so example counts are budgeted rather than
# left on the Hypothesis defaults.
settings.register_profile(
    "dev",
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
# Debug: surface raw counterexamples without paying for the shrink loop.
settings.register_profile(
    "noshrink",
    max_examples=50,
    deadline=None,
    phases=(Phase.explicit, Phase.reuse, Phase.generate),
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "thorough",
    max_examples=250,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture
def themes_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> pathlib.Path:
    """Redirect every on-disk side effect into tmp_path.

    Dispatching a command for real would otherwise write the repo's themes/
    and rewrite extension.toml (save_theme compares against THEMES_DIR and
    then calls write_extension_toml). cli.py imports the path constants by
    value, so both modules are patched together.
    """
    from zed_theme_generator import cli, generator

    monkeypatch.setattr(generator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(generator, "THEMES_DIR", tmp_path / "themes")
    monkeypatch.setattr(generator, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(generator, "ZED_THEMES_DIR", tmp_path / "zed-themes")
    monkeypatch.setattr(cli, "THEMES_DIR", tmp_path / "themes")
    monkeypatch.setattr(cli, "ZED_THEMES_DIR", tmp_path / "zed-themes")
    monkeypatch.setattr(cli, "PROFILES_DIR", tmp_path / "profiles")
    return tmp_path
