"""The pydantic model's aliases must exactly mirror the vendored Zed schema."""

import json
import pathlib

from zed_theme_generator.gen.zed_theme import ThemeStyleContent

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_model_aliases_match_schema() -> None:
    schema = json.loads((REPO_ROOT / "zed_schema_v0.2.0.json").read_text())
    schema_keys = set(schema["definitions"]["ThemeStyleContent"]["properties"])
    model_keys = {
        field.alias or name
        for name, field in ThemeStyleContent.model_fields.items()
    }
    assert model_keys == schema_keys
