"""Dynamic tariff simulator: a read-only view on the result tables.

Everything on this page is read from the DuckDB file written by `ets simulate` and `ets herding`,
except the "custom household" section, which runs one household through the simulator on demand.
Not tariff advice: a model on assumptions that are listed in DECISIONS.md.
"""

import os
from pathlib import Path

import duckdb
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from ets.config import HouseholdCfg, load_config
from ets.profiles.archetypes import build_household
from ets.settings import GOLD_DB
from ets.simulate.context import load_gold_context
from ets.simulate.run import simulate_one
from ets.tariffs.billing import build_variants, has_device

QH_PER_WEEK = 7 * 96
METHOD_LABEL = {"none": "unshifted", "greedy": "greedy shifting", "lp": "optimal shifting (LP)"}
VARIANT_LABEL = {
    "fixed": "Fixed tariff",
    "dynamic": "Dynamic",
    "dynamic_m1": "Dynamic + Module 1",
    "dynamic_m2": "Dynamic + Module 2",
    "dynamic_m3": "Dynamic + Module 3",
    "dynamic_m1_m3": "Dynamic + Module 1 and 3",
}
COLORS = {"none": "#8c8c8c", "greedy": "#2a9d8f", "lp": "#1f5fbf"}
SPLIT_COLORS = {"Tariff switch": "#e9c46a", "Timing": "#2a9d8f", "Grid fee module": "#1f5fbf"}
DB_PATH = Path(os.environ.get("ETS_GOLD_DB") or GOLD_DB)

st.set_page_config(page_title="Dynamic tariff simulator", layout="wide")


# ------------------------------------------------------------------ data access (read-only)


@st.cache_resource
def get_con(path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(path, read_only=True)


def _stamp() -> float:
    """Changes whenever `ets simulate` rewrites the database, so cached data is never stale."""
    return DB_PATH.stat().st_mtime


def rows(sql: str, params: tuple = ()) -> list[dict]:
    cur = get_con(str(DB_PATH)).cursor()  # one cursor per call: the app is multi-threaded
    return cur.execute(sql, list(params)).to_arrow_table().to_pylist()


@st.cache_data
def load_summary(stamp: float) -> list[dict]:
    return rows("select * from v_latest_summary order by household_id, variant_id, method")


@st.cache_data
def load_run(stamp: float) -> dict:
    found = rows(
        "select run_id, finished_at, git_sha, config_hash from sim_runs "
        "where status = 'ok' order by finished_at desc limit 1"
    )
    return found[0] if found else {}


@st.cache_data
def load_week(
    stamp: float, household: str, variant: str, method: str, first_idx: int
) -> list[dict]:
    return rows(
        "select r.idx, t.ts_local, p.price_ct_kwh, r.device_kwh, r.cost_ct "
        "from sim_results_qh r join dim_time t using (idx) join fact_price_qh p using (idx) "
        "where r.run_id = (select run_id from sim_runs where status = 'ok' "
        "order by finished_at desc limit 1) and r.household_id = ? and r.variant_id = ? "
        "and r.method = ? and r.idx >= ? and r.idx < ? order by r.idx",
        (household, variant, method, first_idx, first_idx + QH_PER_WEEK),
    )


@st.cache_data
def load_monthly(stamp: float, household: str, variant: str, method: str) -> list[dict]:
    return rows(
        "select t.month, sum(r.cost_ct) / 100 as energy_eur from sim_results_qh r "
        "join dim_time t using (idx) where r.run_id = (select run_id from sim_runs "
        "where status = 'ok' order by finished_at desc limit 1) and r.household_id = ? "
        "and r.variant_id = ? and r.method = ? group by 1 order by (t.month + 2) % 12",
        (household, variant, method),
    )


@st.cache_data
def load_herding(stamp: float, first_idx: int) -> tuple[list[dict], list[dict]]:
    curves = rows(
        "select h.scenario, h.idx, t.ts_local, h.kw "
        "from herding_curve h join dim_time t using (idx) "
        "where h.idx >= ? and h.idx < ? order by 1, 2",
        (first_idx, first_idx + QH_PER_WEEK),
    )
    return curves, rows("select * from herding_summary order by peak_kw")


@st.cache_data
def week_label(stamp: float, first_idx: int) -> str:
    r = rows(
        "select min(ts_local)::date as a, max(ts_local)::date as b from dim_time "
        "where idx >= ? and idx < ?",
        (first_idx, first_idx + QH_PER_WEEK),
    )[0]
    return f"{r['a']} to {r['b']}"


@st.cache_resource
def get_context(path: str, stamp: float):
    """Gold context for the custom household (no PV shape: that needs the silver files)."""
    return load_gold_context(get_con(path).cursor(), opsd_parquet=None)


# ------------------------------------------------------------------ page


if not DB_PATH.exists():
    st.error(f"{DB_PATH} not found. Run `ets ingest`, `ets transform`, `ets simulate` first.")
    st.stop()
try:
    stamp = _stamp()
    summary = load_summary(stamp)
    run = load_run(stamp)
except duckdb.Error as e:
    st.error(f"The result tables are missing ({e}). Run `ets simulate` first.")
    st.stop()
if not summary:
    st.error("No successful simulation run found. Run `ets simulate` first.")
    st.stop()

st.title("Dynamic tariff simulator")
st.caption(
    "Annual electricity bill for four German household types under a fixed and a dynamic "
    "(quarter-hour) tariff and the §14a grid-fee modules, 1 Oct 2025 to 30 Sep 2026. "
    "A model on documented assumptions, not tariff advice."
)

households = sorted({r["household_id"] for r in summary})
st.sidebar.header("Choose")
household = st.sidebar.selectbox("Household", households)
mine = [r for r in summary if r["household_id"] == household]
variants_here = [v for v in VARIANT_LABEL if any(r["variant_id"] == v for r in mine)]
methods_here = [m for m in METHOD_LABEL if any(r["method"] == m for r in mine)]
shown = st.sidebar.multiselect(
    "Tariff variants to compare",
    variants_here,
    default=[v for v in ("fixed", "dynamic", "dynamic_m1_m3") if v in variants_here],
    format_func=VARIANT_LABEL.get,
)
method = st.sidebar.radio(
    "Load shifting", methods_here, index=len(methods_here) - 1, format_func=METHOD_LABEL.get
)
dynamic_variants = [v for v in variants_here if v != "fixed"]
week_variant = st.sidebar.selectbox(
    "Variant in the week and month charts", dynamic_variants, format_func=VARIANT_LABEL.get
)
n_weeks = 52
week = st.sidebar.slider("Week (1 = starting 1 Oct 2025)", 1, n_weeks, 12)
first_idx = (week - 1) * QH_PER_WEEK
st.sidebar.caption(f"Week {week}: {week_label(stamp, first_idx)}")

by_key = {(r["household_id"], r["variant_id"], r["method"]): r for r in summary}
fixed_row = by_key[(household, "fixed", "none")]

# ---- key figures
chosen = week_variant if (household, week_variant, method) in by_key else dynamic_variants[0]
row = by_key[(household, chosen, method)]
save_pct = 100 * row["saving_vs_fixed_eur"] / fixed_row["annual_eur"]
cols = st.columns(5)
cols[0].metric(f"Annual bill, {VARIANT_LABEL[chosen]}", f"{row['annual_eur']:,.0f} €")
cols[1].metric(
    "Saving vs fixed tariff", f"{row['saving_vs_fixed_eur']:,.0f} €", f"{save_pct:.1f} %"
)
cols[2].metric("Energy shifted", f"{row['shifted_kwh']:,.0f} kWh")
cols[3].metric("Most expensive month", f"{row['worst_month_eur']:,.0f} €")
cols[4].metric("Highest-price week", f"{row['spike_week_eur']:,.0f} €")

left, right = st.columns(2)

# ---- 1. annual cost by variant
with left:
    st.subheader("Annual cost by tariff")
    fig = go.Figure()
    for m in dict.fromkeys(["none", method]):
        xs = [v for v in shown if (household, v, m) in by_key]
        fig.add_bar(
            name=METHOD_LABEL[m],
            x=[VARIANT_LABEL[v] for v in xs],
            y=[by_key[(household, v, m)]["annual_eur"] for v in xs],
            marker_color=COLORS[m],
        )
    fig.update_layout(
        barmode="group", yaxis_title="€ per year", margin={"t": 10}, legend_title=None
    )
    st.plotly_chart(fig, width="stretch")

# ---- 2. where the savings come from
with right:
    st.subheader("Where the saving comes from")
    xs = [v for v in shown if v != "fixed" and (household, v, method) in by_key]
    fig = go.Figure()
    for label, key in (
        ("Tariff switch", "saving_tariff_switch_eur"),
        ("Timing", "saving_timing_eur"),
        ("Grid fee module", "saving_gridfee_eur"),
    ):
        fig.add_bar(
            name=label,
            x=[VARIANT_LABEL[v] for v in xs],
            y=[by_key[(household, v, method)][key] for v in xs],
            marker_color=SPLIT_COLORS[label],
        )
    fig.update_layout(barmode="relative", yaxis_title="€ saved vs fixed", margin={"t": 10})
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "Tariff switch: fixed minus dynamic, unshifted. Timing: what shifting adds. "
        "Grid fee: the module on top (includes the extra gain from module-specific cheap windows)."
    )

# ---- 3. one week: device load before and after, with the price
st.subheader(f"One week of device load: {week_label(stamp, first_idx)}")
if "none" in methods_here and (household, week_variant, "none") in by_key:
    before = load_week(stamp, household, week_variant, "none", first_idx)
    after = load_week(stamp, household, week_variant, method, first_idx)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    x = [r["ts_local"] for r in before]
    fig.add_scatter(
        x=x, y=[r["device_kwh"] * 4 for r in before], name="unshifted", line_shape="hv",
        line={"color": COLORS["none"]},
    )  # fmt: skip
    if method != "none":
        fig.add_scatter(
            x=x, y=[r["device_kwh"] * 4 for r in after], name=METHOD_LABEL[method],
            line_shape="hv", line={"color": COLORS[method]},
        )  # fmt: skip
    fig.add_scatter(
        x=x, y=[r["price_ct_kwh"] for r in before], name="spot price (ct/kWh)", line_shape="hv",
        line={"color": "#e76f51", "width": 1}, secondary_y=True,
    )  # fmt: skip
    fig.update_yaxes(title_text="EV + heat pump (kW)", secondary_y=False)
    fig.update_yaxes(title_text="spot price (ct/kWh, net)", secondary_y=True)
    fig.update_layout(margin={"t": 10}, legend_orientation="h")
    st.plotly_chart(fig, width="stretch")
    if not any(r["device_kwh"] for r in before):
        st.info("This household has no EV or heat pump, so there is no flexible load to shift.")

left, right = st.columns(2)

# ---- 4. monthly cost
with left:
    st.subheader("Monthly bill")
    fig = go.Figure()
    months = ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"]
    for v in dict.fromkeys(["fixed", *shown]):
        m = "none" if v == "fixed" else method
        if (household, v, m) not in by_key:
            continue
        energy = load_monthly(stamp, household, v, m)
        fees = by_key[(household, v, m)]["annual_eur"] - sum(r["energy_eur"] for r in energy)
        fig.add_scatter(
            x=months[: len(energy)], y=[r["energy_eur"] + fees / 12 for r in energy],
            name=VARIANT_LABEL[v], mode="lines+markers",
        )  # fmt: skip
    fig.update_layout(yaxis_title="€ per month", margin={"t": 10})
    st.plotly_chart(fig, width="stretch")
    st.caption("Energy cost per month plus one twelfth of the fixed fees and module credit.")

# ---- 5. herding
with right:
    st.subheader("What if everybody follows the price?")
    try:
        curves, hsum = load_herding(stamp, first_idx)
    except duckdb.Error:
        curves, hsum = [], []
        st.info("Run `ets herding` to see the aggregated load of 1,000 households.")
    if curves:
        fig = go.Figure()
        names = {
            "unshifted": "unshifted",
            "greedy": "everybody shifts",
            "greedy_spread": "spread out",
        }
        colors = {
            "unshifted": COLORS["none"],
            "greedy": "#e76f51",
            "greedy_spread": COLORS["greedy"],
        }
        for scenario in ("unshifted", "greedy", "greedy_spread"):
            pts = [r for r in curves if r["scenario"] == scenario]
            fig.add_scatter(
                x=[r["ts_local"] for r in pts], y=[r["kw"] for r in pts], name=names[scenario],
                line={"color": colors[scenario]}, line_shape="hv",
            )  # fmt: skip
            top = max(pts, key=lambda r: r["kw"])
            fig.add_scatter(
                x=[top["ts_local"]], y=[top["kw"]], mode="markers", showlegend=False,
                marker={"color": colors[scenario], "size": 10, "symbol": "diamond"},
            )  # fmt: skip
        fig.update_layout(
            yaxis_title="aggregated load (kW)", margin={"t": 10}, legend_orientation="h"
        )
        st.plotly_chart(fig, width="stretch")
        peaks = {r["scenario"]: r for r in hsum}
        st.caption(
            "Whole year, 1,000 synthetic households: peak "
            + ", ".join(f"{names[s]} {peaks[s]['peak_kw']:,.0f} kW" for s in names if s in peaks)
            + ". Diamonds mark the peak within the chosen week."
        )

# ---- custom household (the only place the app computes)
with st.expander("Custom household (calculated when you press the button)"):
    st.write(
        "Change the size of the household and compare it with the four precomputed types. "
        "Uses the same prices, tariffs and shifting as above; PV is not offered here."
    )
    c1, c2, c3 = st.columns(3)
    annual_kwh = c1.slider("Household electricity (kWh/year)", 1500, 7000, 3500, 100)
    ev_km = c2.slider("EV driving (km/year, 0 = no EV)", 0, 40000, 0, 1000)
    heat_kwh = c3.slider("Heat demand (kWh/year, 0 = no heat pump)", 0, 30000, 0, 1000)
    if st.button("Calculate"):
        cfg = load_config()
        ev_t = next((h.ev for h in cfg.households if h.ev), None)
        hp_t = next((h.heat_pump for h in cfg.households if h.heat_pump), None)
        custom = HouseholdCfg(
            id="custom",
            annual_kwh=annual_kwh,
            smart_meter_fee_eur_year=50.0 if (ev_km or heat_kwh) else 30.0,
            ev=ev_t.model_copy(update={"km_per_year": ev_km}) if ev_km and ev_t else None,
            heat_pump=hp_t.model_copy(update={"annual_heat_kwh": heat_kwh})
            if heat_kwh and hp_t
            else None,
        )
        cfg = cfg.model_copy(update={"households": [*cfg.households, custom]})
        ctx = get_context(str(DB_PATH), stamp)
        hh = build_household(custom, ctx)
        variants = {v.id: v for v in build_variants(cfg)}
        methods = list(dict.fromkeys(["none", method]))
        wanted = [("fixed", "none")] + [(v, m) for v in shown if v != "fixed" for m in methods]
        out = []
        with st.spinner("Simulating a year of quarter-hours..."):
            cache: dict = {}
            for vid, m in wanted:
                if vid not in variants or (
                    vid != "fixed" and not has_device(hh) and variants[vid].modules
                ):
                    continue
                if m != "none" and not has_device(hh):
                    continue
                res = simulate_one(hh, variants[vid], m, ctx, cfg, cache)
                out.append((vid, m, res.annual_eur))
        fixed_cost = next(c for v, _, c in out if v == "fixed")
        table = [
            {
                "household": "custom",
                "variant": VARIANT_LABEL[v],
                "method": METHOD_LABEL[m],
                "€ per year": round(c),
                "saving vs fixed": f"{100 * (fixed_cost - c) / fixed_cost:.1f} %",
            }
            for v, m, c in out
        ]
        for r in summary:  # the four precomputed households, same variants and methods
            if r["variant_id"] in shown and r["method"] in ("none", method):
                own_fixed = by_key[(r["household_id"], "fixed", "none")]["annual_eur"]
                table.append(
                    {
                        "household": r["household_id"],
                        "variant": VARIANT_LABEL[r["variant_id"]],
                        "method": METHOD_LABEL[r["method"]],
                        "€ per year": round(r["annual_eur"]),
                        "saving vs fixed": f"{100 * r['saving_vs_fixed_eur'] / own_fixed:.1f} %",
                    }
                )
        st.dataframe(table, width="stretch", hide_index=True)

# ---- provenance
st.divider()
st.caption(
    f"Results of run {run.get('run_id', '?')[:8]} (code {run.get('git_sha', '?')}, config "
    f"{str(run.get('config_hash', '?'))[:8]}, finished {run.get('finished_at')}). Sources: SMARD "
    "day-ahead prices (Bundesnetzagentur, CC BY 4.0), Open Power System Data (CC BY 4.0), BDEW "
    "standard load profiles, GELSENWASSER Energienetze price sheet 2026. Assumptions: DECISIONS.md."
)
