"""All CLI aspects: the cyclopts app, its commands, and the entry modes."""

import inspect
import os
import pathlib
import shlex
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from typing import Annotated, Any, Literal, NamedTuple, NoReturn

import cyclopts
from cyclopts import (
    App,
    Argument,
    EditorDidNotSaveError,
    EditorError,
    EditorNotFoundError,
    Parameter,
    Token,
)
from cyclopts.exceptions import CycloptsError
from pydantic import TypeAdapter, ValidationError
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from zed_theme_generator.generator import (
    PROFILES_DIR,
    THEMES_DIR,
    ZED_THEMES_DIR,
    HarmonicPaletteThemeGenerator,
    ThemeGenerator,
)
from zed_theme_generator.light import HarmonicLightPaletteThemeGenerator
from zed_theme_generator.rainbow import RainbowThemeGenerator
from zed_theme_generator.schemas import (
    HarmonicInputs,
    Profile,
    ProfileError,
    RainbowInputs,
    format_validation_error,
    render_profile,
    render_template,
)

app = App(name="ztg")

# Every generator the CLI can drive, keyed by generator name (== command
# name); the wizard, file mode, and list-generators all read this registry.
GENERATORS: dict[str, type[ThemeGenerator]] = {
    cls.generator_name: cls
    for cls in (
        HarmonicPaletteThemeGenerator,
        HarmonicLightPaletteThemeGenerator,
        RainbowThemeGenerator,
    )
}

type _Register = Annotated[
    bool, Parameter(help="Also copy the generated theme into ~/.config/zed/themes.")
]
type _IfExists = Annotated[
    Literal["overwrite", "raise"],
    Parameter(help="What to do when registering over an existing theme file."),
]


def run_generator(
    cls: type[ThemeGenerator],
    inputs: Any,
    *,
    register: bool = False,
    if_exists: Literal["overwrite", "raise"] = "overwrite",
    directory: pathlib.Path | None = None,
) -> pathlib.Path:
    """Build, save, and optionally register a theme from a validated inputs spec.

    The shared runner behind every mode: typed commands, profile files, the
    editor, and the wizard all end up here.
    """
    generator = cls.from_inputs(inputs)
    path = generator.save_theme(
        generator.build_theme(), name=inputs.name, directory=directory
    )
    print(f"Wrote {path}")
    if register:
        register_themes(inputs.name, if_exists)
    return path


@app.command
def harmonic(
    params: Annotated[HarmonicInputs, Parameter(name="*")],
    *,
    register: _Register = False,
    if_exists: _IfExists = "overwrite",
) -> None:
    """Generate a dark Zed theme using a harmonic colour palette."""
    run_generator(
        HarmonicPaletteThemeGenerator, params, register=register, if_exists=if_exists
    )


@app.command
def harmonic_light(
    params: Annotated[HarmonicInputs, Parameter(name="*")],
    *,
    register: _Register = False,
    if_exists: _IfExists = "overwrite",
) -> None:
    """Generate a light Zed theme using a harmonic colour palette.

    The background must be light (oklch lightness >= 0.5).
    """
    run_generator(
        HarmonicLightPaletteThemeGenerator,
        params,
        register=register,
        if_exists=if_exists,
    )


@app.command
def rainbow(
    params: Annotated[RainbowInputs, Parameter(name="*")],
    *,
    register: _Register = False,
    if_exists: _IfExists = "overwrite",
) -> None:
    """Generate a Zed theme from a weight-ordered colour list used verbatim.

    The theme's appearance follows the background's lightness.
    """
    run_generator(RainbowThemeGenerator, params, register=register, if_exists=if_exists)


@app.command
def register_themes(
    name: str, if_exists: Literal["overwrite", "raise"] = "raise"
) -> None:
    """Registers the theme in ~/.config/zed/themes"""
    source = THEMES_DIR / f"{name}.json"
    if not source.exists():
        raise FileNotFoundError(f"No generated theme at {source}; run generate first")
    ZED_THEMES_DIR.mkdir(parents=True, exist_ok=True)
    destination = ZED_THEMES_DIR / f"{name}.json"
    if destination.exists() and if_exists == "raise":
        raise FileExistsError(f"{destination} already exists (use overwrite)")
    shutil.copyfile(source, destination)
    print(f"Registered {destination}")


@app.command
def list_generators() -> None:
    """List all available theme generators"""
    for generator_name, generator in GENERATORS.items():
        print(f"{generator_name}: {generator.summary}")


# --- profile file mode ---------------------------------------------------------


class ParsedProfile(NamedTuple):
    """A validated profile document, ready to run."""

    generator_cls: type[ThemeGenerator]
    inputs: Any
    register: bool
    if_exists: Literal["overwrite", "raise"]


def fail(message: str) -> NoReturn:
    """Print a user-facing error to stderr and exit non-zero."""
    print(message, file=sys.stderr)
    raise SystemExit(1)


def load_profile_path(path: pathlib.Path) -> dict[str, object]:
    """Read a profile TOML from disk, failing loudly on absence or bad syntax."""
    if not path.is_file():
        raise ProfileError(f"profile not found: {path}")
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as err:
        raise ProfileError(f"invalid TOML in {path}: {err}") from None


def parse_profile_document(doc: Mapping[str, object], *, source: str) -> ParsedProfile:
    """Validate a profile document: envelope, generator, then generator inputs.

    The single validation path shared by `-f`, the editor retry loop, and the
    tests; every failure raises `ProfileError` naming the culprit.
    """
    try:
        profile = TypeAdapter(Profile).validate_python(doc)
    except ValidationError as err:
        raise ProfileError(format_validation_error(err, source=source)) from None
    generator_cls = GENERATORS.get(profile.generator)
    if generator_cls is None:
        known = ", ".join(GENERATORS)
        raise ProfileError(
            f"unknown generator {profile.generator!r} in {source}; "
            f"known generators: {known}"
        )
    try:
        inputs = TypeAdapter(generator_cls.inputs_spec).validate_python(profile.inputs)
    except ValidationError as err:
        raise ProfileError(
            format_validation_error(err, source=source, prefix="inputs.")
        ) from None
    return ParsedProfile(generator_cls, inputs, profile.register, profile.if_exists)


def run_profile_path(
    path: pathlib.Path, *, directory: pathlib.Path | None = None
) -> pathlib.Path:
    """Generate a theme from a profile TOML file."""
    parsed = parse_profile_document(load_profile_path(path), source=str(path))
    return run_generator(
        parsed.generator_cls,
        parsed.inputs,
        register=parsed.register,
        if_exists=parsed.if_exists,
        directory=directory,
    )


# --- default entry: wizard, -f, --editor ---------------------------------------


@app.default
def default_action(
    *,
    file: Annotated[
        pathlib.Path | None,
        Parameter(
            name=["--file", "-f"],
            help="Generate a theme from a profile TOML file.",
        ),
    ] = None,
    editor: Annotated[
        str | None,
        Parameter(
            help="Open $EDITOR on a prefilled profile for the named generator; "
            "on success the profile is saved to profiles/ and the theme built."
        ),
    ] = None,
    generator: Annotated[
        str | None, Parameter(help="Preselect the interactive wizard's generator.")
    ] = None,
) -> None:
    """Interactively build a theme (default), or run one of the file entry modes."""
    if file is not None and editor is not None:
        fail("choose one of --file / --editor")
    if file is not None:
        try:
            run_profile_path(file)
        except ProfileError as err:
            fail(str(err))
    elif editor is not None:
        edit_profile(editor)
    else:
        run_wizard(generator)


# Flip to False for a save-only editor mode (profiles run later via -f).
EDITOR_MODE_GENERATES = True
# Error banners re-injected into the editor buffer use this prefix, so they can
# be stripped on the next round without ever eating the user's own comments.
_ERROR_COMMENT_PREFIX = "#!! "


def _strip_error_comments(text: str) -> str:
    return "".join(
        line
        for line in text.splitlines(keepends=True)
        if not line.startswith(_ERROR_COMMENT_PREFIX)
    )


def _error_banner(problem: str) -> str:
    lines = [
        "ERROR — fix and save again, or quit without saving to abort:",
        *str(problem).splitlines(),
    ]
    return "".join(f"{_ERROR_COMMENT_PREFIX}{line}\n" for line in lines)


def edit_profile(generator_name: str) -> pathlib.Path:
    """Author a profile in $EDITOR, save it to profiles/, and build the theme.

    The buffer starts from the generator's template (defaults prefilled). An
    invalid save re-opens the editor with the error prepended as `#!!` comment
    lines; quitting without saving — or saving a retry round unchanged —
    aborts. The validated buffer is written to profiles/<name>.toml verbatim,
    so the user's own comments survive.
    """
    generator_cls = GENERATORS.get(generator_name)
    if generator_cls is None:
        known = ", ".join(GENERATORS)
        fail(f"unknown generator {generator_name!r}; known generators: {known}")
    text = render_template(
        generator_name, generator_cls.inputs_spec, generator_cls.summary
    )
    # The path only gives the editor a .toml suffix to highlight; cyclopts
    # always deletes it, so the buffer text is what carries the result.
    scratch = (
        pathlib.Path(tempfile.gettempdir()) / f"ztg-{generator_name}-{os.getpid()}.toml"
    )
    while True:
        try:
            edited = cyclopts.edit(text, path=scratch, required=False)
        except EditorNotFoundError:
            fail("no editor found; set $EDITOR")
        except EditorDidNotSaveError:
            fail("editor closed without saving; aborted, no profile written")
        except EditorError as err:
            fail(f"editor failed: {err}")
        body = _strip_error_comments(edited)
        try:
            parsed = parse_profile_document(tomllib.loads(body), source="editor buffer")
            break
        except tomllib.TOMLDecodeError as err:
            problem = f"invalid TOML: {err}"
        except ProfileError as err:
            problem = str(err)
        if edited == text:
            # A retry round saved with no changes: stop looping, report loudly.
            fail(problem)
        text = _error_banner(problem) + body
    if parsed.generator_cls is not generator_cls:
        fail(
            f"profile generator changed to {parsed.generator_cls.generator_name!r}; "
            f"run ztg --editor {parsed.generator_cls.generator_name} instead"
        )
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profile_path = PROFILES_DIR / f"{parsed.inputs.name}.toml"
    profile_path.write_text(body)
    print(f"Saved profile {profile_path}")
    if EDITOR_MODE_GENERATES:
        run_generator(
            parsed.generator_cls,
            parsed.inputs,
            register=parsed.register,
            if_exists=parsed.if_exists,
        )
    return profile_path


# --- interactive wizard --------------------------------------------------------

_console = Console()
# Command-action flags handled at the confirmation step, not as questions.
_WIZARD_SKIP = frozenset({"--register", "--if-exists"})


def _ask(prompt: str, *, default: str | None = None) -> str:
    """Prompt shim; tests monkeypatch these three instead of rich internals."""
    return Prompt.ask(prompt, default=default, console=_console) or ""


def _confirm(prompt: str, *, default: bool = False) -> bool:
    return Confirm.ask(prompt, default=default, console=_console)


def _choose(prompt: str, choices: list[str], *, default: str | None = None) -> str:
    return Prompt.ask(prompt, choices=choices, default=default, console=_console) or ""


def _stdin_isatty() -> bool:
    return sys.stdin.isatty()


class _Answer(NamedTuple):
    """One wizard answer: the cyclopts keyword and the values the user gave."""

    name: str
    values: list[str]  # empty: default accepted / optional skipped
    grouped: bool = False  # fixed-N tuples emit one keyword followed by N values


def run_wizard(preselect: str | None) -> None:
    """Walk through generator selection and inputs interactively."""
    if not _stdin_isatty():
        app.help_print()
        return
    try:
        _wizard(preselect)
    except KeyboardInterrupt:
        print("\nAborted.")


def _wizard(preselect: str | None) -> None:
    generator_name = _select_generator(preselect)
    subapp = app[generator_name]
    collection = subapp.assemble_argument_collection(parse_docstring=True)
    answers = [
        _prompt_argument(subapp, arg)
        for arg in collection.filter_by(show=True)
        if arg.name not in _WIZARD_SKIP
    ]
    tokens = _build_tokens(generator_name, answers)
    _print_summary(answers, tokens)
    if not _confirm("Generate theme?", default=True):
        print("Aborted.")
        return
    if _confirm("Register into ~/.config/zed/themes?", default=False):
        tokens.append("--register")
    try:
        app(tokens)
    except ValueError as err:
        # Generation-time constraint failures (e.g. a dark background handed
        # to the light generator) arrive as plain ValueErrors; report cleanly.
        fail(str(err))
    if _confirm("Save these inputs as a reusable profile?", default=False):
        _save_wizard_profile(generator_name, answers)


def _select_generator(preselect: str | None) -> str:
    names = list(GENERATORS)
    if preselect is not None:
        if preselect in GENERATORS:
            return preselect
        fail(f"unknown generator {preselect!r}; known generators: {', '.join(names)}")
    _console.print("[bold]Available generators[/bold]")
    for index, name in enumerate(names, start=1):
        _console.print(f"  {index}. [bold]{name}[/bold] — {GENERATORS[name].summary}")
    while True:
        raw = _ask(f"Generator (1-{len(names)} or name)")
        if raw in GENERATORS:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        _console.print(f"[red]choose one of: {', '.join(names)}[/red]")


def _prompt_argument(subapp: App, arg: Argument) -> _Answer:
    """Ask for one argument, dispatching on its shape (scalar/variadic/fixed-N)."""
    if arg.parameter.help:
        _console.print(f"[dim]{arg.parameter.help}[/dim]")
    label = arg.name.removeprefix("--")
    per_element, consume_all = arg.token_count()
    if consume_all:
        return _Answer(arg.name, _prompt_variadic(subapp, arg, label))
    if per_element > 1:
        values = _prompt_fixed(subapp, arg, label, per_element)
        return _Answer(arg.name, values, grouped=True)
    return _Answer(arg.name, _prompt_scalar(subapp, arg, label))


def _validate_answer(subapp: App, name: str, values: list[str]) -> str | None:
    """Run cyclopts conversion+validation for one argument; None when valid.

    Assembles a fresh collection every attempt: an Argument caches its
    converted value even after a failed validation.
    """
    collection = subapp.assemble_argument_collection()
    arg = next(a for a in collection if a.name == name)
    for index, value in enumerate(values):
        arg.append(Token(keyword=name, value=value, source="wizard", index=index))
    try:
        arg.convert_and_validate()
    except (CycloptsError, ValueError) as err:
        message = str(err)
        # CycloptsError.__str__ leads with the class name; drop it for prompts.
        return message.removeprefix(f"{type(err).__name__}\n")
    return None


def _prompt_scalar(subapp: App, arg: Argument, label: str) -> list[str]:
    choices = arg.get_choices()
    default = arg.field_info.default
    has_default = default is not inspect.Parameter.empty
    while True:
        if choices:
            raw = _choose(
                label,
                [str(choice) for choice in choices],
                default=str(default) if has_default else None,
            )
        elif has_default and default is None:
            raw = _ask(f"{label} (Enter to skip)", default="")
            if raw == "":
                return []
        elif has_default:
            raw = _ask(label, default=str(default))
        else:
            raw = _ask(label)
            if raw == "":
                _console.print("[red]a value is required[/red]")
                continue
        if has_default and default is not None and raw == str(default):
            return []  # default accepted: emit nothing, cyclopts applies it
        error = _validate_answer(subapp, arg.name, [raw])
        if error is None:
            return [raw]
        _console.print(f"[red]{error}[/red]")


def _prompt_variadic(subapp: App, arg: Argument, label: str) -> list[str]:
    values: list[str] = []
    while True:
        raw = _ask(f"{label} {len(values) + 1} (blank to finish)", default="")
        if raw:
            values.append(raw)
            continue
        if not values:
            _console.print("[red]at least one value is required[/red]")
            continue
        error = _validate_answer(subapp, arg.name, values)
        if error is None:
            return values
        _console.print(f"[red]{error}[/red]")
        _console.print(f"[yellow]restarting {label} from the first value[/yellow]")
        values = []


def _prompt_fixed(subapp: App, arg: Argument, label: str, count: int) -> list[str]:
    if not arg.required and not _confirm(f"Provide {label} explicitly?", default=False):
        return []
    while True:
        values: list[str] = []
        for index in range(count):
            while True:
                raw = _ask(f"{label} {index + 1}/{count}")
                if raw:
                    break
                _console.print("[red]a value is required[/red]")
            values.append(raw)
        error = _validate_answer(subapp, arg.name, values)
        if error is None:
            return values
        _console.print(f"[red]{error}[/red]")


def _build_tokens(command: str, answers: list[_Answer]) -> list[str]:
    tokens = [command]
    for answer in answers:
        if not answer.values:
            continue
        if answer.grouped:
            tokens.append(answer.name)
            tokens.extend(answer.values)
        else:
            for value in answer.values:
                tokens.extend((answer.name, value))
    return tokens


def _print_summary(answers: list[_Answer], tokens: list[str]) -> None:
    table = Table(title="Inputs", show_header=False)
    for answer in answers:
        value = ", ".join(answer.values) if answer.values else "(default)"
        table.add_row(answer.name.removeprefix("--"), value)
    _console.print(table)
    _console.print(f"[dim]equivalent: {shlex.join(['ztg', *tokens])}[/dim]")


def _save_wizard_profile(generator_name: str, answers: list[_Answer]) -> pathlib.Path:
    """Persist the wizard's answers as profiles/<name>.toml for later `-f` runs."""
    generator_cls = GENERATORS[generator_name]
    doc: dict[str, object] = {}
    for answer in answers:
        if not answer.values:
            continue
        field_name = answer.name.removeprefix("--").replace("-", "_")
        if answer.grouped or len(answer.values) > 1:
            doc[field_name] = list(answer.values)
        else:
            doc[field_name] = answer.values[0]
    inputs = TypeAdapter(generator_cls.inputs_spec).validate_python(doc)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    path = PROFILES_DIR / f"{inputs.name}.toml"
    path.write_text(render_profile(generator_name, inputs))
    print(f"Saved profile {path}")
    return path


def main() -> None:
    app()
