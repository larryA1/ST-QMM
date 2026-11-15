# Released data: road networks and synthetic trajectories

This directory holds the six audited road networks and the synthetic truck
trajectories used in the paper, exactly as the pipeline in this repository
produces them. The code reads them from these paths, so the benchmark can be
run from Step 06 without rebuilding the networks.

**Licence:** Open Database License (ODbL) 1.0, with CC BY 4.0 attribution for
the government sources. See [`LICENSE`](LICENSE) for the full notices.
The MIT licence of the code does not apply to these files.

## Attribution

Contains information from OpenStreetMap, © OpenStreetMap contributors, made
available under the [ODbL 1.0](https://www.openstreetmap.org/copyright).
Contains material adapted from these datasets, each licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/):

| Dataset | Attribution |
|---|---|
| [Vicmap Transport - Road Line](https://discover.data.vic.gov.au/dataset/vicmap-transport-road-line) | © State of Victoria, Department of Energy, Environment and Climate Action |
| [NSW Transport Theme - Road Segment](https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Transport_Theme/FeatureServer/5) | © State of New South Wales (Spatial Services, a business unit of the Department of Customer Service NSW) |
| [Speed Zones (Victoria)](https://opendata.transport.vic.gov.au/dataset/975b80b9-e530-46e2-80a5-54002765e81a) | © State of Victoria, Department of Transport and Planning |
| [Speed Zones (New South Wales)](https://opendata.transport.nsw.gov.au/data/dataset/4253a054-b377-4b5b-83d1-71385bb6ff33) | Source: Transport for NSW |

The source data was modified: it was clipped to the study windows, re-coded,
matched to the OSM topology, and used to add tunnel and bridge structure,
names, direction and posted speeds; see `LICENSE` for the full list. The
publishers do not endorse this work, and the data is provided as is, without
warranty. The trajectories are synthetic and contain no real vehicle or
personal data.

Every network file also carries these notices in its graph-level `license`,
`license_url`, `attribution`, `modifications` and `disclaimer` attributes.

## Contents

| File | Contents | SHA-256 |
|---|---|---|
| `graphs/step01_Rozelle_Interchange_NSW_unified.graphml` | 180 nodes, 315 edges | `34b21d3eef7de53bedfa90ee54b2459e92dc73d2a952e728574adca06f109a74` |
| `graphs/step01_West_Gate_Tunnel_VIC_unified.graphml` | 173 nodes, 244 edges | `0e61807948b02715c3d7e4a93ed9a8ef5a1921da48c3eb5896180123e7dfc0e5` |
| `graphs/step01_NorthConnex_NSW_unified.graphml` | 105 nodes, 177 edges | `2a45999acf963d3848bb560328f143903c9ee6acae0871506cc28820358e42d7` |
| `graphs/step01_Light_Horse_Interchange_NSW_unified.graphml` | 130 nodes, 181 edges | `861d40c746103eacdb93f1ce81a8a9ae0533a23f7584b487340fe05518fbc36f` |
| `graphs/step01_Domain_Tunnel_VIC_unified.graphml` | 350 nodes, 581 edges | `d788232b50877e1cfd649e10b8b1f2079be9992d342f601ce75be15de6d95280` |
| `graphs/step01_M80_Princes_Freeway_VIC_unified.graphml` | 127 nodes, 188 edges | `6c284e63d6f97f08ad612cc2d415eb15170bcb6e39ec2a158af0913ea6cafb68` |
| `trajectories/step03_Rozelle_Interchange_NSW_trajectories_v5.csv` | 180 trucks, 36,000 fixes | `59f7afe82d7f896601aee2b11c0e9b44be9b5c10860297ba185b57c285294376` |
| `trajectories/step03_West_Gate_Tunnel_VIC_trajectories_v5.csv` | 180 trucks, 36,000 fixes | `bf6e510ce783318fe8e80e2e4d50575e40aea5853db77e487c7290230cc8ead3` |
| `trajectories/step03_NorthConnex_NSW_trajectories_v5.csv` | 180 trucks, 36,000 fixes | `6783c37e9de1f271fbc24272da6826ca35cf40ddfa2659a101d8d80f3858720e` |
| `trajectories/step03_Light_Horse_Interchange_NSW_trajectories_v5.csv` | 180 trucks, 16,200 fixes | `9f8bc44a3e26f10e01b43882b819970ddd890e3ae01ceeca366887d259716c71` |
| `trajectories/step03_Domain_Tunnel_VIC_trajectories_v5.csv` | 180 trucks, 36,000 fixes | `fc7b21d5988c87dbf9e80c3f05c5105b11a164a1bfb2cf4b901b9936560f2b42` |
| `trajectories/step03_M80_Princes_Freeway_VIC_trajectories_v5.csv` | 180 trucks, 9,000 fixes | `57c72d3a1872cb0fc5428e13228136267fc08b09bd805e1f52877ef6022f6c23` |

`SOURCES.json` records the download address, build time and SHA-256 digest
of each input, and `datapackage.json` describes the same files in the
[Frictionless Data Package](https://datapackage.org/) format.

## How the files were produced

```bash
export PYTHONHASHSEED=0
python3 step00b_build_open_road_reference.py
python3 step01_reproduce_unified_graph.py
python3 step03_generate_quantum_trajectories.py --seed 42 --num-trucks 180
```

Running these commands again overwrites the files here. OpenStreetMap and the
government layers are edited over time, so a rebuild at a later date can
differ slightly; the files in this directory are the versions used in the
paper.

Each network edge keeps its OSM attributes, the fused `layer`, `tunnel` and
`bridge` semantics, and the `ref_*` provenance of its reference-road match
(source road identifier, match method and scores). Each trajectory row holds
one 1 Hz fix: the noisy observation (`obs_x`, `obs_y`), the true position and
edge, and the GNSS quality state.
