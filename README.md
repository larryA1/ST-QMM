# ST-QMM: code and data

This repository contains the code and data for the paper "Quantum Feature Maps for
3D Vehicle Positioning: A Hybrid Framework and Controlled Benchmark" by Larry M.
Arjomandi, submitted to the IEEE Transactions on Intelligent Transportation Systems.

ST-QMM (Spatio-Temporal Quantum Map Matcher) combines a 9-qubit circuit that scores
candidate road edges (QMM) and a 4-qubit circuit that scores transitions between
edges (QTS) with a graph-constrained Viterbi decoder. The paper compares it with a
spatial hidden Markov model, a level-aware EKF-HMM and a classical model of matched
size on six Australian road networks.

## Contents

- The Python scripts of the whole pipeline, from building the road graphs to the
  statistics and figures in the paper (MIT licence, `LICENSE`).
- `data/`: the six audited road networks (GraphML) and the synthetic truck
  trajectories (CSV) used in the paper, with their licence notices, checksums and
  source manifest (ODbL 1.0; see `data/README.md` and `data/LICENSE`). The
  trajectories are synthetic and contain no real vehicle or personal data.
- `paper_results/`: reference outputs of the run reported in the paper (trained
  weights, thresholds and the step 03, 06 and 07 summaries), for checking the
  tables and comparing a new run; see `paper_results/README.md`.
- `requirements.txt` and `requirements-lock.txt`: the package versions used.

All inputs are openly licensed:

| Input | Source and attribution | Licence |
|---|---|---|
| Road topology | OpenStreetMap, © OpenStreetMap contributors | ODbL 1.0 |
| Victorian road geometry, tunnel and bridge structure, travel direction | Vicmap Transport Road Line, © State of Victoria, Department of Energy, Environment and Climate Action | CC BY 4.0 |
| NSW road geometry, tunnel and bridge structure, status | NSW Transport Theme Road Segment, © State of New South Wales (Spatial Services, a business unit of the Department of Customer Service NSW) | CC BY 4.0 |
| Victorian posted speed limits | Speed Zones, © State of Victoria, Department of Transport and Planning | CC BY 4.0 |
| NSW posted speed limits | Speed Zones, source: Transport for NSW | CC BY 4.0 |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt
```

The paper was run with Python 3.9.6 on an Apple M4 (10 cores, macOS), using the
CPU state-vector simulator `lightning.qubit`; no GPU or quantum hardware is
needed. `requirements-lock.txt` pins every package to the version used.
`requirements.txt` lists only the direct dependencies, for newer environments.

## Checking the results

There are three levels, from seconds to a full rebuild.

**1. Tables and Figure 3 from the reference outputs (seconds).**

```bash
python3 step10_paper_tables_and_figure.py --source paper_results
```

This prints Tables III, IV and VI to VIII, the ablation and matched-control comparisons of
Sec. VI-B and the latencies of Sec. VI-C, and redraws Figure 3, all from the
outputs of the paper's run. The tables are also saved as CSV under
`results/step10_paper_results/`.

**2. Rerun calibration and evaluation from the released data (several hours).**

```bash
export PYTHONHASHSEED=0 HOLDOUT_TRUCKS_PER_CASE=100
python3 step03_validate_trajectories.py
python3 step06_quantum_calibration_corrected.py --seed 42 --restarts 5 --validation-fraction 0.2
python3 step07_reproducibility_and_ci_evaluation.py --runs 100 --base-seed 42 --radius 60 --max-points 350
python3 step07_statistical_analysis.py
python3 step10_paper_tables_and_figure.py --compare paper_results
```

The last command prints the new tables and the largest difference of every column
from the paper's run. `PYTHONHASHSEED=0` fixes the iteration order of sets and
dictionaries, and `HOLDOUT_TRUCKS_PER_CASE=100` gives the 80/100 calibration and
held-out split of the paper. On the machine above, step 03 validation takes under
a minute, step 06 about 75 minutes (one case at a time; `--force-parallel` runs
the cases in parallel) and step 07 about 75 minutes with its default 8 workers.
With the pinned packages, step 06 reproduces the paper's weights and thresholds
exactly; on other processors or package versions the last floating-point digits
can differ, which can change individual runs slightly.

The candidate-radius study of Table VIII repeats step 07 at 30, 45, 60, 90 and
120 m, which takes about five times as long:

```bash
python3 step07_radius_sweep.py --runs 100 --base-seed 42
python3 step07_statistical_analysis.py
```

**3. Rebuild everything from the public sources.** This overwrites `data/`.
OpenStreetMap and the government layers change over time, so a later rebuild can
differ slightly from the released files. Before running step 00b, download the
two statewide speed-zone files listed in its `SPEED_ZONE_SOURCES` into
`Roads/Open/raw/`; the road geometry itself is fetched from the publishers'
services.

```bash
export PYTHONHASHSEED=0 HOLDOUT_TRUCKS_PER_CASE=100
python3 step00b_build_open_road_reference.py
python3 step01_reproduce_unified_graph.py
python3 step02_check_graphml_file.py
python3 step03_generate_quantum_trajectories.py --seed 42 --num-trucks 180
```

then continue with level 2.

## Pipeline

The scripts are numbered in the order they run. Folder and file names are set in
`pipeline_config.py`, and each step reads the outputs of the earlier steps from
`data/` or `results/` (created on the first run).

| Step | Script | What it does |
|---|---|---|
| 00 | `step00_pull_hard_case_maps.py` | Downloads the raw OSM drive network around each of the six study sites (for inspection only). |
| 00b | `step00b_build_open_road_reference.py` | Builds the CC BY reference road layer: Vicmap and NSW Road Segment geometry and structure, with posted speeds from the state speed-zone datasets. |
| 01 | `step01_reproduce_unified_graph.py` | Builds the unified road graphs: OSM topology enriched with the reference-road attributes and the layer, tunnel and bridge semantics. |
| 02 | `step02_check_graphml_file.py` | Checks the GraphML files (CRS, geometry, connectivity, attributes). |
| 03 | `step03_generate_quantum_trajectories.py` | Generates the synthetic truck trajectories with GNSS noise, blackouts and recovery. |
| 03 | `step03_validate_trajectories.py` | Checks the trajectories against the graph topology and the recovery rules, and writes the statistics of Table IV. |
| 04 | `step04_viterbi_quantum_integration.py` | Self-contained quantum-Viterbi matcher for single-case runs and figures. |
| 05 | `step05_quantum_classical_benchmarking.py` | Benchmark engine: QMM and QTS circuits, spatial HMM, Level-EKF-HMM, MLP, SVM and LSTM controls, and the matched classical control. |
| 06 | `step06_quantum_calibration_corrected.py` | Offline calibration of the QMM, QTS and matched classical weights with the pairwise margin ranking loss, and selection of the confidence-dip thresholds. |
| 07 | `step07_reproducibility_and_ci_evaluation.py` | Repeated held-out evaluation (100 held-out trajectories per case) with 95% confidence intervals. |
| 07 | `step07_statistical_analysis.py` | Wilcoxon signed-rank tests with Holm correction, Hodges-Lehmann estimates and the ST-QMM against matched-classical comparison. |
| 07 | `step07_radius_sweep.py` | Runs step 07 at every radius of the sensitivity study (Table VIII). |
| 08 | `step08_generate_candidates.py` | Candidate edges for each fix of a GPS trace. |
| 09 | `step09_quantum_transition_scorer.py` | Plots of the QTS transition scores along a decoded route, using the step 06 weights. |
| 10 | `step10_paper_tables_and_figure.py` | Prints Tables III, IV and VI to VIII and redraws Figure 3 from `results/` or `paper_results/`, and compares two runs. |

Supporting files:

- `pipeline_config.py`: shared paths, seeds, held-out split, search radii and helper functions.
- `diagnostic_barren_plateau_scan.py`: the gradient-variance (barren plateau) check following McClean et al., mentioned in Sec. III-A of the paper.
- `test_step06_key_aware_training.py`: unit tests for the handling of parallel edges during calibration (`python3 -m unittest test_step06_key_aware_training`).

Figures 1 and 2 of the paper (the circuit diagram and the confidence-dip
diagram) are drawn in LaTeX and are not produced by this code.

## Citation

If you use this code or data, please cite the paper. Its citation will be added
here once it is published; until then, `CITATION.cff` describes this repository.

## Licence

The code is released under the MIT licence (`LICENSE`), which covers the original
pipeline code only and grants no rights to third-party data.

The files in `data/` and the derived outputs in `paper_results/` are released under
the Open Database License (ODbL) 1.0
(https://opendatacommons.org/licenses/odbl/1-0/). Because the networks are derived
from OpenStreetMap, they and any database derived from them must be shared under the
ODbL with the attribution "© OpenStreetMap contributors". Parts adapted from the
government datasets above remain subject to CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/): keep the attributions in the table
above, link the licence and state that the material was modified. The full notices,
the list of changes, and the no-endorsement and no-warranty statements are in
`data/LICENSE`.

The data publishers do not endorse this work. The data is a research artefact,
provided as is, and must not be used for navigation or operational decisions.
