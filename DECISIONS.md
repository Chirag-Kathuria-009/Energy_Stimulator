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

## Phase 3 (household profiles) notes, 2026-10-24

### 22. What was built and how it differs from the plan
- **`ets households` is a new command** (not in the plan): runs the Phase 3 stage alone (context -> four `Household` objects -> `dim_household`) so the Phase 3 gate can be run before billing and shifting exist. `ets simulate` still reports "not implemented" and will call the same functions once Phases 4-5 exist.
- **Context** (`simulate/context.py`): `load_gold_context` reads dim_time, fact_price_qh, fact_heat_qh and fact_load_profile_qh as NumPy arrays and fails if any table does not line up with dim_time. The PV shape is the one input that is not a gold table: it is aligned from `silver_load_opsd_qh.parquet` (DECISIONS.md #19).
- **`align_to_year(days, ctx, match_daytype, total)`** differs from the plan's signature: it takes `{source date: 96 values}` instead of a value array plus day list, because the source days are chosen per site. Weekday class = Sunday/holiday, Saturday, weekday. Ties in day-of-year distance go to the earlier source date. Source days with 92 or 100 quarter-hours are excluded (a DST day cannot be copied by local clock time). The weekday-class matching is implemented and tested but not used in production yet: PV is weather-driven, so `match_daytype=False`; EV and the heat pump do not come from OPSD (assumptions and when2heat).
- **PV shape:** each OPSD site is aligned to the nearest day of year (843 complete days for r4, 658 for r1), normalised to sum 1, then the two are averaged with equal weight (DECISIONS.md #14). `build_pv` = shape x kWp x 950.
- **Heat pump with two COPs:** heat = `annual_heat x ((1 - dhw_share) x space + dhw_share x water)`; electricity = space/COP_space + water/COP_water; the household's single `hp_cop` array is the effective COP heat/electricity, so Phases 4-5 can keep using `heat / cop`. The power cap is applied to electricity; unserved electricity is carried to the next quarter-hour and logged if it lasts more than one hour or is left over at year end. With the configured 6 kW it never binds.
- **EV sessions:** one per local date, 18:00 -> 07:00 next morning (clock time, so DST nights are 14 and 12 hours long), same energy every day. The night before 1 October (arrival 30 Sep) is not modelled, so the car is not charged between 00:00 and 07:00 on 1 October; the last evening (30 Sep) is cut off at midnight (6 h window, still enough). A window too short for the daily energy is capped at wallbox capacity and logged; this never happens with the configured values.
- **`dim_household`** (in `energy.duckdb`): household_id, archetype (`base`, `ev`, `hp`, `ev+hp+pv`), annual_base_kwh, ev_kwh, hp_kwh (electricity), pv_kwh, params_json (config plus all totals including heat kWh and seasonal COP). Dropped and recreated on every run. dbt does not touch it.

### 23. Validation on the real data (not just on the tests)
- Totals match the YAML: base 3,500 kWh for all four; EV 2,550 kWh (= 15,000 km x 17 / 100) over 365 sessions; heat 15,000 kWh -> 5,096 kWh electricity (seasonal COP 2.94, effective COP 2.07-4.00); PV 7,600 kWh (8 kWp x 950).
- EV: all sessions start at 18:00 and end at 07:00 local; nights with a clock change last 56 and 48 quarter-hours (14 h and 12 h); nothing is charged outside a session; the peak is exactly the wallbox limit (2.75 kWh per quarter-hour = 11 kW); every session delivers exactly its energy.
- PV: zero between 22:00 and 04:00; peak 6.42 kW (8 kWp system) at 13:45 local; monthly kWh Oct..Sep: 357, 312, 184, 183, 300, 640, 807, 932, 1041, 1100, 1017, 728. November is higher than December and January because individual real days from 2015-2018 are copied, so weather noise is part of the shape.
- Heat pump: peak 2.81 kW electric against the 6 kW limit; 51% of the electricity falls in Dec-Feb and 6% in Jun-Aug; the baseline equals heat/COP at every quarter-hour (no carry-over).
- 103 tests (31 new for Phase 3: context, base load, EV windows/DST/weekend/cap, heat pump energy and cap, PV, `align_to_year`, `dim_household`, plus one test against the real gold data that is skipped when the data is absent), ruff clean.

## Phase 4 (tariffs and billing, unshifted) notes, 2026-10-24

### 24. Type-A sanity gate: PASS, 1.42%
`uv run ets baseline` bills every household x eligible tariff variant without any shifting. **Household A_standard on the dynamic tariff costs 1,283.58 EUR/year against 1,302.06 EUR on the fixed tariff: a saving of 1.42%** (gate -2% to +5%, literature about 1-3%). This agrees with the estimate made from the raw prices before any billing code existed (#13: 1.42%, dynamic 1,283.55 EUR; the 0.03 EUR difference is the October repeated-hour split in the gold load profile). The fixed bill is exactly 3,500 kWh x 37.2018 ct = 1,302.06 EUR.

Unshifted annual bills (EUR, saving vs the household's fixed tariff):

| Household | fixed | dynamic | + Module 1 | + Module 2 | + Module 3 | + Module 1 and 3 |
|---|---|---|---|---|---|---|
| A_standard | 1302.06 | 1283.58 (1.42%) | n/a | n/a | n/a | n/a |
| B_ev | 2250.71 | 2246.79 (0.17%) | 2082.63 (7.47%) | 2075.10 (7.80%) | 2246.79 (0.17%) | 2082.63 (7.47%) |
| C_heat_pump | 3197.72 | 3018.15 (5.62%) | 2853.99 (10.75%) | 2675.06 (16.34%) | 3005.71 (6.00%) | 2841.55 (11.14%) |
| D_full | 2781.26 | 2766.81 (0.52%) | 2602.65 (6.42%) | 2328.89 (16.26%) | 2804.77 (-0.85%) | 2640.61 (5.06%) |

These are the starting points for Phase 5; the plan's claim that types B and C gain more than A only after shifting is not tested here. Verified relations on the real data (tests): Module 1 is exactly 164.16 EUR (137.95 x 1.19) below plain dynamic for every household; Module 2 saves 5,096 kWh x 9.43 x 0.6 x 1.19 = 343 EUR for the heat pump; the fixed bill is kWh x price.

### 25. Findings that look odd but are correct
- **B_ev: Module 3 = plain dynamic, to the cent.** The unshifted EV charges at full power from 18:00 and is done after about 2.5 quarter-hours, always inside the 17:00-19:00 standard window (100% of EV energy), whose fee equals the base fee. Module 3 only pays once charging is moved into the low windows (01:30-04:30, 10:00-17:00): that is Phase 5.
- **C_heat_pump: Module 3 gains about 12 EUR.** Its energy falls 37% in the low, 42% in the standard and 21% in the high window; weighted fee 9.23 ct against 9.43 ct.
- **D_full: Module 3 is 38 EUR worse than plain dynamic.** PV covers the base load first and the heat pump's midday (low-window) demand, so the device energy still imported is 17% low, 67% standard, 16% high: weighted fee 9.92 ct (+0.49 ct x 1.19 x 6,504 kWh = 38 EUR).
- **Dynamic saves 5.6% for the heat pump but 1.4% for A.** The energy line saving (about 3.4 ct/kWh) applies to every kWh, while the fixed fees (supplier base fee 71.88 EUR, meter share 30 or 50 EUR) are the same per household; the heat pump has 2.5x the consumption, so the fees weigh less. The saving from "timing" proper (choosing cheap hours) is still zero here: this is the price of the tariff switch alone.

### 26. Modelling choices in Phase 4 and what is not modelled
- **Module variants only for households with a controllable device** (EV or heat pump): A_standard gets `fixed` and `dynamic` only. A household without a device cannot take a §14a module.
- **Reductions apply to device energy only; the base load always pays the standard grid fee** (the Preisblatt's modules cover "separat gemessene" device meters). PV covers the base load first, then the device; exports earn the household's feed-in rate (7.78 ct for D) under every tariff, including the fixed one.
- **Annual bill** = energy cost + 12 x 5.99 EUR (dynamic only) + the household's smart-meter share (dynamic only) - Module 1 credit (gross, 164.16 EUR). The fixed tariff has no fees; they are inside its all-in price.
- **Not modelled: the operator's grid base price.** The Preisblatt lists 80 EUR/year (net) for the metering point under Module 1 and Module 3 and 0 EUR under Module 2. The plan has no grid base price; it is assumed identical for all variants and inside the benchmark. **If the separately measured device under Module 3 carries its own 80 EUR (95.20 EUR gross) metering-point price, Module 3 and Module 1+3 would be 95 EUR worse than shown, which would make B's Module 3 about 4% worse than plain dynamic.** Modules 1 and 2 would be unaffected. To be confirmed with the operator before quoting Module 3 results.
- **Not modelled:** the Preisblatt clause that the total grid charge of a metering point cannot fall below 0 EUR after the Module 1 reduction (never close to binding here), the heat-pump-specific concession fee, the 2026 negative-price rules for small PV (feed-in is paid at all prices), and dimming events.
- 134 tests (39 new: hand-calculated four-quarter-hour bill, fixed bill = kWh x price, negative spot behaviour, Module 2 = 40%, Module 3 windows on DST days, Module 1 credit, PV flows, feed-in, meter fees, eligibility, and eight tests on the real data including the gate). Ruff clean.

## Phase 5 (shifting and scenarios) notes, 2026-10-24

### 27. What was built, and where it differs from the plan
- **PuLP 4.0.1 has a new API** (Rust core): variables come from `prob.add_variable(name, low, up)`, `LpVariable(...)` can no longer be constructed directly, and `prob.solve(pulp.HiGHS(msg=False))` returns a stats object (`.status`, `.has_solution`) instead of an integer; `pulp.LpStatus` is gone. The LP module is written for this API. One session LP takes about 2 ms and one heat-pump day about 7 ms, so a full run is fast.
- **Function signatures:** `apply_shifting(hh, prices, method, ctx, cache)` and `simulate_one(hh, variant, method, ctx, cfg, cache)` take the context (heat-pump days need the local calendar) and an optional cache. `shift_hp_greedy` / `shift_hp_lp` take `days` (list of day slices from `GoldContext.day_slices()`, 92/96/100 slots) instead of the context. The cache is keyed by household, device, method and a hash of the device price array, so variants with identical device prices (dynamic and dynamic_m1; dynamic_m3 and dynamic_m1_m3) share one solve.
- **Heat pump model** (greedy and LP identical constraints): per local day, thermal buffer 10 kWh starting and ending at half full, electricity per quarter-hour at most 6 kW x 0.25, `level[t] = level[t-1] + electricity x COP - heat`, level within [0, buffer]. Planned one day at a time with the day-ahead price, as a controller would.
- **Greedy heat pump rule** (the plan only sketched it): a backward pass gives the level the buffer must have after each slot to still finish the day at the start level (`floor`); a ceiling keeps it from ending above that level; between floor and cap it charges to the cap when the slot's price per unit of heat (price / COP) is at most that of every slot within reach of a full buffer (the window where cumulative future demand reaches the buffer size), otherwise it only does the minimum. A day that cannot be served within the power limit runs just in time at the limit and is logged (never happens with the configured values).
- **EV:** per session, cheapest quarter-hours first (ties: earlier slot) for greedy; one small LP per session for the LP (a window that is too short falls back to greedy and counts as shortfall). Both give the same cost, as expected for this problem.
- **`sim_results_qh` has an extra column `device_kwh`** (EV + heat pump electricity after shifting) so the app (Phase 7) can draw device load before and after shifting; grid import alone cannot show it. `SimResult` got an optional `device_kwh` field.
- **Run bookkeeping:** `sim_runs` records config hash and git commit (with `+dirty` if the tree had uncommitted changes); results and summary are written in one transaction (a failure rolls back, marks the run `failed`, leaves the older results untouched); per-quarter-hour rows of runs older than the newest two are deleted (about 30 MB per run), summaries and run records are kept. `v_latest_summary` shows the newest `ok` run. The summary is computed in Python from the results in memory (not by a second pass over 1.75 million rows) and checked against SQL in the tests.
- **Savings split** (three parts that add up to the saving vs fixed, per row): tariff switch = fixed - dynamic unshifted; timing = dynamic unshifted - dynamic with the row's method; grid fee = dynamic with the method - this variant with the method. The grid-fee part also contains the extra timing gain from shifting into module-specific cheap windows, which cannot be separated.
- **Commands:** `ets simulate` replaces its stub and prints the table and the gate; `ets households` and `ets baseline` stay as separate checks. `ets simulate` takes about 31 s.

### 28. Results (annual bill in EUR, saving vs the household's fixed tariff)
Gate: type A unchanged at 1.42%; B (dynamic, LP) 6.01% and C 11.66% both exceed it: PASS. No EV energy was left undelivered (total shortfall 0).

| Household | Variant | unshifted | greedy | LP |
|---|---|---|---|---|
| A_standard | dynamic | 1284 (1.4%) | n/a | n/a |
| B_ev | dynamic | 2247 (0.2%) | 2115 (6.0%) | 2115 (6.0%) |
| B_ev | dynamic_m1 | 2083 (7.5%) | 1951 (13.3%) | 1951 (13.3%) |
| B_ev | dynamic_m2 | 2075 (7.8%) | 1944 (13.6%) | 1944 (13.6%) |
| B_ev | dynamic_m3 | 2247 (0.2%) | 1966 (12.7%) | 1966 (12.7%) |
| B_ev | dynamic_m1_m3 | 2083 (7.5%) | 1802 (19.9%) | 1802 (19.9%) |
| C_heat_pump | dynamic | 3018 (5.6%) | 2839 (11.2%) | 2825 (11.7%) |
| C_heat_pump | dynamic_m1 | 2854 (10.7%) | 2675 (16.3%) | 2661 (16.8%) |
| C_heat_pump | dynamic_m2 | 2675 (16.3%) | 2503 (21.7%) | 2488 (22.2%) |
| C_heat_pump | dynamic_m3 | 3006 (6.0%) | 2644 (17.3%) | 2612 (18.3%) |
| C_heat_pump | dynamic_m1_m3 | 2842 (11.1%) | 2480 (22.4%) | 2448 (23.4%) |
| D_full | dynamic | 2767 (0.5%) | 2474 (11.0%) | 2486 (10.6%) |
| D_full | dynamic_m1 | 2603 (6.4%) | 2310 (16.9%) | 2322 (16.5%) |
| D_full | dynamic_m2 | 2329 (16.3%) | 2048 (26.4%) | 2055 (26.1%) |
| D_full | dynamic_m3 | 2805 (-0.8%) | 2201 (20.9%) | 2196 (21.1%) |
| D_full | dynamic_m1_m3 | 2641 (5.1%) | 2037 (26.8%) | 2032 (27.0%) |

Timing savings on plain dynamic (EUR, shifted kWh): B 131.4 (2,062 kWh, greedy and LP identical), C greedy 178.7 (2,388) and LP 193.3 (3,099), D greedy 292.8 (4,405) and LP 280.6 (5,113). Greedy gets 92% of the LP's timing saving for the heat pump. Module 1 + 3 is exactly Module 1 plus Module 3 (B: 164.16 + 149.55 = 313.71).

### 29. Findings
- **Tests found a flaw in my first greedy rule.** With a strict `<` (charge only when strictly cheaper than everything within reach), a run of equally cheap slots charged only at its last slot, which cannot fill the buffer: on a cheap-night/expensive-day test day greedy cost 203.8 against 198.4 for doing nothing. Changing to `<=` gives the hand-calculated optimum (198.4 - 30 = 168.4: 5 kWh of heat moved from 20 ct to 2 ct at COP 3). On flat prices the cost is unchanged.
- **Greedy vs LP:** equal for the EV, LP better by 0.5 percentage points for the heat pump (EUR 14), as expected. The LP cost is never above the greedy cost on 20 random days with negative prices (test), and every schedule on the real data satisfies all constraints to 1e-14 (buffer within bounds, end-of-day level, daily heat delivered, power limit).
- **Heat-pump electricity falls by about 93 kWh (1.8%) when optimised**, because the optimiser also moves load to quarter-hours with a better COP; heat delivered is conserved, electricity is not.
- **Shifting is what makes Module 3 pay:** unshifted, Module 3 is worth 0 (B), 12 EUR (C) and -38 EUR (D); shifted into its low windows it is worth 150 (B), 213 (C) and 290 (D) EUR on top of dynamic (LP). The Module 3 base-price caveat (#26, possibly 95 EUR gross) applies in full to these numbers.
- **D_full: the LP is worse than greedy on the bill (2,486 vs 2,474 EUR), although it is better on its own objective.** The optimisers price device energy at the grid price in every quarter-hour (as the plan specifies) and know nothing about the PV surplus that would cover the device for free (opportunity cost: the 7.78 ct feed-in). The LP moves more energy (5,113 vs 4,405 kWh) away from sunny hours and loses self-consumption. D's timing saving (281 EUR with LP) is about 44 EUR (13%) lower than the sum of B's and C's (131 + 193 = 325 EUR), which is roughly the price of being PV-blind. **D's shifting results are therefore conservative, not wrong; a PV-aware optimiser (device energy costs the feed-in rate up to the PV surplus, then the grid price) is the natural next step and is not built.**
- **The price-spike week is not a winter week.** The 7-day window with the highest mean spot price is 10-17 September 2026 (18.7 ct/kWh vs 10.4 for the year). For a heat-pump household that week costs 39 EUR against an average week of 54 EUR, because there is hardly any heating demand. `spike_week_eur` follows the plan's definition (highest price), so it is a price-risk measure for households with flexible or constant load, not a worst-cost week; `worst_month_eur` (January for C, 413 EUR) is the better risk number for the heat pump.

### 30. Limits of the Phase 5 model (ASSUMPTIONS)
- Perfect knowledge of the day's prices; every day planned on its own; no standing losses of the heat buffer, no minimum run or off times, no comfort limits beyond the buffer bounds; electricity price = day-ahead spot plus the tariff components (no imbalance or intraday effects).
- The EV needs the same energy every day and is always plugged in during its window; no battery state-of-charge dynamics, no vehicle-to-grid, no day-to-day flexibility (charging on a cheap Tuesday for a Wednesday).
- Shifted energy is a measure of timing change (sum of |shifted - baseline| / 2), including the heat pump's repeated cycling of the small buffer (61% of its electricity moves with the LP); it is not "kWh moved from evening to night".
- Because the optimisers know prices perfectly and ignore real-world frictions, the savings are an upper bound for what a household could achieve.
- 178 tests (44 new: 18 on the optimisers incl. hand-calculated cases, constraint checks and LP <= greedy on random days; 22 on shifting, scenarios, result tables, failure and rollback; 4 on the real data). Ruff clean.

## Phase 6 (herding) notes, 2026-10-25

### 31. What was built, and where it differs from the plan
- **New config `config/herding.yaml`** (the plan says "all parameters from config"): 1,000 households, seed 42, 30% EV, 20% heat pump, base load ~ normal(3,500, 800) clipped to 1,500-7,000 kWh, EV km ~ normal(15,000, 4,000) clipped to 3,000-40,000, plug-in time +/- 60 minutes (uniform, rounded to quarter-hours), spread k = 16. Validated by `HerdingCfg` (probabilities in [0, 1], min <= mean <= max). **All of these are assumptions, not measured data.** EV and heat-pump households are copies of the first EV and first heat pump in `households.yaml` (wallbox 11 kW, plug-out 07:00; heat pump 6 kW, 10 kWh buffer); no household in the sample has PV.
- **Reproducibility:** all random numbers are drawn up front with fixed array sizes, so one seed gives one population whatever the probabilities are (changing `p_ev` adds or removes EVs but does not change anybody's base load); the spread scenario uses one random generator per household seeded with (seed, index).
- **One pass for all scenarios:** `aggregate_scenarios` builds each household once and adds its net load (base + device - PV) to a running total for each method; the heat-pump solve is shared by the greedy and spread scenarios. `aggregate(cfgs, ctx, cfg, method, variant)` keeps the plan's signature for one method. The full run (1,000 households, three scenarios) takes about 34 s. Only three arrays of 35,040 values are held in memory.
- **Scenarios:** `unshifted`, `greedy` (everybody shifts to the cheapest quarter-hours of the dynamic tariff without module, so all households see the same price) and `greedy_spread`. The mitigation (`shift_ev_spread`) lets each EV charge in random order among its k cheapest quarter-hours; it considers at least as many slots as the energy needs. Only the EVs are spread; the heat pumps use the greedy rule in both shifted scenarios.
- **`peak_report`** returns a Polars DataFrame (pandas is not a dependency). `herding_summary` has one extra column, `total_mwh`. "Night" for the share is the 18:00-07:00 window of the EV plug-in; the share is the fraction of nights in which the highest load of the night falls in the cheapest quarter-hour of that night (ties: earlier).
- Tables `herding_curve(scenario, idx, kw)` and `herding_summary` are replaced in one transaction; a failing write rolls back and keeps the old tables.

### 32. Results (1,000 households, seed 42, dynamic tariff without module)
| Scenario | Peak kW | Peak at (local) | Peak / mean | Nights with the peak in the cheapest quarter-hour | Energy MWh |
|---|---|---|---|---|---|
| unshifted | 2,423 | 2025-12-28 18:15 | 4.01 | 0% | 5,299 |
| greedy | **4,947 (+104%)** | 2026-01-01 23:15 | 8.21 | **74%** | 5,281 |
| greedy_spread (k = 16) | 2,433 (+0%) | 2025-12-25 20:45 | 4.04 | 19% | 5,281 |

- **Herding is real in this model:** when all households follow the same price, the peak doubles and moves from the early evening to the cheapest night quarter-hour; in three quarters of all nights the highest load of the night is exactly there.
- **Energy:** 5,299 MWh unshifted against 5,281 MWh shifted (-0.34%). The EV energy is exactly conserved; the difference is the heat pumps using slightly less electricity in better-COP quarter-hours (#29). The test with EVs only shows exact equality.
- **Sensitivity to k** (peak kW; unshifted 2,423, greedy 4,947): k = 4: 3,931; k = 8: 2,895; k = 16: 2,433; k = 32: 2,101; k = 64: 2,057. From k about 16 the mitigation removes the herding peak; k = 32 even goes about 13% below today's peak, because the EVs then also leave the early-evening peak.
- **What the mitigation costs the household:** for the EV of household B (device price, EUR/year): unshifted 943.2, greedy 811.9 (saves 131.4), spread k = 8: 827.8, k = 16: 837.5, k = 32: 858.2, k = 64: 911.2 (mean of ten seeds). At k = 16 an EV gives up 25.6 EUR a year, 19% of its greedy saving; k = 32 costs 35%, k = 64 76%.
- **Partial adoption** (estimate: blend of the stored curves, assuming the adopters are a random subset): the system peak is below today's up to about 25% of households following the price (-14% at 25%), because shifting first removes load from the evening peak; it exceeds today's peak from about 40% adoption (+18% at 50%, +60% at 75%, +104% at 100%). With the spread mitigation the peak stays at or below today's level at every adoption rate (1,955-2,433 kW from 50% to 100%). The plan notes that only 5.9% of metering points had a smart meter in mid-2026, so the 100% case is a long-term worst case.

### 33. Limits
- A synthetic, seeded population; no feeder or transformer topology, no grid limits (the peak is the sum over 1,000 households, about 2.4 kW per household unshifted, which a real low-voltage feeder with fewer households would see with much higher variance).
- Every household follows exactly the same price with the same rule and perfect foresight: this is the worst case for herding. Different suppliers, tariffs, EV chargers with their own smart charging, or randomised start times would soften it; that is what the spread scenario stands in for.
- The result depends on the 2025/26 price shape (cheap quarter-hours cluster around night and midday) and on the assumed shares of EV and heat-pump households; both are inputs, not findings.
- 198 tests (20 new: sampling, spread optimiser, reproducibility, energy conservation, herding peak and its removal, peak report formulas, table writes and rollback, configuration validation, and one on the real data). Ruff clean.

## Phase 7 (app, CI, refresh, docs) notes, 2026-10-25 (in progress; status in HANDOFF.md)

### 34. What was built so far, and what is still open
- **Environment overrides** `ETS_DATA_DIR`, `ETS_SIM_START`, `ETS_SIM_END` (`settings.py`; `dbt/profiles.yml` reads `ETS_DATA_DIR` through `env_var`, `ets transform` exports it as an absolute path). They exist for one reason: the plan wants CI to run `dbt build` on a committed two-week sample, which needs another data folder and a shorter window. The app additionally reads `ETS_GOLD_DB` (used by its tests).
- **CI sample** (`ets make-sample`, `data/sample/`, 357 KB): window 2026-04-20 to 2026-05-03, chosen because it holds 336 negative-price quarter-hours (the dbt test "at least one negative price" needs them); the weekly SMARD chunks as downloaded, three weeks of the OPSD columns, 16 days of the when2heat columns (decimal comma kept), and the staged BDEW profile CSV. The window is stored in `window.json` and read by the workflow (one place). The sample build uses the committed holiday seed, so `ets seed-holidays` is not run on it (running it would overwrite the tracked seed with fewer years). **Open: the BDEW CSV is a reshaped copy of BDEW's published values; its redistribution terms were not checked.**
- **`ets refresh`** works on a machine with nothing on it: it fetches the price chunks of the simulation year (about 1 MB; existing final chunks are skipped, the newest is always fetched again), checks each chunk against the layout the SQL relies on (`SourceFormatError` otherwise), rebuilds `+fact_price_qh` with dbt and runs the gold checks on the tables it built (`run_gold_checks(..., tables=...)`). **Bug found by simulating a fresh runner:** without `--indirect-selection cautious` dbt also selected tests on tables it does not build (`gold_row_counts`, the relationships test on `fact_load_profile_qh`), so the daily GitHub job would have failed every day; it passed locally only because everything was already built. Fixed (`DBT_SELECTION`), with a regression test that fails without the flag (checked) and a live run on an empty directory (56 chunks, 18 dbt tests, exit 0).
- **Workflows** `ci.yml` (locked install, ruff, pytest, `ets transform` on the sample) and `refresh_prices.yml` (daily 06:00 UTC and manual, uploads `energy.duckdb`). Validated as YAML and their commands run locally; never run on GitHub.
- **App** (`app/streamlit_app.py`, `ets app`): read-only on the result tables (cache keyed by the database file's modification time, one DuckDB cursor per query because the server is multi-threaded); sidebar: household, tariff variants, shifting method, variant for the week and month charts, week; five key figures; charts: annual cost (unshifted vs chosen method), savings split, one week of EV + heat-pump load before/after with the spot price, monthly bill (energy plus one twelfth of the fixed fees), herding curves with the peak of the chosen week marked; provenance line with run id, code version, config hash and sources. The custom-household section is the only place that computes: it builds one household (no PV, because that needs the silver files) from three sliders, runs `simulate_one` for the chosen variants and methods, and shows the result next to all four precomputed households. Missing database or missing tables give a message, not a crash.
- **Acceptance criterion 1 (plan §12) met:** a copy of the project with no `data/` folder and no holiday seed ran `ets ingest`, `seed-holidays`, `transform`, `simulate` and `herding` without manual steps in 12 minutes (729 s) and produced the same numbers as the main folder (type-A gate passes; herding peaks 2,423 / 4,947 / 2,433 kW; 35,040 rows in each gold fact table; 0 null prices). `make` is not installed on this machine, so the Makefile targets were run as the equivalent commands. A first attempt failed only because the scratch folder's path exceeded Windows' 260-character limit inside dbt's scan of the virtual environment.
- **Update 2026-10-25 (later):** lint fixed (`ruff check .` clean). `data/sample/` is now committed (user removed it from `.gitignore`), so the CI sample step can run. README written to plan §13. **Licences checked online (with the user's consent):** OPSD when2heat data CC BY 4.0 (scripts MIT); OPSD household_data CC BY 4.0 seen on the 2017 package pages, the 2020-04-15 version's licence line was not retrievable; SMARD CC BY 4.0 with credit "Bundesnetzagentur | SMARD.de", but SMARD's data-use page ties it to the market-data downloads and the Bundesnetzagentur imprint names "Datenlizenz Deutschland - Namensnennung 4.0" for xls/csv, so the status for the chart-data API is not unambiguous; BDEW H25: no licence or redistribution terms stated on the page, so the BDEW-derived sample CSV stays an open risk. The README says all of this.
- **Still open for Phase 7:** the app screenshot or GIF (placeholder `docs/app.png` in the README, needs a person with a browser) and the final run of the acceptance check (`pytest` and `ruff` after these edits).
