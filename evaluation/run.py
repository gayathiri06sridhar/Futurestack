import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

from backend import storage
from backend.retrieval import index_document, retrieve_evidence
from backend.reviewer import ReviewServiceError, review_text
from backend.visual import analyze_visual_content, wcag_contrast_ratio


DATASET_PATH = Path(__file__).with_name("cases.json")
DEFAULT_OUTPUT = Path(__file__).with_name("results.jsonl")
FIXTURE_DIR = Path(__file__).with_name("fixtures")


def _safe_ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _binary_metrics(expected: list[bool], predicted: list[bool]) -> dict[str, Any]:
    if not expected:
        return {
            "tested": 0,
            "accuracy": None,
            "precision": None,
            "recall": None,
            "f1": None,
            "false_positive_rate": None,
            "false_negative_rate": None,
            "confusion_matrix": {
                "true_positive": 0,
                "true_negative": 0,
                "false_positive": 0,
                "false_negative": 0,
            },
        }
    tp = sum(actual and guess for actual, guess in zip(expected, predicted))
    tn = sum(not actual and not guess for actual, guess in zip(expected, predicted))
    fp = sum(not actual and guess for actual, guess in zip(expected, predicted))
    fn = sum(actual and not guess for actual, guess in zip(expected, predicted))
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    return {
        "tested": len(expected),
        "accuracy": _safe_ratio(tp + tn, len(expected)),
        "precision": precision,
        "recall": recall,
        "f1": _safe_ratio(2 * precision * recall, precision + recall),
        "false_positive_rate": _safe_ratio(fp, fp + tn),
        "false_negative_rate": _safe_ratio(fn, fn + tp),
        "confusion_matrix": {
            "true_positive": tp,
            "true_negative": tn,
            "false_positive": fp,
            "false_negative": fn,
        },
    }


def _category_metrics(
    cases: list[dict[str, Any]], expected_key: str, predicted_key: str
) -> dict[str, dict[str, int | float]]:
    categories = {
        category
        for case in cases
        for category in case.get(expected_key, [])
    }
    results: dict[str, dict[str, int | float]] = {}
    for category in sorted(categories):
        tp = fp = fn = 0
        for case in cases:
            expected = category in case.get(expected_key, [])
            predicted = category in case.get(predicted_key, [])
            tp += int(expected and predicted)
            fp += int(not expected and predicted)
            fn += int(expected and not predicted)
        precision = _safe_ratio(tp, tp + fp)
        recall = _safe_ratio(tp, tp + fn)
        results[category] = {
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "precision": precision,
            "recall": recall,
            "f1": _safe_ratio(2 * precision * recall, precision + recall),
        }
    return results


def _severity_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    if not cases:
        return {
            "tested": 0,
            "correct": 0,
            "accuracy": None,
            "confusion_matrix": {},
        }
    matrix: dict[str, dict[str, int]] = {}
    correct = 0
    for case in cases:
        expected = case["expected_severity"]
        predicted = case["predicted_severity"]
        matrix.setdefault(expected, {})
        matrix[expected][predicted] = matrix[expected].get(predicted, 0) + 1
        correct += int(expected == predicted)
    return {
        "tested": len(cases),
        "correct": correct,
        "accuracy": _safe_ratio(correct, len(cases)),
        "confusion_matrix": matrix,
    }


def _run_visual_case(
    case: dict[str, Any],
    palette: list[str],
    thresholds: dict[str, float],
    logo_bytes: bytes,
    expected_logo_zone: str,
) -> dict[str, Any]:
    fixture_path = DATASET_PATH.parent / case["fixture"]
    if not fixture_path.is_file():
        raise FileNotFoundError(f"Required visual fixture is missing: {fixture_path}")
    asset_bytes = [logo_bytes] if case.get("include_logo_reference") else []
    measurement = analyze_visual_content(
        fixture_path.read_bytes(),
        approved_colours=palette,
        approved_logos=asset_bytes,
        contrast_pairs=case.get("contrast_pairs", []),
        thresholds=thresholds,
        expected_logo_zone=expected_logo_zone,
    )
    categories = [
        finding["type"]
        for check in (
            measurement["colour_check"],
            measurement["logo_check"],
        )
        for finding in check["findings"]
    ]
    predicted_severity = "low"
    if any(
        category in categories
        for category in (
            "visual_inconsistency",
            "logo_violation",
            "logo_placement_violation",
        )
    ):
        predicted_severity = "medium"
    for contrast in measurement["contrast_checks"]:
        if not contrast["passes"]:
            categories.append("accessibility_issue")
            predicted_severity = "high"
    return {
        "case_id": case["id"],
        "expected_compliant": case["expected_compliant"],
        "predicted_compliant": not categories,
        "expected_categories": case.get("expected_categories", []),
        "predicted_categories": sorted(set(categories)),
        "expected_severity": case.get("expected_severity", "low"),
        "predicted_severity": predicted_severity,
        "measurements": measurement,
    }


async def _run(output_path: Path) -> dict[str, Any]:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    text_cases = [case for case in cases if case["kind"] == "text"]
    visual_cases = [case for case in cases if case["kind"] == "visual"]
    skipped: list[dict[str, str]] = []
    live_results: list[dict[str, Any]] = []
    text_retrieval: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="futurestack-eval-") as temporary_dir:
        previous_database = storage.DATABASE_PATH
        storage.DATABASE_PATH = Path(temporary_dir) / "evaluation.sqlite3"
        try:
            storage.initialize_database()
            rules = dataset["brand_rules"]
            await index_document(
                "evaluation-brand-rules",
                "Everpine sample product and brand guide",
                "evaluation/cases.json#brand_rules",
                json.dumps(rules, ensure_ascii=False),
            )
            for case in text_cases:
                retrieved = await retrieve_evidence(case["content"], top_k=5)
                expected_evidence = case.get("expected_evidence")
                if expected_evidence:
                    text_retrieval.append(
                        {
                            "case_id": case["id"],
                            "relevant_at_5": any(
                                expected_evidence.casefold() in item["guideline"].casefold()
                                for item in retrieved["evidence"]
                            ),
                            "retrieved_sources": [
                                item["source"] for item in retrieved["evidence"]
                            ],
                        }
                    )
                if not os.getenv("LLM_API_KEY"):
                    skipped.append(
                        {
                            "case_id": case["id"],
                            "reason": "LLM_API_KEY is not configured; live Gemini prediction skipped.",
                        }
                    )
                    continue
                try:
                    result = await review_text(
                        case["content"],
                        rules,
                        evidence=retrieved["evidence"],
                        retrieval_mode=retrieved["mode"],
                    )
                except ReviewServiceError as exc:
                    skipped.append(
                        {"case_id": case["id"], "reason": f"Gemini review failed: {exc}"}
                    )
                    continue
                live_results.append(
                    {
                        "case_id": case["id"],
                        "expected_compliant": case["expected_compliant"],
                        "predicted_compliant": result.status.value == "approved",
                        "expected_categories": [case["expected_category"]]
                        if case.get("expected_category")
                        else [],
                        "predicted_categories": sorted(
                            {issue.type for issue in result.issues}
                        ),
                        "expected_severity": case.get("expected_severity"),
                        "predicted_severity": result.severity.value,
                        "retrieval_mode": retrieved["mode"],
                        "retrieved_sources": [
                            source["source"] for source in retrieved["evidence"]
                        ],
                        "review": result.model_dump(mode="json"),
                    }
                )
        finally:
            storage.DATABASE_PATH = previous_database

    logo_path = FIXTURE_DIR / "approved_logo.png"
    if not logo_path.is_file():
        raise FileNotFoundError(f"Required approved logo fixture is missing: {logo_path}")
    visual_settings = dataset["visual_settings"]
    visual_results = [
        _run_visual_case(
            case,
            visual_settings["approved_colours"],
            visual_settings["thresholds"],
            logo_path.read_bytes(),
            visual_settings.get("expected_logo_zone", "any"),
        )
        for case in visual_cases
    ]
    for case, result in zip(visual_cases, visual_results):
        if "expected_contrast_ratio" in case:
            result["measured_contrast_ratio"] = wcag_contrast_ratio(
                case["contrast_pairs"][0]["foreground"],
                case["contrast_pairs"][0]["background"],
            )

    api_key_configured = bool(os.getenv("LLM_API_KEY"))
    failed_cases = sum(
        "Gemini review failed" in item["reason"] for item in skipped
    )
    live_scope = "Live Gemini calls against synthetic text examples."
    if not api_key_configured:
        live_scope = "Not run; LLM_API_KEY is missing. No model predictions or metrics were produced."
    elif failed_cases:
        live_scope = (
            "Live Gemini calls were attempted; failed cases are excluded from "
            "performance metrics and listed in skipped."
        )
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": dataset["dataset"],
        "dataset_version": dataset["version"],
        "model": os.getenv("LLM_MODEL", "gemini-3.8-flash"),
        "live_model_scope": live_scope,
        "live_model_metrics": {
            "status": (
                "measured"
                if live_results and not skipped
                else "partial"
                if live_results
                else "failed"
                if api_key_configured and failed_cases
                else "not_run"
            ),
            "metrics": _binary_metrics(
                [item["expected_compliant"] for item in live_results],
                [item["predicted_compliant"] for item in live_results],
            ),
            "issue_category_performance": _category_metrics(
                live_results, "expected_categories", "predicted_categories"
            ),
            "severity_classification": _severity_metrics(
                [item for item in live_results if item["expected_severity"] is not None]
            ),
            "results": live_results,
        },
        "visual_measurement_metrics": {
            "scope": "Deterministic local image measurements; not Gemini vision performance.",
            "metrics": _binary_metrics(
                [item["expected_compliant"] for item in visual_results],
                [item["predicted_compliant"] for item in visual_results],
            ),
            "issue_category_performance": _category_metrics(
                visual_results, "expected_categories", "predicted_categories"
            ),
            "severity_classification": _severity_metrics(visual_results),
            "results": visual_results,
        },
        "retrieval_relevance": {
            "tested": len(text_retrieval),
            "hits_at_5": sum(item["relevant_at_5"] for item in text_retrieval),
            "hit_at_5": _safe_ratio(
                sum(item["relevant_at_5"] for item in text_retrieval),
                len(text_retrieval),
            ),
            "results": text_retrieval,
        },
        "skipped": skipped,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(report, ensure_ascii=False) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate live Gemini reviews and objective local visual checks."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Append a timestamped measured report as one JSON line.",
    )
    arguments = parser.parse_args()
    try:
        report = asyncio.run(_run(arguments.output))
    except (ReviewServiceError, RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"Evaluation not completed: {exc}", file=sys.stderr)
        return 2
    summary = {key: value for key, value in report.items() if key != "live_model_metrics"}
    summary["live_model_metrics"] = {
        key: value
        for key, value in report["live_model_metrics"].items()
        if key != "results"
    }
    print(json.dumps(summary, indent=2))
    if not os.getenv("LLM_API_KEY"):
        print(
            "Live model evaluation skipped: set LLM_API_KEY to run the Gemini text cases.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
