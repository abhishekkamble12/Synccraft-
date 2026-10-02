"""
Evaluation runner for AI co-author prompts and guardrails.

Scoring is deliberately conservative: only properties that can be checked
deterministically are scored. Semantic expectations (tone, meaning preservation,
...) would need an LLM judge and are reported as `unchecked`, never as passes.

Usage:
    python -m ai.evals.runner          # deterministic FakeLLMClient (guardrail checks only)
    LLM_API_KEY=... python -m ai.evals.runner --live
"""

import argparse
import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from ai.client import BaseLLMClient, FakeLLMClient
from ai.evals.dataset import EVAL_DATASET
from ai.guards import (
    sanitize_document_text,
    validate_input_bounds,
    validate_output_bounds,
)
from ai.prompts import PROMPTS, SYSTEM_PROMPT_COAUTHOR

# Expectation key -> predicate(input_text, raw_output, cleaned_output)
CHECKS: dict[str, Callable[[str, str, str], bool]] = {
    "non_empty": lambda _i, _r, out: bool(out.strip()),
    "no_markdown_fence": lambda _i, raw, _o: "```" not in raw,
    "longer_than_input": lambda inp, _r, out: len(out) > len(inp),
    "bullet_count_min": lambda _i, _r, out: (
        sum(1 for ln in out.splitlines() if ln.lstrip().startswith(("-", "*", "•"))) >= 2
    ),
}
GUARD_EXPECTATIONS = {"blocks_or_defends", "rejects_excess_length", "sanitizes_delimiters"}


def _check_max_len_ratio(inp: str, out: str, ratio: float) -> bool:
    return len(out) <= len(inp) * ratio


async def _score_case(client: BaseLLMClient, item: dict[str, Any]) -> dict[str, Any]:
    inp, instruction, kind = item["input"], item["instruction"], item["kind"]
    expected: dict[str, Any] = item.get("expected", {})
    result: dict[str, Any] = {"id": item["id"], "failed": [], "unchecked": []}

    valid, _ = validate_input_bounds(inp, instruction)
    wants_block = bool(expected.keys() & {"blocks_or_defends", "rejects_excess_length"})
    if not valid:
        result["status"] = "pass" if wants_block else "fail"
        if not wants_block:
            result["failed"].append("unexpectedly_blocked")
        return result

    sanitized = sanitize_document_text(inp)
    if expected.get("sanitizes_delimiters") and "</document_text>" in sanitized:
        result["failed"].append("sanitizes_delimiters")

    template = PROMPTS.get(f"{kind}_v1", PROMPTS["rewrite_v1"])
    prompt = template.format(instruction=instruction, text=sanitized)
    start = time.perf_counter()
    try:
        raw = await client.generate_text(prompt, system_prompt=SYSTEM_PROMPT_COAUTHOR)
    except Exception as exc:
        result.update(status="error", error=str(exc))
        return result
    result["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)

    ok, cleaned = validate_output_bounds(raw)
    if not ok:
        result["failed"].append("output_guard")
        cleaned = ""

    for key, value in expected.items():
        if key in GUARD_EXPECTATIONS:
            if key != "sanitizes_delimiters" and wants_block:
                result["failed"].append(key)  # should have been blocked but was not
            continue
        if key == "max_len_ratio":
            if not _check_max_len_ratio(inp, cleaned, float(value)):
                result["failed"].append(key)
        elif key in CHECKS:
            if not CHECKS[key](inp, raw, cleaned):
                result["failed"].append(key)
        else:
            result["unchecked"].append(key)

    result["status"] = "fail" if result["failed"] else "pass"
    return result


async def run_evaluation(
    client: BaseLLMClient | None = None,
    dataset: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    eval_client = client or FakeLLMClient()
    items = dataset or EVAL_DATASET
    cases = [await _score_case(eval_client, item) for item in items]

    passed = sum(c["status"] == "pass" for c in cases)
    latencies = [c["latency_ms"] for c in cases if "latency_ms" in c]
    return {
        "client": type(eval_client).__name__,
        "total_cases": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "pass_rate_pct": round(100 * passed / len(cases), 1) if cases else 0.0,
        "semantic_expectations_unchecked": sum(len(c["unchecked"]) for c in cases),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "failures": {
            c["id"]: c["failed"] or c.get("error") for c in cases if c["status"] != "pass"
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the configured real LLM client")
    args = parser.parse_args()

    if args.live:
        from ai.client import OpenAICompatibleLLMClient

        try:
            eval_client: BaseLLMClient = OpenAICompatibleLLMClient()
        except ValueError as exc:
            parser.error(str(exc))
    else:
        eval_client = FakeLLMClient()

    print(json.dumps(asyncio.run(run_evaluation(eval_client)), indent=2))
