"""Run isolated twin evaluations. Raw model responses and source data are not saved."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from worktwin.evaluation import evaluate


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkTwin twin authorization and answer contract")
    parser.add_argument("--mode", choices=("mock", "gateway"), default="mock",
                        help="Gateway mode sends synthetic fixtures through enterprise BYOK and may incur charges")
    parser.add_argument("--output", type=Path, help="Write only aggregate/per-check scores to a local JSON file")
    args = parser.parse_args()
    result = evaluate(mode=args.mode)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"WorkTwin twin contract ({result['mode']}): {result['passed']}/{result['total']} passed")
    for item in result["cases"]:
        print(f"  {'PASS' if item['passed'] else 'FAIL'} {item['name']}")
    print(result["limitations"])
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
