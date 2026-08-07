import json
from pathlib import Path

from jsonschema import ValidationError, validate

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
SCHEMA_PATH = ASSETS_DIR / "schema.json"
SAMPLE_PATH = ASSETS_DIR / "sample.json"
TEMPLATE_PATH = ASSETS_DIR / "ds-template.docx"


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_schema(schema_path: Path = SCHEMA_PATH) -> dict:
    schema = load_json(schema_path)
    return {key: value for key, value in schema.items() if key != "$schema"}


def load_sample(sample_path: Path = SAMPLE_PATH) -> dict:
    if not sample_path.is_file():
        raise FileNotFoundError(f"Missing sample JSON: {sample_path}")
    return load_json(sample_path)


def validate_context(context: dict, schema_path: Path = SCHEMA_PATH) -> None:
    schema = load_json(schema_path)
    validate(instance=context, schema=schema)


def load_validated_context(
    context_path: Path,
    schema_path: Path = SCHEMA_PATH,
) -> dict:
    context = load_json(context_path)
    try:
        validate_context(context, schema_path)
    except ValidationError as exc:
        raise ValueError(f"Invalid context.json: {exc.message}") from exc
    return context
