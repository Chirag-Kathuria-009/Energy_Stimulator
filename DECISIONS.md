# Decisions and verification log

Format: what was checked, what was found, source. Checked on 2026-10-18 unless noted.
Items marked **OPEN** need a decision or caveat in a later phase.

## Day-one checklist (plan §10)

### 1. SMARD URL pattern, region, filter 4169
- **Checked:** fetched the index and chunks with curl.
- **Found:** pattern is correct. Index: `https://www.smard.de/app/chart_data/4169/DE-LU/index_quarterhour.json` returns `{"timestamps": [ms, ...]}` (key name `timestamps`, 420 entries, one per week, 2018-09-30T22:00Z to 2026-10-11T22:00Z). Chunk: `.../4169/DE-LU/4169_DE-LU_quarterhour_{ts}.json` returns `series: [[ts_ms, value], ...]`, 672 rows per week (more or fewer on DST weeks: one week has 169 hours). Values are EUR/MWh (e.g. 161.27 on 2026-10-04). Filter 4169 with region `DE-LU` is the day-ahead price. No auth, no bund.dev proxy needed.
- **Not yet checked:** context filters 410, 4068, 4067 (optional series, Phase 1).
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
- **OPEN:** a lot of rows are empty (63k of 154k for r1). Silver needs a plan for gaps before computing shapes.

### 4. when2heat
- **URL:** `https://data.open-power-system-data.org/when2heat/2023-07-27/when2heat.csv` (328 MB), `;` separated, hourly from 2007-12-31T22:00Z.
- **DE columns:** `DE_heat_demand_total` (MW), also `_space`/`_water` and `_SFH/_MFH/_COM` splits; COP columns `DE_COP_{ASHP,GSHP,WSHP}_{floor,radiator,water}` (dimensionless).
- **Decision (ASSUMPTION):** use `DE_heat_demand_total` and `DE_COP_ASHP_radiator` (air-source, radiator heating, the commonest German retrofit). Configured in `sources.yaml`.
- **Caveat:** the file has 300+ columns for all countries and is much bigger than the plan's "keep DE columns only" suggests. Phase 1 downloads it whole and Phase 2 keeps DE columns. when2heat covers weather years 2008-2022, so the shape must be mapped onto 2025/26 by calendar (Phase 3).

### 5. BDEW H25 / P25
- **Download:** a single workbook with H25, G25, L25, P25, S25 (manual-download-free, direct URL works with curl): https://www.bdew.de/media/documents/Kopie_von_Repr%C3%A4sentative_Profile_BDEW_H25_G25_L25_P25_S25_Ver%C3%B6ffentlichung.xlsx. Guide: https://www.bdew.de/media/documents/2025-03-17_AWH_Aktualisierte_SLP_Strom_2025_Ver%C3%B6ffentlichung.pdf. Config still expects the file at `data/manual/bdew_slp_2025.xlsx`; `bdew.py` in Phase 1 may download it directly if missing.
- **Layout:** one sheet per profile; 96 rows (15-min) by 36 value columns = 12 months x (SA = Saturday, FT = Sunday/holiday, WT = weekday). Header cell says "normiert auf 1 Mio kWh Jahresverbrauch" (1,000,000 kWh/year), so scale = annual_kwh / 1,000,000.
- **Dynamisation:** H25 and P25 require it. `x = x0 * (-3.92e-10 t^4 + 3.20e-7 t^3 - 7.02e-5 t^2 + 2.10e-3 t + 1.24)`, t = day of year (1..365/366), factors rounded to 4 decimals, result to 3 decimals. The formula is an image in the PDF (read from the rendered image). G25 and L25 take none. After dynamisation the yearly sum does not equal exactly 1,000,000, so Phase 2/3 must rescale to the household's annual kWh.
- **IMPORTANT, changes the plan:** P25 is **not a PV generation profile**. BDEW describes it as the net grid-offtake profile of a household that has PV (midday value ~6 vs ~33 for H25 in July, December nearly unchanged). `build_pv = P25 x kWp x yield` in the plan would be wrong. **OPEN for Phase 3:** options are (a) OPSD residential1/4 `pv` increments scaled to kWp (real 2015-17 shape, 8 kWp assumed) or (b) a simple clear-sky-based PV model. Recommendation: (a). It also keeps type D cheap to build.

### 6. Grid operator Preisblatt for §14a
- **Chosen:** GELSENWASSER Energienetze GmbH, Preisblatt Netznutzungsentgelte Strom, valid from 2026-01-01 (Stand 18.12.2025): https://www.gw-energienetze.de/fileadmin/gw-energienetze/30_marktpartner/30.2_netznutzungsentgelte/preisblatt_gwn_strom_2026.pdf
- **Why this operator:** year-round Module 3 windows that cover the whole day (EWR Netz applies its windows to Q1+Q4 only), and all amounts are in the primary PDF.
- **Values (all NET of VAT):** standard NS grid work price 9.43 ct/kWh plus 80 EUR/yr base price. Module 1: grid-fee reduction of 137.95 EUR/yr (gross 164.16). Module 2: 3.77 ct/kWh and 0 EUR base price for the separately metered device (= 0.3998 x 9.43, matches the 0.40 factor). Module 3: low 3.77, standard 9.43, high 18.71 ct/kWh. Windows, same in all four quarters: low 01:30-04:30 and 10:00-17:00; standard 22:00-01:30, 04:30-07:00, 08:00-10:00, 17:00-19:00; high 07:00-08:00 and 19:00-22:00.
- **Pitfall found:** the comparison site 14a-atlas.de lists the low-load window wrongly (puts 10:00-17:00 in "high" and 22:00-01:30 in "low"). The windows above come from the rendered page 6 of the PDF. Lesson: do not copy tariff windows from aggregator pages.
- **Preisblatt wording:** modules apply to *separately measured* controllable devices ("separat gemessene Entnahme"). That matches the plan's assumption that reductions apply to the device load only.
- **OPEN (Phase 4):** (a) `grid_fee_ct_kwh` in `price_components.yaml` is the BDEW average 9.24, while this operator's standard rate is 9.43. Phase 4 must decide which one applies to base load; the type-A sanity gate is the check. (b) Module 3 requires Module 1 (they combine; Module 2 and 3 do not). Config enforces that 2 and 3 cannot be combined.

### 7. Smart-meter annual fee
- **Found (secondary source):** statutory annual price caps for an intelligent metering system since 2025-01-01: 40 EUR for 6,000-10,000 kWh/yr; 50 EUR for 10,000-20,000. The lower bands were not in the retrieved table (the source lists only the bands from "under 6,000 to 10,000"). Source: https://www.verbraucherzentrale-niedersachsen.de/themen/energie-wohnen/energiepreise-anbieterwechsel/smart-meter-preisobergrenzen-gestiegen. A modern (non-smart) meter costs at most 20 EUR/yr.
- **Decision (ASSUMPTION):** `smart_meter_fee_eur_year: 40.0` for every dynamic variant. Households B, C, D (EV, heat pump) exceed 6,000 kWh/yr and fall in this band; for type A (3,500 kWh) the true cap is probably lower, so this slightly overstates A's fixed cost. Gross vs net is not stated by the source; treated as gross.
- **OPEN:** confirm the exact cap bands from MsbG §31 before quoting results.

### 8. Supplier markup and base fee for the dynamic tariff
- **Found (secondary source, Stand April 2026, via a price-comparison summary):** Tibber: base fee 5.99 EUR/month plus 2.15 ct/kWh procurement markup. aWATTar HOURLY: 4.58 EUR/month, 1.5 ct/kWh. Tibber's own pages did not show figures (tibber.com/de/stromtarif has no numbers; /de/preise returns 404).
- **Decision (ASSUMPTION):** use Tibber. Gross vs net is not stated; treated as gross, so net markup = 2.15 / 1.19 = 1.81 ct/kWh (VAT re-added in billing) and base fee 5.99 EUR/month gross. If it was net, the markup is understated by 0.41 ct/kWh (about 1% of the price). Sensitivity to be shown in the README.
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
- `sources.yaml` carries OPSD and when2heat column names so silver SQL does not hardcode them.
- Module 3 windows may wrap past midnight (`22:00-01:30`); the validator expands them to quarter-hour indices and checks every one of the 96 is covered exactly once.

### 12. PV feed-in tariff (type D)
EEG partial-feed-in rate for systems up to 10 kWp: 7.78 ct/kWh until 31 July 2026, 7.70 ct/kWh from 1 August 2026 (secondary sources; Bundesnetzagentur confirmation was reported as pending). The simulation year straddles both; 7.78 is used as one flat value (ASSUMPTION; 0.08 ct difference). Specific yield 950 kWh/kWp is a typical German assumption, not verified. https://www.t-online.de/heim-garten/aktuelles/id_101353222/einspeiseverguetung-sinkt-ab-august-das-aendert-sich-fuer-solar-besitzer.html
