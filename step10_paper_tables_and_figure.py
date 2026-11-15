# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Larry M. Arjomandi
"""Step 10: rebuild Figure 3 and Tables III, IV and VI to VIII of the paper.

Reads the outputs of steps 03, 06 and 07 from a results folder, prints the
paper's tables, writes each of them as CSV and draws Figure 3 (paired RMSE
gains). The ablation and matched-control comparisons quoted in Sec. VI-B and
the per-fix latencies of Sec. VI-C are written as well.

By default the script reads results/, which is what running the pipeline
produces. With --source paper_results it reads the reference outputs of the
run reported in the paper, and --compare paper_results prints how far the
numbers of a new run are from that reference. Outputs go to
results/step10_<source folder name>/.
"""
import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

from pipeline_config import CANDIDATE_RADIUS_SENSITIVITY_SWEEP_M, PROJECT_DIR

SEED_SUMMARY = "step07_benchmark_reproducibility_model_seed_summary{tag}.csv"
STATISTICS = "step07_statistical_analysis.csv"
DURATIONS = "step03_trajectory_duration_and_blackout_stats.csv"

# Case order and labels used in Table VII and Figure 3.
CASES = [
    ("Domain_Tunnel_VIC", "Domain Tunnel"),
    ("Rozelle_Interchange_NSW", "Rozelle Interchange"),
    ("West_Gate_Tunnel_VIC", "West Gate Tunnel"),
    ("M80_Princes_Freeway_VIC", "M80 Princes Freeway"),
    ("NorthConnex_NSW", "NorthConnex"),
    ("Light_Horse_Interchange_NSW", "Light Horse Interchange"),
]
CASE_LABEL = dict(CASES)
METHODS = [
    ("ST-QMM", "quantum"),
    ("HMM", "classical"),
    ("Level-EKF-HMM", "level_ekf_hmm"),
    ("MLP", "mlp"),
    ("RBF-SVM", "svm"),
]
# Comparisons behind the ablation paragraph of Sec. VI-B. For "A_vs_B" the
# gain is RMSE(A) - RMSE(B), so a positive gain favours B.
ABLATIONS = [
    ("classical_vs_qmm_entanglement", "QMM only vs HMM", "holm_p_full_family"),
    ("classical_vs_qts_only", "QTS only vs HMM", "holm_p_full_family"),
    ("classical_vs_frozen", "Frozen QMM vs HMM", "holm_p_full_family"),
    ("classical_vs_classical_matched", "Matched classical vs HMM", "holm_p_full_family"),
    ("classical_matched_vs_quantum", "ST-QMM vs matched classical", "holm_p_matched_family"),
    ("level_ekf_hmm_vs_level_ekf_hmm_quantum", "Level-EKF-HMM + QMM/QTS vs Level-EKF-HMM", "holm_p_ekf_family"),
]


def read_csv(source, name):
    """Read a report from `source`/reports, or return None if it is missing."""
    path = source / "reports" / name
    if not path.exists():
        print(f"  (missing {source.name}/reports/{name})")
        return None
    return pd.read_csv(path)


def table4(source):
    """Table IV: trajectory length, tunnel share, blackouts and portal transitions."""
    durations = read_csv(source, DURATIONS)
    if durations is None:
        return None
    return pd.DataFrame({
        "case": durations["case"],
        "fixes_per_truck": durations["fixes_per_truck_mean"],
        "duration_s": durations["duration_s_mean"],
        "tunnel_fix_pct": 100.0 * durations["tunnel_fix_fraction_mean"],
        "max_blackout_p95_s": durations["max_blackout_interval_s_p95"],
        "portal_transitions": durations["n_portal_transitions_mean"],
    })


def table3(source):
    """Table III: validation-selected confidence-dip thresholds."""
    rows = []
    for case, _label in CASES:
        path = source / "models" / f"step06_{case}_confidence_threshold_v5.json"
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
        total = record["tp"] + record["tn"] + record["fp"] + record["fn"]
        rows.append({
            "case": case,
            "tau_c": record["threshold"] if record.get("detector_enabled", True) else np.nan,
            "detector_enabled": bool(record.get("detector_enabled", True)),
            "balanced_accuracy": record["balanced_accuracy"],
            "prevalence_pct": 100.0 * (record["tp"] + record["fn"]) / total,
        })
    return pd.DataFrame(rows) if rows else None


def table6(runs):
    """Table VI: pooled metrics over all case-trajectory runs, plus latency."""
    rows = []
    for label, mode in METHODS:
        rows.append({
            "method": label,
            "rmse_m": runs[f"{mode}_rmse"].mean(),
            "segment_match_pct": runs[f"{mode}_edge_accuracy"].mean(),
            "relaxed_match_pct": runs[f"{mode}_relaxed_adjacent_accuracy"].mean(),
            "layer_accuracy_pct": runs[f"{mode}_layer_accuracy"].mean(),
            "mismatch_pct": runs[f"{mode}_route_mismatch_rate"].mean(),
            "latency_ms_per_fix": runs[f"{mode}_latency_ms_per_fix"].mean(),
        })
    return pd.DataFrame(rows)


def table7(runs, stats):
    """Table VII: per-case mean RMSE and the paired ST-QMM against HMM tests."""
    paired = stats[stats["comparison"] == "classical_vs_quantum"].set_index("case")
    rows = []
    for case, _label in CASES:
        case_runs = runs[runs["case"] == case]
        test = paired.loc[case]
        rows.append({
            "case": case,
            "rmse_st_qmm_m": case_runs["quantum_rmse"].mean(),
            "rmse_hmm_m": case_runs["classical_rmse"].mean(),
            "rmse_level_ekf_hmm_m": case_runs["level_ekf_hmm_rmse"].mean(),
            "mean_gain_m": test["mean_gain_m"],
            "hl_gain_m": test["hodges_lehmann_m"],
            "hl_ci95_lo_m": test["hodges_lehmann_ci95_lo"],
            "hl_ci95_hi_m": test["hodges_lehmann_ci95_hi"],
            "rank_biserial_r": test["rank_biserial_r"],
            "p_wilcoxon_raw": test["wilcoxon_p"],
            "p_holm_primary": test["holm_p_primary_family"],
            "p_holm_full": test["holm_p_full_family"],
        })
    return pd.DataFrame(rows)


def table8(source):
    """Table VIII: pooled RMSE and positive-gain rate at each sweep radius."""
    rows = []
    for radius in CANDIDATE_RADIUS_SENSITIVITY_SWEEP_M:
        path = source / "reports" / SEED_SUMMARY.format(tag=f"_r{float(radius):g}")
        if not path.exists():
            continue
        runs = pd.read_csv(path)
        rows.append({
            "radius_m": float(radius),
            "rmse_st_qmm_m": runs["quantum_rmse"].mean(),
            "rmse_hmm_m": runs["classical_rmse"].mean(),
            "rmse_level_ekf_hmm_m": runs["level_ekf_hmm_rmse"].mean(),
            "positive_gain_rate": float((runs["rmse_gain"] > 0).mean()),
        })
    if not rows:
        print("  (no radius-tagged step 07 summaries; run step07_radius_sweep.py for Table VIII)")
        return None
    return pd.DataFrame(rows)


def ablations(stats):
    """Ablation and matched-control comparisons quoted in Sec. VI-B."""
    rows = []
    for comparison, label, family in ABLATIONS:
        subset = stats[stats["comparison"] == comparison].set_index("case")
        for case, _case_label in CASES:
            if case not in subset.index:
                continue
            test = subset.loc[case]
            rows.append({
                "comparison": label,
                "case": case,
                "hl_gain_m": test["hodges_lehmann_m"],
                "p_holm_own_family": test[family],
                "p_holm_full": test["holm_p_full_family"],
            })
    return pd.DataFrame(rows)


def figure3(runs, stats, out_dir):
    """Figure 3: mean and Hodges-Lehmann gains (a) and all paired gains (b)."""
    paired = stats[stats["comparison"] == "classical_vs_quantum"].set_index("case")
    cases = [case for case, _label in CASES]
    labels = [label for _case, label in CASES]
    groups = [runs.loc[runs["case"] == case, "rmse_gain"].to_numpy() for case in cases]
    hl = paired.loc[cases, "hodges_lehmann_m"].to_numpy()
    hl_lo = paired.loc[cases, "hodges_lehmann_ci95_lo"].to_numpy()
    hl_hi = paired.loc[cases, "hodges_lehmann_ci95_hi"].to_numpy()

    # The paper uses Times New Roman; any serif font is used if it is missing.
    installed = {font.name for font in font_manager.fontManager.ttflist}
    plt.rcParams.update({
        "font.family": "Times New Roman" if "Times New Roman" in installed else "serif",
        "font.size": 8, "axes.labelsize": 9, "axes.titlesize": 9, "legend.fontsize": 8,
        "xtick.labelsize": 8, "ytick.labelsize": 8, "pdf.fonttype": 42, "ps.fonttype": 42,
    })
    fig, axes = plt.subplots(2, 1, figsize=(3.5, 4.3))
    positions = np.arange(len(groups))
    means = np.array([np.mean(values) for values in groups])
    axes[0].bar(positions - 0.18, means, width=0.36, label="Mean", color="#3677a9")
    axes[0].bar(positions + 0.18, hl, width=0.36, label="HL with 95% CI", color="#e28e2c",
                yerr=[hl - hl_lo, hl_hi - hl], capsize=2.0,
                error_kw={"elinewidth": 0.8, "capthick": 0.8, "ecolor": "#333333"})
    axes[0].axhline(0, color="black", linewidth=0.8)
    axes[0].set_xticks(positions, labels, rotation=40, ha="right")
    axes[0].set_ylabel("RMSE gain (m)")
    axes[0].set_title("(a) Mean and Hodges–Lehmann (HL) paired gain")
    axes[0].set_ylim(top=max(means.max(), hl_hi.max()) * 1.35)
    axes[0].legend(frameon=False, ncols=2, loc="upper left")
    axes[0].grid(axis="y", alpha=0.22)

    parts = axes[1].violinplot(groups, positions=positions, showmeans=False, showmedians=True, widths=0.76)
    for body in parts["bodies"]:
        body.set_facecolor("#6a9ec1")
        body.set_edgecolor("black")
        body.set_alpha(0.60)
    for key in ("cmedians", "cbars", "cmins", "cmaxes"):
        parts[key].set_color("black")
        parts[key].set_linewidth(0.75)
    # Fixed jitter seed, so the scatter is the same in every rebuild.
    rng = np.random.default_rng(42)
    for position, values in zip(positions, groups):
        axes[1].scatter(position + rng.uniform(-0.09, 0.09, len(values)), values,
                        s=9, color="#245d7c", edgecolor="black", linewidth=0.2, zorder=3)
    axes[1].axhline(0, color="black", linewidth=0.8)
    axes[1].set_xticks(positions, labels, rotation=40, ha="right")
    axes[1].set_title("(b) Paired trajectory-gain distributions")
    axes[1].grid(axis="y", alpha=0.22)
    fig.tight_layout()
    fig.savefig(out_dir / "figure3_paired_gain.pdf", bbox_inches="tight", pad_inches=0.15)
    fig.savefig(out_dir / "figure3_paired_gain.png", dpi=350, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


def build(source):
    """All tables for one results folder, as a dict of name -> DataFrame."""
    runs = read_csv(source, SEED_SUMMARY.format(tag=""))
    stats = read_csv(source, STATISTICS)
    tables = {"table3_thresholds": table3(source), "table4_trajectories": table4(source)}
    if runs is not None:
        tables["table6_pooled_metrics"] = table6(runs)
    if runs is not None and stats is not None:
        tables["table7_paired_inference"] = table7(runs, stats)
        tables["ablation_and_matched_control"] = ablations(stats)
    tables["table8_radius_sensitivity"] = table8(source)
    return {name: table for name, table in tables.items() if table is not None}, runs, stats


def compare(tables, reference):
    """Print the largest absolute difference of every numeric column against `reference`."""
    print("\nDifference from the reference run (largest absolute difference per column):")
    for name, table in tables.items():
        if name not in reference:
            print(f"  {name}: not in reference")
            continue
        ref = reference[name]
        key = table.columns[0]
        merged = table.merge(ref, on=[c for c in table.columns if table[c].dtype == object and c in ref.columns] or [key],
                             suffixes=("", "_ref"))
        diffs = {}
        for column in table.columns:
            if column + "_ref" in merged and np.issubdtype(table[column].dtype, np.number):
                delta = (merged[column] - merged[column + "_ref"]).abs()
                diffs[column] = float(delta.max()) if delta.notna().any() else 0.0
        worst = max(diffs.values()) if diffs else 0.0
        status = "identical" if worst < 1e-9 else f"max |diff| = {worst:.6g}"
        print(f"  {name:32s} {status}")
        if worst >= 1e-9:
            for column, value in diffs.items():
                if value >= 1e-9:
                    print(f"      {column}: {value:.6g}")


def main():
    """Rebuild the tables and Figure 3 from a results folder."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default="results",
                        help="Folder with reports/ and models/ (default: results, or paper_results).")
    parser.add_argument("--compare", default=None,
                        help="Reference folder to compare against, e.g. paper_results.")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_absolute():
        source = PROJECT_DIR / source
    out_dir = PROJECT_DIR / "results" / f"step10_{source.name}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Reading {source.name}/")
    tables, runs, stats = build(source)
    pd.set_option("display.width", 200)
    for name, table in tables.items():
        table.to_csv(out_dir / f"{name}.csv", index=False)
        print(f"\n{name}\n{table.round(4).to_string(index=False)}")
    if runs is not None and stats is not None:
        figure3(runs, stats, out_dir)
        print(f"\nFigure 3 saved to {out_dir.relative_to(PROJECT_DIR)}/figure3_paired_gain.pdf")

    if args.compare:
        reference_dir = Path(args.compare)
        if not reference_dir.is_absolute():
            reference_dir = PROJECT_DIR / reference_dir
        reference, _runs, _stats = build(reference_dir)
        compare(tables, reference)


if __name__ == "__main__":
    main()
