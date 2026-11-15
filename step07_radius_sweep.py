# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Larry M. Arjomandi
"""Step 07 (radius sweep): candidate-radius sensitivity study, Table VIII.

Step 07 is run once for each radius in CANDIDATE_RADIUS_SENSITIVITY_SWEEP_M,
always with the same step 06 weights. After each run the seed summary, the
per-case summary and the metadata are copied to files tagged with the radius
(for example ..._seed_summary_r30.csv), which step10_paper_tables_and_figure.py
reads. The 60 m radius is the primary evaluation of the paper, so it is run
last and its untagged outputs are the ones left in place for
step07_statistical_analysis.py.

Each radius takes as long as one full step 07 evaluation.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline_config import (
    CANDIDATE_RADIUS_SENSITIVITY_SWEEP_M,
    INFERENCE_CANDIDATE_RADIUS_M,
    REPORTS_DIR,
    REPRO_BASE_SEED,
)

STEP07 = Path(__file__).resolve().parent / "step07_reproducibility_and_ci_evaluation.py"
OUTPUTS = (
    "step07_benchmark_reproducibility_model_seed_summary.csv",
    "step07_benchmark_reproducibility_model_seed_summary_by_case.csv",
    "step07_benchmark_reproducibility_model_seed_metadata.json",
)


def radius_tag(radius):
    """File-name tag for a radius, e.g. 30.0 -> '_r30'."""
    return f"_r{float(radius):g}"


def main():
    """Run step 07 at every sweep radius and keep a tagged copy of each result."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=100, help="Held-out runs per case (100 in the paper).")
    parser.add_argument("--base-seed", type=int, default=REPRO_BASE_SEED, help="Seed of run 0; run i uses base-seed + i.")
    parser.add_argument("--max-points", type=int, default=350, help="Passed on to step 07 (350 in the paper).")
    parser.add_argument("--max-workers", type=int, default=None, help="Parallel workers for step 07 (default: its own).")
    args = parser.parse_args()

    # Primary radius last, so its untagged outputs remain for the statistics step.
    radii = sorted(CANDIDATE_RADIUS_SENSITIVITY_SWEEP_M,
                   key=lambda r: (float(r) == float(INFERENCE_CANDIDATE_RADIUS_M), float(r)))
    for radius in radii:
        command = [
            sys.executable, str(STEP07),
            "--runs", str(args.runs),
            "--base-seed", str(args.base_seed),
            "--radius", str(radius),
            "--max-points", str(args.max_points),
        ]
        if args.max_workers is not None:
            command += ["--max-workers", str(args.max_workers)]
        print(f"\n=== Step 07 at radius {radius:g} m ===", flush=True)
        subprocess.run(command, check=True)
        for name in OUTPUTS:
            source = REPORTS_DIR / name
            target = REPORTS_DIR / name.replace(".", radius_tag(radius) + ".", 1)
            shutil.copyfile(source, target)
            print(f"Saved {target.name}")


if __name__ == "__main__":
    main()
