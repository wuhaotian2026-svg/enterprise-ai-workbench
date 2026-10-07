from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path
from typing import Sequence

from policy_api.evaluation.holdout_generator import write_seed_blind_holdout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a model-independent seed-blind HR Tool Calling holdout."
    )
    parser.add_argument("--dataset-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path, required=True)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args(list(argv) if argv is not None else None)
    seed = args.seed if args.seed is not None else secrets.randbits(64)
    try:
        metadata = write_seed_blind_holdout(
            seed=seed,
            dataset_path=args.dataset_output,
            manifest_path=args.manifest_output,
            metadata_path=args.metadata_output,
        )
    except (OSError, ValueError) as exc:
        print(f"holdout_generation_error={exc}")
        return 1
    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
