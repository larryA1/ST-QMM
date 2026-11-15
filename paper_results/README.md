# Reference outputs of the run reported in the paper

These files are outputs of the pipeline run behind the numbers in the paper,
produced from the released data in `../data/` with the code in this repository.
They let you check the paper's tables without running the pipeline, and compare
a new run with the original one.

The folder has the same layout as `results/`, so any script that reads
`results/` can read it instead:

| Folder | Files | Written by |
|---|---|---|
| `models/` | Trained QMM (`optimized_weights_v5`), QTS (`qts_optimized_weights_v1`) and matched classical weights (`classical_matched_*`), and the confidence-dip threshold of each case | step 06 |
| `reports/step03_*` | Trajectory durations, blackouts and validation checks (Table IV) | step03_validate_trajectories.py |
| `reports/step06_*` | Training losses, restart selection and threshold selection of each case | step 06 |
| `reports/step07_*seed_summary*.csv` | One row per held-out run with the metrics of every method, for the 60 m primary evaluation (untagged) and for each radius of the sensitivity study (`_r30` to `_r120`) | step 07, step07_radius_sweep.py |
| `reports/step07_statistical_analysis.csv` | Wilcoxon tests, Holm corrections and Hodges-Lehmann estimates | step07_statistical_analysis.py |

## Using them

Print the result tables of the paper and redraw Figure 3 from these files (takes
seconds):

```bash
python3 step10_paper_tables_and_figure.py --source paper_results
```

After running steps 06 and 07 yourself, compare your numbers with these:

```bash
python3 step10_paper_tables_and_figure.py --compare paper_results
```

You can also compare your step 06 weights in `results/models/` with the files in
`models/` here; with the pinned package versions they should be identical.

## Not included

- `step07_benchmark_per_fix_paths.csv` (240 MB), the per-fix diagnostics of
  step 07. It is written again by every step 07 run.
- The MLP and SVM control models (`*_ablation_model_v1.joblib`). They are
  pickled scikit-learn objects, which are unsafe to load from an untrusted
  source, and step 06 trains them again as part of its normal run.

File paths recorded inside these outputs are relative to the repository folder.
