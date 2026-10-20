# Decisions and verification log

Format: what was checked, what was found, source. Checked on 2026-10-18 unless noted.
Items marked **OPEN** need a decision or caveat in a later phase.

## Day-one checklist (plan §10)

### 1. SMARD URL pattern, region, filter 4169
- **Checked:** fetched the index and chunks with curl.
- **Found:** pattern is correct. Index: `https://www.smard.de/app/chart_data/4169/DE-LU/index_quarterhour.json` returns `{"timestamps": [ms, ...]}` (key name `timestamps`, 420 entries, one per week, 2018-09-30T22:00Z to 2026-10-11T22:00Z). Chunk: `.../4169/DE-LU/4169_DE-LU_quarterhour_{ts}.json` returns `series: [[ts_ms, value], ...]`, 672 rows per week (more or fewer on DST weeks: one week has 169 hours). Values are EUR/MWh (e.g. 161.27 on 2026-10-04). Filter 4169 with region `DE-LU` is the day-ahead price. No auth, no bund.dev proxy needed.
- **Context series (checked 2026-10-19):** filters 410, 4068, 4067 return an index under region `DE` (HTTP 200), not `DE-LU`. Config has a separate `smard_context_region`. Their meaning (grid load, solar, wind) is taken from the plan and not cross-checked against the values.
- URL: https://www.smard.de/app/chart_data/4169/DE-LU/index_quarterhour.json

### 2. SMARD dates before 1 October 2025 at `quarterhour` resolution
- **Found:** not missing. SMARD returns 15-minute rows back to 2018, but before 2025-10-01 each hour's four values are identical (100% of hours in chunks up to the week of 2025-09-21; 48 of 168 hours in the week starting 2025-09-28, i.e. the switch happens inside that week; 0% afterwards).
- **Decision:** the pre-October-2025 rows are hourly prices repeated four times. Silver must flag them `source_resolution='h'` by date (before 2025-10-01 local) and not by comparing values. The separate `hour` index is therefore not needed for ingestion. The simulation year starts 2025-10-01 (`settings.SIM_START`), so it contains only true quarter-hour prices.
- **Note:** weekly chunks start at Sunday 22:00 or 23:00 UTC (local Monday 00:00). They are not aligned to a fixed UTC hour.

### 3. OPSD household_data 15-minute CSV
- **URL:** `https://data.open-power-system-data.org/household_data/2020-04-15/household_data_15min_singleindex.csv` (58 MB, 153,810 rows, `utc_timestamp` 2014-12-11T17:45Z to 2019-05-01T22:00Z).
- **Columns (residential1, residential4):** `DE_KN_residential{1,4}_{grid_import, heat_pump, pv}`, plus `DE_KN_residential4_ev`, `..._grid_export`, and appliance columns. Residential1 has no EV.
- **Semantics:** cumulative kWh meter readings, not power. Verified: every channel is monotonically non-decreasing (100% of consecutive non-null pairs); e.g. residential1 grid_import runs from 0.028 to 9096 kWh. Silver must take `lag` differences per channel.
- **Covered ranges (non-null):** r1 grid/heat pump/pv 2015-05-21 to 2017-03-12; r4 heat pump 2015-10-10 to 2017-03-06; r4 EV 2015-10-13 to 2018-02-04; r4 grid/pv 2015-10-10 to 2018-02-04. No overlap with 2025/26: OPSD is used only for device *shapes* (plan §14).
- **Gaps tested, see #15:** the empty rows are mostly before each site's start date, not holes. Within a site's range 661 of 663 days (residential1) are complete.

### 4. when2heat
- **URL:** `https://data.open-power-system-data.org/when2heat/2023-07-27/when2heat.csv` (328 MB), `;` separated, hourly from 2007-12-31T22:00Z.
- **DE columns:** `DE_heat_demand_total` (MW), also `_space`/`_water` and `_SFH/_MFH/_COM` splits; COP columns `DE_COP_{ASHP,GSHP,WSHP}_{floor,radiator,water}` (dimensionless).
- **Tested 2026-10-19 (full file downloaded, scratch only):** (a) values use a **decimal comma** (`3,19`): DuckDB `read_csv` needs `decimal_separator=','`. (b) `DE_heat_demand_*` columns (national MW) are **empty from 2016 on** (61,361 of 131,483 rows null), so the plan's `DE_heat_demand_total` cannot cover 2016-2022 and is also the wrong scale for one household. (c) The single-family-house columns `DE_heat_profile_space_SFH` and `DE_heat_profile_water_SFH` are complete for 2007-2022 and sum to about 1,000,000 per year (2018: 999,978). (d) COP columns are complete: ASHP radiator 1.46-4.06 (mean 3.48), within the planned pandera range 1-8.
- **Decision:** use the SFH profile columns for space and water, `DE_COP_ASHP_radiator` for space, `DE_COP_ASHP_water` for water. Hot-water share of SFH heat from the 2013-2015 national SFH demand columns: 14.7%, 17.3%, 16.5%; `dhw_share: 0.165` (2015) is configured. 2015 seasonal COP from the same data: 2.99 (space radiator + water), a realistic air-source heat pump value, so no COP tuning is needed.
- **Plan impact:** Phase 2 `silver_heat_qh` reads two profiles and two COP columns, not one demand and one COP; Phase 3 `build_heat_pump` mixes them with `dhw_share`.
- **Caveat:** the file is 328 MB with all countries; extracting the DE columns takes about 1 s with Polars, so streaming or splitting is not needed. when2heat covers weather years 2008-2022; the shape is mapped onto 2025/26 by calendar (Phase 3).

### 5. BDEW H25 / P25
- **Download:** a single workbook with H25, G25, L25, P25, S25 (manual-download-free, direct URL works with curl): https://www.bdew.de/media/documents/Kopie_von_Repr%C3%A4sentative_Profile_BDEW_H25_G25_L25_P25_S25_Ver%C3%B6ffentlichung.xlsx. Guide: https://www.bdew.de/media/documents/2025-03-17_AWH_Aktualisierte_SLP_Strom_2025_Ver%C3%B6ffentlichung.pdf. Config still expects the file at `data/manual/bdew_slp_2025.xlsx`; `bdew.py` in Phase 1 may download it directly if missing.
- **Layout:** one sheet per profile; 96 rows (15-min) by 36 value columns = 12 months x (SA = Saturday, FT = Sunday/holiday, WT = weekday). Header cell says "normiert auf 1 Mio kWh Jahresverbrauch" (1,000,000 kWh/year), so scale = annual_kwh / 1,000,000.
- **Dynamisation:** H25 and P25 require it. `x = x0 * (-3.92e-10 t^4 + 3.20e-7 t^3 - 7.02e-5 t^2 + 2.10e-3 t + 1.24)`, t = day of year (1..365/366), factors rounded to 4 decimals, result to 3 decimals. The formula is an image in the PDF (read from the rendered image). G25 and L25 take none. After dynamisation the yearly sum does not equal exactly 1,000,000, so Phase 2/3 must rescale to the household's annual kWh.
- **IMPORTANT, changes the plan:** P25 is **not a PV generation profile**. BDEW describes it as the net grid-offtake profile of a household that has PV (midday value ~6 vs ~33 for H25 in July, December nearly unchanged). `build_pv = P25 x kWp x yield` in the plan would be wrong. **Resolved by experiment, see #14:** the PV shape comes from the OPSD `pv` channels (mean of residential1 and residential4), scaled to `kwp x specific_yield`; P25 is not used for generation. A clear-sky model is the fallback.

### 6. Grid operator Preisblatt for §14a
- **Chosen:** GELSENWASSER Energienetze GmbH, Preisblatt Netznutzungsentgelte Strom, valid from 2026-01-01 (Stand 18.12.2025): https://www.gw-energienetze.de/fileadmin/gw-energienetze/30_marktpartner/30.2_netznutzungsentgelte/preisblatt_gwn_strom_2026.pdf
- **Why this operator:** year-round Module 3 windows that cover the whole day (EWR Netz applies its windows to Q1+Q4 only), and all amounts are in the primary PDF.
- **Values (all NET of VAT):** standard NS grid work price 9.43 ct/kWh plus 80 EUR/yr base price. Module 1: grid-fee reduction of 137.95 EUR/yr (gross 164.16). Module 2: 3.77 ct/kWh and 0 EUR base price for the separately metered device (= 0.3998 x 9.43, matches the 0.40 factor). Module 3: low 3.77, standard 9.43, high 18.71 ct/kWh. Windows, same in all four quarters: low 01:30-04:30 and 10:00-17:00; standard 22:00-01:30, 04:30-07:00, 08:00-10:00, 17:00-19:00; high 07:00-08:00 and 19:00-22:00.
- **Pitfall found:** the comparison site 14a-atlas.de lists the low-load window wrongly (puts 10:00-17:00 in "high" and 22:00-01:30 in "low"). The windows above come from the rendered page 6 of the PDF. Lesson: do not copy tariff windows from aggregator pages.
- **Preisblatt wording:** modules apply to *separately measured* controllable devices ("separat gemessene Entnahme"). That matches the plan's assumption that reductions apply to the device load only.
- **Resolved by experiment, see #13:** the grid fee is 9.43 ct everywhere and the fixed benchmark is rebuilt from components (37.20 ct).
- **Module combinations:** Module 1 and Module 3 can be combined (since 2025-04-01, per the plan's §1 table); Modules 2 and 3 cannot. The config rejects 2+3. Module 3 does not require Module 1, so `dynamic_m3` (Module 3 alone) and `dynamic_m1_m3` both stay as variants. (An earlier note here said Module 3 requires Module 1; that came from a search snippet and was wrong.)

### 7. Smart-meter annual fee
- **Primary source (read 2026-10-19):** MsbG §30, https://www.gesetze-im-internet.de/messbg/__30.html. Caps are gross per year, split into a grid-operator share (80 EUR) and the connection user's share. 6,000-10,000 kWh: 120 total, user pays 40. 10,000-20,000 kWh **or a controllable device under §14a EnWG**: 130 total, user pays 50. Voluntary installation (§30(3)): 60 total, user pays 30. Bands below 6,000 kWh are not in the retrieved text.
- **Decision:** per household, as the user share: type A (voluntary smart meter for a dynamic tariff) 30 EUR; B, C, D (each has a §14a device) 50 EUR. The field moved from the supplier block to `households.yaml`. The earlier flat 40 EUR (from a secondary source) was wrong for all four households.
- **Caveat:** treating the "Anschlussnutzer" share as what the household pays, and the grid-operator share as covered by grid fees, is my reading of the table; the type-A saving is sensitive to this: meter fee 20/30/40/50 EUR gives 2.19% / 1.42% / 0.65% / -0.11%.

### 8. Supplier markup and base fee for the dynamic tariff
- **Found (secondary source, Stand April 2026, via a price-comparison summary):** Tibber: base fee 5.99 EUR/month plus 2.15 ct/kWh procurement markup. aWATTar HOURLY: 4.58 EUR/month, 1.5 ct/kWh. Tibber's own pages did not show figures (tibber.com/de/stromtarif has no numbers; /de/preise returns 404).
- **Decision (ASSUMPTION):** use Tibber. Gross vs net is not stated; treated as gross, so net markup = 2.15 / 1.19 = 1.81 ct/kWh (VAT re-added in billing) and base fee 5.99 EUR/month gross. If it was net, the markup is understated by 0.41 ct/kWh (about 1% of the price). Sensitivity to be shown in the README.
- **Sensitivity (type A, tested on real prices, see #13):** Tibber with markup 2.15 treated as net instead of gross: saving about 1 percentage point lower; aWATTar (1.26 ct net, 4.58 EUR/month) about 1.7 points higher. Tibber-as-gross is the middle option and stays.
- **OPEN:** replace with primary figures when found.

## Other decisions

### 9. Package and project name
Folder stays `Energy_Stimulator`; `pyproject` name `energy-tariff-simulator` (plan §6) and package `ets` via `src/ets`, `uv run ets ...` as in the phase doc. The `uv init` stub `main.py` was removed.

### 10. Plan files are gitignored
`ENERGY_SIMULATOR_PLAN.md` and `IMPLEMENTATION_PHASES.md` stay out of the repo (user's choice, 2026-10-18). Because they are not versioned, DECISIONS.md records every deviation from them.

### 11. Deviations from the plan's config shape
- `tariffs.yaml` is `{supplier: {...}, variants: [...]}` instead of a flat list. Markup, base fee and meter fee are shared by all dynamic variants, so repeating or inheriting them is avoided.
- `grid_14a.yaml` stores Module 1 as `module1_credit_eur_year_net` (the Preisblatt is net). `AppConfig.module1_credit_eur_year_gross` multiplies by `1 + vat_rate`.
- `grid_14a.yaml` has `grid_operator` and `source_url` as required fields (so the operator is always named).
- `price_components.yaml` has `energy_ct_kwh` and the fixed all-in price is computed, not stored (#13). The smart-meter fee moved to `households.yaml` (#7).
- `sources.yaml` carries OPSD and when2heat column names so silver SQL does not hardcode them.
- Module 3 windows may wrap past midnight (`22:00-01:30`); the validator expands them to quarter-hour indices and checks every one of the 96 is covered exactly once.

### 12. PV feed-in tariff (type D)
EEG partial-feed-in rate for systems up to 10 kWp: 7.78 ct/kWh until 31 July 2026, 7.70 ct/kWh from 1 August 2026 (secondary sources; Bundesnetzagentur confirmation was reported as pending). The simulation year straddles both; 7.78 is used as one flat value (ASSUMPTION; 0.08 ct difference). Specific yield 950 kWh/kWp is a typical German assumption, not verified. https://www.t-online.de/heim-garten/aktuelles/id_101353222/einspeiseverguetung-sinkt-ab-august-das-aendert-sich-fuer-solar-besitzer.html

## Experiments run to settle the open items (2026-10-19)

All experiments were scratch scripts on real downloaded data, not pipeline code. No phase was started. Inputs: SMARD quarter-hour prices for the whole simulation year (35,040 rows, 0 nulls, 2,052 negative quarter-hours, mean 104.05 EUR/MWh = 10.40 ct/kWh, min -499.99, max 747.10); BDEW H25 dynamised and scaled to 3,500 kWh (calendar of 2025/26, local time, DST handled by converting from UTC; Sundays and holidays = FT, Saturdays and 24/31 Dec = SA).

### 13. Type-A gate and grid-fee option (settles plan changes 2, 3 and 7)
Load-weighted spot is 10.47 ct vs time-average 10.40 ct (H25 load is slightly price-correlated, +0.065 ct). Fixed bill for 3,500 kWh at 37.0 ct = 1,295.00 EUR.

| Option (supplier: Tibber net markup 1.81, base 5.99/month, meter 30 EUR) | fixed EUR | dynamic EUR | saving |
|---|---|---|---|
| A. 9.43 ct everywhere, fixed benchmark left at 37.0 ct | 1295.00 | 1283.55 | 0.88% |
| **A+. 9.43 ct everywhere, fixed benchmark rebuilt from components (37.20 ct)** | 1302.06 | 1283.55 | **1.42%** |
| B. 9.24 ct (BDEW average) for base load, plan original | 1294.15 | 1275.63 | 1.43% |

- A+ and B give the same type-A result (1.42% vs 1.43%, inside the literature range of 1-3%), but only A+ is consistent with the Preisblatt: Module 2 = 0.40 x 9.43 = 3.772 ct (published 3.77, +0.05%) and Module 3 standard window = base fee (difference 0.00 ct). Under B, Module 2 would be 3.696 ct (-1.96% vs published) and the Module 3 standard window would cost 0.19 ct more than no module.
- **Chosen: A+.** Consequence for later phases: the fixed benchmark is 37.20 ct, not 37.0 (config computes it); Phase 4's hand test "fixed bill = kWh x 37.0 / 100" becomes "x 37.20 / 100".
- **The gate depends on fixed fees.** Without base fee and meter fee the same model gives about 9% saving (outside the gate), because the energy line shrinks from 15.17 ct to about 12.3 ct (spot + markup). The plan's "above about 10% means a fixed component is missing" is correct, but note that suppliers' fixed fees are exactly what brings the number down. Type A on a dynamic tariff saves about 1.4% all-in.
- Other tested variants (grid 9.24, all-in saving): Tibber markup treated as net (2.15 ct): -0.37%; aWATTar (1.26 ct, 4.58 EUR/month): 3.80%; meter fee 20 EUR: 2.27%. All are inside the -2% to +5% gate. Inputs are chosen from sources, not tuned to hit a target.

### 14. PV shape (settles plan change 1)
Tested on July weekday shapes (96 quarter-hours), real OPSD residential1 and residential4 PV channels as the reference:

| Option | Result |
|---|---|
| A. OPSD `pv` channels | Night share 0.0000 for both sites; peak 13:00 (r1) and 14:30 (r4) local time; r1 vs r4 shape correlation 0.948 (different orientation). kWh/day r1: Jan 4.6, Jun 44.9, Dec 7.2; r4: Jan 10.4, Jun 48.8, Dec 13.0 |
| B. Clear-sky model (sin of solar elevation) | Correlation with r1 0.968 and r4 0.974 (higher than the two real sites with each other); RMSE of normalised shapes 0.0035 and 0.0029 vs 0.0039 between the real sites |
| C. P25 as PV (H25 minus P25 at equal annual energy) | 28% of quarter-hours are negative; correlation only 0.84 / 0.89. Rejected: not a generation profile |

- **Chosen: A** (mean of both sites), because it is closest to the plan's approach (OPSD for device shapes) and has no new code or inputs. B is equally close to the data, so it is the documented fallback.
- Weather years 2015-2018 do not match 2025/26 prices, so the real cloud variability of A adds no price correlation. It adds realism in shape only; the model B would be as defensible. The decision is therefore about effort, not accuracy.
- Annual PV energy is `kwp x specific_yield` (8 x 950 = 7,600 kWh, ASSUMPTION), not taken from OPSD, whose sites' kWp are unknown.

### 15. OPSD gap policy (settles plan change 3 in the replanning list: gaps)
Policies: P1 full days only (at least 95% of quarter-hours valid in the local day, increments between consecutive non-null cumulative readings, negatives dropped); P2 as P1 after linearly interpolating cumulative gaps of up to 4 quarter-hours. Usable days (of days between a site's first and last reading): r1 grid/heat pump/PV 661 of 663; r4 grid/PV 848 of 850; r4 EV 845 of 847; r4 heat pump 512 of 514. P1 and P2 gave identical counts, and every channel covers all 36 (month, day type) cells.
- **Chosen: P1** (simplest). No interpolation code is needed. Silver keeps only complete days; `align_to_year` can find a source day for every (month, day type).

### 16. Hourly series before 2025-10-01 (settles plan change 4)
A week chunk at `quarterhour` and the same week at `hour` resolution (2025-08-31) match exactly: 0 mismatches in 672 rows. So the plan's union with the hourly series adds no information.
- **Chosen:** drop the union in `silver_price_qh`; flag `source_resolution='h'` by date. Keep the `expand_hourly_to_qh` macro only for when2heat (hourly) and its test.

### 17. Not changed
- Supplier choice stays Tibber with assumed-gross figures (#8), pending primary figures.
- `dynamic_m3` stays as Module 3 alone, next to `dynamic_m1_m3`.

## Phase 1 (ingest) notes, 2026-10-20

### 18. Deviations and results
- **Chunk skip rule is stricter than the plan's.** The plan skips any existing chunk except the newest. A chunk first downloaded while it was the newest week can hold null future slots, so it would stay stale forever. A chunk is skipped only if its manifest `fetched_at_utc` is after the chunk's end (`chunk_end_ms` stored in `extra`). Covered by `test_fetch_range_refetches_chunk_that_was_fetched_before_its_week_ended`.
- **`save_bronze` takes raw bytes**, not a parsed dict, so bronze is byte-identical to what SMARD sent. `fetch_chunk` returns bytes for the same reason.
- **Context series are optional.** A failure of 410/4068/4067 is a warning; a failure of the price series or any other source fails `ets ingest` (exit 1). Coverage is checked on real values: every quarter-hour of the simulation year must have a non-null price in the bronze files (35,040 slots), not just a chunk-count check.
- **BDEW file is downloaded automatically** from the direct URL into `data/manual/bdew_slp_2025.xlsx` when absent; the manual-download error message is only the fallback.
- **Result of the first full run:** 6 min 12 s; 108 price chunks (2024-09 to 2026-10, covering the simulation year with no gaps), 56 chunks each for 410, 4067, 4068; 276 SMARD files; manifest has 279 entries, 393 MB (377 MB on disk under `data/bronze`). Series 4068 starts later than the others (first chunk 2025-12-28), which does not matter while the series is unused.
- **Idempotence:** a second run took 12.7 s and re-fetched exactly the four newest chunks (one per SMARD series); OPSD, when2heat and BDEW were skipped by checksum.
- The meaning of the context filters (grid load, solar, wind) is still taken from the plan and not cross-checked against values.

## Phase 2 (transform) notes, 2026-10-20

### 19. Deviations from the plan, and why
- **Bronze readers are macros (`dbt/macros/bronze.sql`), not `models/sources.yml`.** The `read_json(..., columns={...})` expressions contain braces that clash with dbt-duckdb's `external_location` string formatting; macros are also easy to point at temp files in tests.
- **BDEW workbook is converted to a long CSV by Python** (`profiles/bdew_staging.py`, openpyxl, new dependency) into `data/staging/`, because DuckDB's Excel reader needs a network extension install. The bronze workbook is untouched.
- **Dynamisation moved from silver to gold.** `silver_bdew_profiles_qh` is the raw month x day type x quarter-hour table (kWh per quarter-hour per 1,000,000 kWh/year); the factor depends on the date, so it is applied in `fact_load_profile_qh`, together with the day-type rule (Sunday/holiday FT; Saturday, 24 and 31 Dec SA) and the October repeated-hour split (each copy gets half). Result: each profile sums to exactly 1000 per 1000 kWh/year over the simulation year. Phase 3: `profile * annual_kwh / 1000`. The March day simply lacks the 02:00 hour (its daily energy is about 4% lower).
- **`fact_heat_qh` columns:** `heat_space_norm, heat_water_norm, cop_space, cop_water` (each heat series sums to 1), not the plan's `heat_demand_norm, cop`, because the space/water split is a household parameter (`dhw_share`, Phase 3). Appendix B of the plan is out of date on this.
- **Weather season (new config `when2heat_season_start_year: 2014`):** gold maps the simulation year onto when2heat's season Oct 2014 - Sep 2015 by local clock time (sim Oct-Dec -> Oct-Dec 2014, Jan-Sep -> Jan-Sep 2015) and re-normalises. 2014/15 was chosen because its monthly heat-demand shares are closest to the mean of the 14 complete seasons (RMSE 0.0092, lowest of 14; runner-up 2019/20 at 0.0097, so the choice is not sharp). The simulation does not use 2025/26 weather.
- **OPSD shapes stay in silver.** `data/silver/silver_load_opsd_qh.parquet` (site, channel, ts_utc, kwh, is_estimated, date_local; complete local days only) is read by Phase 3 (`align_to_year`, EV/PV/HP shapes). There is no gold table for it. This is the one place where a later phase reads silver, not gold.
- **`ets transform` is the only supported entry point for dbt.** It injects config as dbt vars (paths, simulation window, OPSD and when2heat column names, season year, `n_qh`) so there is one source of truth; direct `dbt build` only works for the scalar defaults in `dbt_project.yml`. dbt runs in a subprocess so the DuckDB file lock is released before the pandera checks open the database.
- **Extra generic/singular tests beyond the plan:** `not_null` on gold prices and COP, `gold_row_counts` (N rows per fact, N per profile), `profile_sums` (load sums to 1000, heat sums to 1).

### 20. Source quirks found while building (all handled)
- DuckDB auto-detects the CSV timestamp column as `TIMESTAMPTZ`; both CSV readers force it to `VARCHAR` and parse with `strptime`.
- when2heat has **one missing hour at each autumn DST change** (15 holes, 2008-2022). Without handling, gold `fact_heat_qh` had 8 null COP quarter-hours on 2025-10-26. `silver_heat_qh` now fills a single missing hour with the mean of its neighbours and flags `is_estimated` (15 hours); longer gaps would stay null and fail the `not_null` tests.
- DuckDB/ICU resolves ambiguous local times (repeated October hour) to the second occurrence and non-existent spring times to an earlier hour; relevant only to the season mapping, where duplicates are absorbed by the re-normalisation.
- OPSD silver (full local days only, gaps of up to 4 quarter-hours spread evenly): residential1 grid/heat pump/PV 661 days, residential4 grid/PV 848, EV 845, heat pump 512; every channel covers all 36 (month, day type) cells; no estimated rows were needed (P1 = P2, as in #15). Annualised from the kept days: r1 grid ~5,021 kWh, r1 heat pump ~5,638, r4 EV ~960, r4 PV ~10,578 kWh/yr.

### 21. Validation of the output (not just green tests)
- Gold row counts: `dim_time`, `fact_price_qh`, `fact_heat_qh` 35,040 each; `fact_load_profile_qh` 70,080 (35,040 per profile). Null prices: 0. Estimated prices: 0. All prices `source_resolution='qh'`.
- Every one of the 35,040 prices equals the raw bronze value (0 mismatches). 2,052 negative quarter-hours and mean 104.045 EUR/MWh, identical to the scratch experiment in #13.
- DST days: 2025-10-26 has 100 quarter-hours, 2026-03-29 has 92; 9 national holidays fall in the simulation year.
- Load-weighted spot price with the gold H25 profile: 10.4701 ct/kWh (scratch experiment 10.469; the 0.001 difference is the October repeated-hour split), time average 10.4045.
- H25 shape: 12.9% of energy before 05:00 local, 23.7% between 17:00 and 21:00; July noon P25 is 5.3 vs H25 28.3 per 1000 kWh (PV effect), consistent with #5.
- Heat: space-heat shares by month peak in December (17.2%) and fall to 0.7% in August; space COP 2.09-4.06 (mean 3.50), water COP 1.94-4.00, inside the 1-8 check.
- Timings: `ets transform` about 33 s (silver heat 18 s dominates). 72 tests, ruff clean.
