{#- Day-ahead price DE-LU per quarter-hour, UTC, ct/kWh.
    SMARD already returns 15-minute rows for the whole history; before 2025-10-01 each hour's four
    values are identical (DECISIONS.md #2, #16), so no union with the hourly series is needed and
    source_resolution is set by date. -#}
with points as (
    select
        epoch_ms(s[1]::bigint) as ts_utc,
        s[2] as price_eur_mwh,
        filename
    from (select unnest(series) as s, filename from {{ bronze_smard_price() }})
    where s[2] is not null          -- future slots in the newest chunk
),

dedup as (
    -- chunks do not overlap, but if they ever do keep the value from the later chunk file
    select ts_utc, price_eur_mwh
    from (
        select *, row_number() over (partition by ts_utc order by filename desc) as rn
        from points
    )
    where rn = 1
),

bounds as (
    select min(ts_utc) as first_ts, max(ts_utc) as last_ts from dedup
),

spine as (
    select unnest(generate_series(first_ts, last_ts, interval 15 minute)) as ts_utc
    from bounds
),

neighbours as (
    select
        sp.ts_utc,
        d.price_eur_mwh as observed,
        last_value(d.price_eur_mwh ignore nulls) over (
            order by sp.ts_utc rows between unbounded preceding and current row) as prev_price,
        last_value(case when d.price_eur_mwh is not null then sp.ts_utc end ignore nulls) over (
            order by sp.ts_utc rows between unbounded preceding and current row) as prev_ts,
        first_value(d.price_eur_mwh ignore nulls) over (
            order by sp.ts_utc rows between current row and unbounded following) as next_price,
        first_value(case when d.price_eur_mwh is not null then sp.ts_utc end ignore nulls) over (
            order by sp.ts_utc rows between current row and unbounded following) as next_ts
    from spine as sp
    left join dedup as d using (ts_utc)
),

filled as (
    select
        ts_utc,
        coalesce(
            observed,
            -- linear interpolation, only if at most 4 consecutive quarter-hours are missing
            case when date_diff('minute', prev_ts, next_ts) <= 75
                then prev_price + (next_price - prev_price)
                    * date_diff('minute', prev_ts, ts_utc) / date_diff('minute', prev_ts, next_ts)
            end
        ) as price_eur_mwh,
        observed is null as was_missing
    from neighbours
)

select
    ts_utc,
    price_eur_mwh,
    price_eur_mwh / 10.0 as price_ct_kwh,
    case when ts_utc < timestamp '{{ var("qh_start_utc") }}' then 'h' else 'qh' end
        as source_resolution,
    (was_missing and price_eur_mwh is not null) as is_estimated
from filled
order by ts_utc
