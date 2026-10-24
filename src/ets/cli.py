"""Typer CLI. Phase 0: only `check-config` works; the rest fail loudly until their phase."""

import typer
from pydantic import ValidationError

from ets.config import config_hash, load_config
from ets.settings import N_QH, SIM_END, SIM_START

app = typer.Typer(no_args_is_help=True, help="Dynamic tariff simulator (ets).")


@app.command("check-config")
def check_config() -> None:
    """Load and validate config/*.yaml; exit 1 on any error."""
    try:
        cfg = load_config()
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e
    typer.echo("Config OK")
    typer.echo(f"  simulation year : {SIM_START} .. {SIM_END} ({N_QH} quarter-hours)")
    typer.echo(f"  grid operator   : {cfg.grid_14a.grid_operator}")
    typer.echo(f"  supplier        : {cfg.tariffs.supplier.name}")
    typer.echo(f"  tariff variants : {', '.join(v.id for v in cfg.tariffs.variants)}")
    typer.echo(f"  households      : {', '.join(h.id for h in cfg.households)}")
    typer.echo(f"  config hash     : {config_hash(cfg)[:16]}")


@app.command()
def ingest() -> None:
    """Download all sources into data/bronze; exit 1 if any source or coverage check fails."""
    from ets.ingest import bdew, manifest, opsd, smard, when2heat

    try:
        cfg = load_config()
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e

    failures: list[str] = []

    def step(name: str, fn):
        typer.echo(f"[{name}] ...")
        try:
            result = fn()
        except Exception as e:  # one failing source must not stop the others
            failures.append(f"{name}: {e}")
            typer.echo(f"[{name}] FAILED: {e}", err=True)
            return
        typer.echo(f"[{name}] ok")
        return result

    prices = step("smard", lambda: smard.ingest_smard(cfg))
    if prices:
        typer.echo(f"  {len(prices[0])} price chunks")
        for w in prices[1]:
            typer.echo(f"  warning: {w}", err=True)
    step("opsd", lambda: opsd.download_household_data(cfg))
    step("when2heat", lambda: when2heat.download_when2heat(cfg))
    step("bdew", lambda: bdew.register_bdew_file(cfg))

    for problem in manifest.check_coverage(cfg):
        failures.append(f"coverage: {problem}")
        typer.echo(f"[coverage] {problem}", err=True)

    if failures:
        typer.echo(f"\nIngest failed ({len(failures)} problem(s)).", err=True)
        raise typer.Exit(1)
    typer.echo("\nIngest OK. Manifest: data/bronze/_manifest.json")


@app.command("seed-holidays")
def seed_holidays() -> None:
    """Write dbt/seeds/holidays_de.csv with national German holidays of the simulation years."""
    from ets.profiles.calendar import write_holiday_seed

    path = write_holiday_seed(SIM_START.year, SIM_END.year)
    typer.echo(f"Wrote {path}")


@app.command()
def transform() -> None:
    """Stage BDEW, run dbt build (seed, models, tests), then gold checks; exit 1 on any failure."""
    import subprocess

    from ets.transform import transform as run_transform

    try:
        cfg = load_config()
        problems = run_transform(cfg)
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e
    except subprocess.CalledProcessError as e:
        typer.echo(f"dbt build failed (exit {e.returncode}); see output above.", err=True)
        raise typer.Exit(1) from e
    if problems:
        for p in problems:
            typer.echo(f"[gold check] {p}", err=True)
        raise typer.Exit(1)
    typer.echo("Transform OK: dbt build green, gold checks passed.")


@app.command()
def households() -> None:
    """Build the household archetypes from the gold tables and write dim_household (Phase 3)."""
    import duckdb

    from ets.profiles.archetypes import household_totals
    from ets.settings import GOLD_DB
    from ets.simulate.context import load_gold_context
    from ets.simulate.run import build_households, write_dim_household

    try:
        cfg = load_config()
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e
    if not GOLD_DB.exists():
        typer.echo(f"{GOLD_DB} missing: run `ets transform` first.", err=True)
        raise typer.Exit(1)
    con = duckdb.connect(str(GOLD_DB))
    try:
        ctx = load_gold_context(con)
        built = build_households(cfg, ctx)
        write_dim_household(con, cfg, built)
    except (ValueError, duckdb.Error) as e:
        typer.echo(f"Building households failed: {e}", err=True)
        raise typer.Exit(1) from e
    finally:
        con.close()
    for hh in built:
        t = household_totals(hh)
        typer.echo(
            f"{hh.id:12} base {t['base_kwh']:7.0f}  ev {t['ev_kwh']:6.0f}  "
            f"hp {t['hp_el_kwh']:6.0f} "
            f"(heat {t['hp_heat_kwh']:6.0f}, COP {t['hp_seasonal_cop']:.2f})  "
            f"pv {t['pv_kwh']:6.0f} kWh/yr"
        )
    typer.echo("dim_household written.")


@app.command()
def baseline() -> None:
    """Bill the unshifted households under every tariff and run the type-A gate (Phase 4)."""
    import duckdb

    from ets.settings import GOLD_DB
    from ets.simulate.context import load_gold_context
    from ets.simulate.run import build_households, run_baseline, saving_vs_fixed_pct

    gate_low, gate_high = -2.0, 5.0  # type A dynamic saving must lie in this window (percent)
    try:
        cfg = load_config()
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e
    if not GOLD_DB.exists():
        typer.echo(f"{GOLD_DB} missing: run `ets transform` first.", err=True)
        raise typer.Exit(1)
    con = duckdb.connect(str(GOLD_DB), read_only=True)
    try:
        ctx = load_gold_context(con)
    finally:
        con.close()
    results = run_baseline(cfg, ctx, build_households(cfg, ctx))
    typer.echo(f"{'household':12} {'variant':14} {'EUR/year':>9} {'vs fixed':>9}")
    for r in results:
        pct = saving_vs_fixed_pct(results, r.household_id, r.variant_id)
        typer.echo(f"{r.household_id:12} {r.variant_id:14} {r.annual_eur:9.2f} {pct:8.2f}%")
    saving_a = saving_vs_fixed_pct(results, "A_standard", "dynamic")
    ok = gate_low <= saving_a <= gate_high
    typer.echo(
        f"\nType A (A_standard) dynamic vs fixed: {saving_a:.2f}% "
        f"(gate {gate_low}% to {gate_high}%, literature about 1-3%): {'PASS' if ok else 'FAIL'}"
    )
    if not ok:
        raise typer.Exit(1)


@app.command()
def simulate() -> None:
    """Build households, bill every tariff, shift the load (greedy, LP) and store the results."""
    import duckdb

    from ets.settings import GOLD_DB
    from ets.simulate.run import run_scenarios

    try:
        cfg = load_config()
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e
    if not GOLD_DB.exists():
        typer.echo(f"{GOLD_DB} missing: run `ets transform` first.", err=True)
        raise typer.Exit(1)
    con = duckdb.connect(str(GOLD_DB))
    try:
        run_id = run_scenarios(cfg, con)
        rows = con.sql(
            "select s.household_id, s.variant_id, s.method, s.annual_eur, s.saving_vs_fixed_eur, "
            "100 * s.saving_vs_fixed_eur / f.annual_eur as pct, s.saving_tariff_switch_eur, "
            "s.saving_timing_eur, s.saving_gridfee_eur, s.shifted_kwh "
            "from sim_summary s join sim_summary f on f.run_id = s.run_id and f.variant_id = "
            "'fixed' and f.household_id = s.household_id and f.method = 'none' "
            "where s.run_id = ? order by 1, 2, 3",
            params=[run_id],
        ).fetchall()
    except (ValueError, duckdb.Error) as e:
        typer.echo(f"Simulation failed: {e}", err=True)
        raise typer.Exit(1) from e
    finally:
        con.close()
    typer.echo(
        f"{'household':12} {'variant':14} {'method':7} {'EUR/yr':>8} {'saving':>8} {'%':>6} "
        f"{'switch':>7} {'timing':>7} {'gridfee':>8} {'shifted kWh':>11}"
    )
    for hid, vid, m, eur, sav, pct, sw, tm, gf, sh in rows:
        typer.echo(
            f"{hid:12} {vid:14} {m:7} {eur:8.2f} {sav:8.2f} {pct:6.2f} {sw:7.2f} {tm:7.2f} "
            f"{gf:8.2f} {sh:11.0f}"
        )
    pct = {(h, v, m): p for h, v, m, _, _, p, *_ in rows}
    a = pct[("A_standard", "dynamic", "none")]
    b, c = pct[("B_ev", "dynamic", "lp")], pct[("C_heat_pump", "dynamic", "lp")]
    ok_a, ok_bc = -2.0 <= a <= 5.0, b > a and c > a
    typer.echo(
        f"\nGate: type A dynamic {a:.2f}% ({'ok' if ok_a else 'OUT OF RANGE'}); "
        f"B dynamic+LP {b:.2f}% and C {c:.2f}% must exceed A: {'PASS' if ok_bc else 'FAIL'}"
    )
    typer.echo(f"run_id {run_id}")
    if not (ok_a and ok_bc):
        raise typer.Exit(1)


@app.command()
def herding() -> None:
    """Aggregate a synthetic population before/after shifting and store the peak analysis."""
    import duckdb

    from ets.settings import GOLD_DB
    from ets.simulate.herding import run_herding

    try:
        cfg = load_config()
    except (ValidationError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Config invalid:\n{e}", err=True)
        raise typer.Exit(1) from e
    if not GOLD_DB.exists():
        typer.echo(f"{GOLD_DB} missing: run `ets transform` first.", err=True)
        raise typer.Exit(1)
    con = duckdb.connect(str(GOLD_DB))
    try:
        summary = run_herding(cfg, con)
    except (ValueError, duckdb.Error) as e:
        typer.echo(f"Herding simulation failed: {e}", err=True)
        raise typer.Exit(1) from e
    finally:
        con.close()
    base = summary.filter(summary["scenario"] == "unshifted")["peak_kw"][0]
    typer.echo(f"{cfg.herding.n_households} households, seed {cfg.herding.seed}")
    typer.echo(
        f"{'scenario':14} {'peak kW':>9} {'vs unshifted':>12} {'peak at (local)':>17} "
        f"{'peak/mean':>9} {'nights peak in cheapest qh':>27} {'MWh':>7}"
    )
    for r in summary.iter_rows(named=True):
        typer.echo(
            f"{r['scenario']:14} {r['peak_kw']:9.0f} {100 * (r['peak_kw'] / base - 1):+11.0f}% "
            f"{r['peak_ts_local']:>17} {r['peak_to_mean']:9.2f} "
            f"{100 * r['share_nights_peak_in_cheapest_qh']:26.0f}% {r['total_mwh']:7.0f}"
        )


@app.command("app")
def app_() -> None:
    """Start the Streamlit app (reads data/gold/energy.duckdb; run `ets simulate` first)."""
    import subprocess
    import sys

    from ets.settings import ROOT

    script = ROOT / "app" / "streamlit_app.py"
    raise typer.Exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(script)]))


@app.command()
def refresh() -> None:
    """Fetch the newest price chunks, rebuild the price models and check them (runs daily in CI)."""
    import subprocess

    from ets.refresh import SourceFormatError
    from ets.refresh import refresh as run_refresh

    try:
        cfg = load_config()
        chunks, problems = run_refresh(cfg)
    except (ValidationError, FileNotFoundError, ValueError) as e:
        kind = "Source format changed" if isinstance(e, SourceFormatError) else "Refresh failed"
        typer.echo(f"{kind}: {e}", err=True)
        raise typer.Exit(1) from e
    except subprocess.CalledProcessError as e:
        typer.echo(f"dbt build failed (exit {e.returncode}); see output above.", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"{len(chunks)} price chunks checked, price models rebuilt.")
    for p in problems:
        typer.echo(f"[gold check] {p}", err=True)
    if problems:
        raise typer.Exit(1)
    typer.echo("Refresh OK.")
