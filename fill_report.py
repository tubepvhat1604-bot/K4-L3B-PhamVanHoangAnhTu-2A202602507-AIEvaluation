"""Fill Exercise 3.2 in exercises.md from the REAL benchmark artifacts.

Run only after:
    python domain_assistant.py
    python evaluate_answers.py

It never invents numbers: every value is read from
``artifacts/benchmark_results.json``. It also writes
``artifacts/failure_cases.md`` with the trace (question, expected answer,
actual answer, gold vs retrieved chunks, ``find_root_cause`` output) of the
three lowest-scoring cases, which is the raw material for ``reflection.md``.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from evaluate_answers import load_evaluation_inputs
from template import BenchmarkRunner, FailureAnalyzer, RAGASEvaluator


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _short(question: str, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", question).replace("|", "\\|")
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def fill_exercises(exercises_path: Path, artifact: dict[str, Any]) -> None:
    """Replace the empty Exercise 3.2 table and aggregate placeholders."""
    text = exercises_path.read_text(encoding="utf-8")
    summary = artifact["summary"]
    results = artifact["results"]

    for row in results:
        pattern = re.compile(rf"^\| {re.escape(row['id'])} \|[ |]*$", re.MULTILINE)
        filled = (
            f"| {row['id']} | {_short(row['question'])} | "
            f"{_fmt(row['context_recall'])} | {_fmt(row['context_precision'])} | "
            f"{row['faithfulness']:.3f} | {row['relevance']:.3f} | "
            f"{row['completeness']:.3f} | {row['overall']:.3f} | "
            f"{'Yes' if row['passed'] else 'No'} | {row['failure_type'] or '-'} |"
        )
        text, count = pattern.subn(filled.replace("\\", "\\\\"), text, count=1)
        if count != 1:
            raise ValueError(f"Could not find an empty table row for {row['id']}")

    replacements = {
        "- Overall pass rate: ____%": f"- Overall pass rate: {summary['pass_rate']:.1%}",
        "- Avg Context Recall: ____": f"- Avg Context Recall: {_fmt(summary['avg_context_recall'])}",
        "- Avg Context Precision: ____": f"- Avg Context Precision: {_fmt(summary['avg_context_precision'])}",
        "- Avg Faithfulness: ____": f"- Avg Faithfulness: {summary['avg_faithfulness']:.3f}",
        "- Avg Relevance: ____": f"- Avg Relevance: {summary['avg_relevance']:.3f}",
        "- Avg Completeness: ____": f"- Avg Completeness: {summary['avg_completeness']:.3f}",
        "- Failure type distribution: ____": f"- Failure type distribution: {summary['failure_types']}",
    }
    for old, new in replacements.items():
        if old not in text:
            raise ValueError(f"Placeholder not found: {old}")
        text = text.replace(old, new, 1)

    worst = sorted(results, key=lambda r: r["overall"])[:3]
    for index, row in enumerate(worst, start=1):
        old = f"{index}. ID: ____ | Score: ____ | Failure type: ____"
        new = (
            f"{index}. ID: {row['id']} | Score: {row['overall']:.3f} | "
            f"Failure type: {row['failure_type'] or '-'}"
        )
        if old not in text:
            raise ValueError(f"Placeholder not found: {old}")
        text = text.replace(old, new, 1)

    exercises_path.write_text(text, encoding="utf-8")


def write_failure_cases(
    golden: Path, actual: Path, artifact: dict[str, Any], output: Path
) -> None:
    """Write the evidence trace for the three lowest-scoring cases."""
    pairs, answers = load_evaluation_inputs(golden, actual)
    results = BenchmarkRunner().run(pairs, answers.__getitem__, RAGASEvaluator())
    analyzer = FailureAnalyzer()

    golden_data = json.loads(golden.read_text(encoding="utf-8"))
    gold_sources = {
        rec["id"]: [c["source_doc"] for c in rec["contexts"]]
        for rec in golden_data["qa_pairs"]
    }
    actual_data = json.loads(actual.read_text(encoding="utf-8"))
    trace = {rec["id"]: rec["retrieved_contexts"] for rec in actual_data["answers"]}

    worst = sorted(results, key=lambda r: r.overall_score())[:3]
    lines: list[str] = ["# Three lowest-scoring cases (raw trace)\n"]
    for result in worst:
        case_id = result.qa_pair.metadata["id"]
        lines += [
            f"## {case_id} — {result.qa_pair.metadata.get('difficulty')}",
            f"**Question:** {result.qa_pair.question}\n",
            f"**Expected:** {result.qa_pair.expected_answer}\n",
            f"**Actual:** {result.actual_answer}\n",
            (
                f"**Scores:** recall={_fmt(result.context_recall)} "
                f"precision={_fmt(result.context_precision)} "
                f"faithfulness={result.faithfulness:.3f} "
                f"relevance={result.relevance:.3f} "
                f"completeness={result.completeness:.3f} "
                f"overall={result.overall_score():.3f} "
                f"failure_type={result.failure_type or '-'}\n"
            ),
            f"**find_root_cause():** {analyzer.find_root_cause(result)}\n",
            f"**Gold sources:** {', '.join(gold_sources[case_id])}\n",
            "**Retrieved chunks (rank order):**",
        ]
        for rank, chunk in enumerate(trace[case_id], start=1):
            preview = re.sub(r"\s+", " ", chunk["text"])[:110]
            lines.append(
                f"{rank}. `{chunk['chunk_id']}` ({chunk['source_doc']}, "
                f"score {chunk['score']:.2f}) — {preview}..."
            )
        lines.append("")

    failures = [r for r in results if not r.passed]
    suggestions = analyzer.generate_improvement_suggestions(failures)
    lines += [
        "## Improvement log (paste into reflection.md section 4)\n",
        "```text",
        analyzer.generate_improvement_log(failures, suggestions),
        "```",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("golden_dataset.json"))
    parser.add_argument("--actual", type=Path, default=Path("artifacts/actual_answers.json"))
    parser.add_argument("--results", type=Path, default=Path("artifacts/benchmark_results.json"))
    parser.add_argument("--exercises", type=Path, default=Path("exercises.md"))
    parser.add_argument("--cases-out", type=Path, default=Path("artifacts/failure_cases.md"))
    args = parser.parse_args()

    if not args.results.exists() or not args.actual.exists():
        print("ERROR: run `python domain_assistant.py` and `python evaluate_answers.py` first.")
        return 2
    artifact = json.loads(args.results.read_text(encoding="utf-8"))
    fill_exercises(args.exercises, artifact)
    write_failure_cases(args.golden, args.actual, artifact, args.cases_out)
    print(f"Filled Exercise 3.2 in {args.exercises}; trace written to {args.cases_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
