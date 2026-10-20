{#- when2heat DE single-family-house heat profiles and heat-pump COP, hourly -> quarter-hour.
    The source has one missing hour at each autumn DST change (15 holes, 2008-2022,
    DECISIONS.md #19); a single missing hour is filled with the mean of its neighbours and flagged
    is_estimated, longer gaps stay null so the not_null tests catch them.
    Heat profiles are normalised so each calendar (UTC) year of the source sums to 1; energy is
    divided by 4 per quarter-hour, COP is repeated (DECISIONS.md #4). -#}
{%- set c = var('when2heat_columns') %}
with observed as (
    select
        strptime(utc_timestamp, '%Y-%m-%dT%H:%M:%SZ') as ts_utc,
        "{{ c['heat_profile_space'] }}" as heat_space_raw,
        "{{ c['heat_profile_water'] }}" as heat_water_raw,
        "{{ c['cop_space'] }}" as cop_space,
        "{{ c['cop_water'] }}" as cop_water
    from {{ bronze_when2heat() }}
),

bounds as (
    select min(ts_utc) as first_ts, max(ts_utc) as last_ts from observed
),

spine as (
    select unnest(generate_series(first_ts, last_ts, interval 1 hour)) as ts_utc from bounds
),

joined as (
    select
        s.ts_utc,
        o.ts_utc is null as is_estimated,
        o.heat_space_raw, o.heat_water_raw, o.cop_space, o.cop_water
    from spine as s
    left join observed as o using (ts_utc)
),

filled as (
    select
        ts_utc, is_estimated,
        coalesce(heat_space_raw, (lag(heat_space_raw) over w + lead(heat_space_raw) over w) / 2)
            as heat_space_raw,
        coalesce(heat_water_raw, (lag(heat_water_raw) over w + lead(heat_water_raw) over w) / 2)
            as heat_water_raw,
        coalesce(cop_space, (lag(cop_space) over w + lead(cop_space) over w) / 2) as cop_space,
        coalesce(cop_water, (lag(cop_water) over w + lead(cop_water) over w) / 2) as cop_water
    from joined
    window w as (order by ts_utc)
),

normalised as (
    select
        ts_utc, is_estimated, cop_space, cop_water,
        heat_space_raw / sum(heat_space_raw) over (partition by year(ts_utc)) as heat_space_norm,
        heat_water_raw / sum(heat_water_raw) over (partition by year(ts_utc)) as heat_water_norm
    from filled
)

select *, 'h' as source_resolution
from (
    {{ expand_hourly_to_qh(
        'normalised', 'ts_utc',
        repeat_cols=['cop_space', 'cop_water', 'is_estimated'],
        split_cols=['heat_space_norm', 'heat_water_norm']) }}
)
order by ts_utc
