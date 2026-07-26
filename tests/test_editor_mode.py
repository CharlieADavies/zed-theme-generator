"""Editor-mode behaviour, driven by fake $EDITOR scripts.

cyclopts resolves $EDITOR via shutil.which and runs `[editor, path]`, so each
fake editor is a single executable receiving the buffer path as argv[1]; it
must write the file, since the modified mtime is the save signal.
"""

import pathlib
import textwrap

import pytest

from zed_theme_generator.cli import app, edit_profile


def _install_editor(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    script = tmp_path / "fake-editor.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n" + textwrap.dedent(body)
    )
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))


def test_edit_save_and_generate(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful session writes the buffer verbatim and builds the theme."""
    _install_editor(
        themes_sandbox,
        monkeypatch,
        """\
        text = path.read_text().replace('"my-theme"', '"edited"')
        text += "# my own comment\\n"
        path.write_text(text)
        """,
    )
    profile_path = edit_profile("rainbow")
    assert profile_path == themes_sandbox / "profiles" / "edited.toml"
    saved = profile_path.read_text()
    assert 'name = "edited"' in saved
    assert "# my own comment" in saved
    assert (themes_sandbox / "themes" / "edited.json").is_file()


def test_invalid_then_fixed_retries_with_banner(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid save re-opens the editor with the error prepended."""
    counter = themes_sandbox / "count"
    saw_banner = themes_sandbox / "saw_banner"
    _install_editor(
        themes_sandbox,
        monkeypatch,
        f"""\
        counter = pathlib.Path({str(counter)!r})
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        if n == 0:
            path.write_text(
                'generator = "rainbow"\\n[inputs]\\nname = "x"\\ncolors = ["#ff004d"]\\n'
            )
        else:
            pathlib.Path({str(saw_banner)!r}).write_text(
                "yes" if path.read_text().startswith("#!! ") else "no"
            )
            path.write_text(
                'generator = "rainbow"\\n[inputs]\\nname = "fixed"\\n'
                'colors = ["#ff004d", "#ffa300"]\\n'
            )
        """,
    )
    profile_path = edit_profile("rainbow")
    assert counter.read_text() == "2"
    assert saw_banner.read_text() == "yes"
    assert profile_path.name == "fixed.toml"
    assert "#!!" not in profile_path.read_text()
    assert (themes_sandbox / "themes" / "fixed.json").is_file()


def test_unchanged_retry_aborts(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saving a retry round without changes stops the loop and fails loudly."""
    counter = themes_sandbox / "count"
    _install_editor(
        themes_sandbox,
        monkeypatch,
        f"""\
        counter = pathlib.Path({str(counter)!r})
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        if n == 0:
            path.write_text(
                'generator = "rainbow"\\n[inputs]\\nname = "x"\\ncolors = ["#ff004d"]\\n'
            )
        else:
            path.write_text(path.read_text())
        """,
    )
    with pytest.raises(SystemExit):
        edit_profile("rainbow")
    assert counter.read_text() == "2"
    assert not (themes_sandbox / "profiles").exists()


def test_editor_nonzero_exit_fails(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_editor(themes_sandbox, monkeypatch, "sys.exit(1)\n")
    with pytest.raises(SystemExit):
        edit_profile("rainbow")
    assert not (themes_sandbox / "profiles").exists()


def test_unknown_generator_fails_before_editor(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EDITOR", raising=False)
    with pytest.raises(SystemExit):
        edit_profile("sparkle")


def test_editor_mode_dispatch(
    themes_sandbox: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ztg --editor rainbow` reaches edit_profile through the default action."""
    _install_editor(
        themes_sandbox,
        monkeypatch,
        "path.write_text(path.read_text().replace('\"my-theme\"', '\"dispatched\"'))\n",
    )
    app(["--editor", "rainbow"], result_action="return_value", exit_on_error=False)
    assert (themes_sandbox / "profiles" / "dispatched.toml").is_file()
    assert (themes_sandbox / "themes" / "dispatched.json").is_file()
