import json
from pathlib import Path
from typing import Any


RULES_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "brand_rules.json"


def load_brand_rules() -> dict[str, Any]:
    try:
        with RULES_PATH.open(encoding="utf-8") as rules_file:
            rules = json.load(rules_file)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Brand rules file was not found: {RULES_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Brand rules file contains invalid JSON: {RULES_PATH}") from exc

    if not isinstance(rules, dict):
        raise RuntimeError("Brand rules must be a JSON object.")
    return rules
