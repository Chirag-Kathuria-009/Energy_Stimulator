# CI sample

A small slice of every source so CI can build the whole silver and gold layer without live APIs.
Regenerate with `uv run ets make-sample` (needs the full bronze data from `ets ingest`).

- Simulation window: 2026-04-20 to 2026-05-03 (local dates, inclusive); `window.json` holds the dates.
- `bronze/smard/`: the raw weekly price chunks of SMARD (Bundesnetzagentur | SMARD.de, CC BY 4.0).
- `bronze/opsd/`, `bronze/when2heat/`: three weeks / a few days of the Open Power System Data files,
  only the configured columns (CC BY 4.0, https://open-power-system-data.org).
- `staging/bdew_profiles_long.csv`: BDEW standard load profiles H25 and P25 reshaped by
  `ets.profiles.bdew_staging` (source: BDEW, https://www.bdew.de/energie/standardlastprofile-strom/).

Use (as CI does):

    ETS_DATA_DIR=/tmp/ets ETS_SIM_START=2026-04-20 ETS_SIM_END=2026-05-03 \
        uv run ets transform      # after copying bronze/ and staging/ into $ETS_DATA_DIR

Annual figures computed on this sample are meaningless (the fixed fees still count 12 months).
