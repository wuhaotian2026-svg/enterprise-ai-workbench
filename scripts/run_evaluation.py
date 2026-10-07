from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from policy_api.evaluation.runner import EvaluationRunner, RunnerConfigurationError


def _generate_procurement_holdout(argv: Sequence[str]) -> int:
    from policy_api.evaluation.procurement_holdout_generator import (
        write_procurement_holdout,
    )

    parser = argparse.ArgumentParser(
        description="Generate a model-independent procurement holdout bundle."
    )
    parser.add_argument("--generate-procurement-holdout", action="store_true")
    parser.add_argument("--seed", required=True)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--cases-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    args = parser.parse_args(list(argv))
    try:
        metadata = write_procurement_holdout(
            seed=args.seed,
            count=args.count,
            cases_output=args.cases_output,
            manifest_output=args.manifest_output,
        )
    except (OSError, ValueError) as exc:
        print(f"evaluation_error={exc}", file=sys.stderr)
        return 1
    print(f"dataset_sha256={metadata['dataset_sha256']}")
    print(f"manifest_sha256={metadata['manifest_sha256']}")
    print(f"case_count={metadata['case_count']}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    if "--generate-procurement-holdout" in raw_args:
        return _generate_procurement_holdout(raw_args)
    if "--suite" in raw_args:
        index = raw_args.index("--suite")
        if index + 1 >= len(raw_args):
            print("evaluation_error=missing_suite", file=sys.stderr)
            return 2
        suite = raw_args[index + 1]
        delegated = raw_args[:index] + raw_args[index + 2:]
        if suite == "procurement-tool-calling-real":
            from policy_api.evaluation.procurement_tool_calling import (
                main as procurement_main,
            )

            return procurement_main(delegated)
        print(f"evaluation_error=unknown_suite:{suite}", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(description="Run the policy assistant evaluation dataset.")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(raw_args)
    cases = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    runner: EvaluationRunner | None = None
    try:
        runner = EvaluationRunner.from_environment()
    except RunnerConfigurationError as exc:
        print(f"evaluation unavailable: {exc}", file=sys.stderr)
        return 2
    try:
        report = runner.run(cases, args.output)
        print(json.dumps(report["metrics"], ensure_ascii=False, sort_keys=True))
    finally:
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
