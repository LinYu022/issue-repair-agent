"""Evaluator/operator-only CLI. It never forwards results to an Agent session."""

import argparse
import json
import subprocess
from pathlib import Path
from uuid import UUID

from evals.contracts import EvaluationSpec, PatchSubmission
from evals.oracle import IndependentEvaluator

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=ROOT / ".local/independent-evaluations")
    parser.add_argument("--eval-run-id", type=UUID)
    parser.add_argument("--image", help="Local runner image name or immutable ID")
    args = parser.parse_args()
    try:
        spec = EvaluationSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
        submission = PatchSubmission.model_validate_json(args.submission.read_text(encoding="utf-8"))
        image = (
            subprocess.run(
                ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            if args.image
            else json.loads((ROOT / "environments/evaluator-image.json").read_text())["image_id"]
        )
        evaluator = IndependentEvaluator(args.output_root, image, ROOT)
        result = evaluator.evaluate(spec, submission, eval_run_id=args.eval_run_id)
        if evaluator.read(result.eval_run_id) != result:
            raise ValueError("evaluation_readback_mismatch")
    except (ValueError, OSError):
        print("EVALUATION REFUSED: check schema, input hashes, fresh run ID and runtime")
        return 2
    print(result.model_dump_json(indent=2))
    return 0  # CLI success records unresolved/unscorable just as faithfully as resolved.


if __name__ == "__main__":
    raise SystemExit(main())
