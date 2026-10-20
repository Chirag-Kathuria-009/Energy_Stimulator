{#- Heat demand shape and COP for the simulation year, taken from one when2heat weather season
    (DECISIONS.md #19): simulation Oct-Dec maps to Oct-Dec of the season start year, Jan-Sep to the
    next year, by local clock time. The shape is re-normalised over the simulation year so that
    each heat series sums to exactly 1. -#}
{%- set y0 = var('when2heat_season_start_year') %}
with mapped as (
    select
        idx, ts_utc,
        ((ts_local + to_years(
            (case when month(ts_local) >= 10 then {{ y0 }} else {{ y0 + 1 }} end) - year(ts_local)))
            at time zone 'Europe/Berlin') at time zone 'UTC' as src_ts_utc
    from {{ ref('dim_time') }}
),

joined as (
    select m.idx, m.ts_utc, h.heat_space_norm, h.heat_water_norm, h.cop_space, h.cop_water
    from mapped as m
    left join {{ ref('silver_heat_qh') }} as h on h.ts_utc = m.src_ts_utc
)

select
    idx, ts_utc,
    heat_space_norm / sum(heat_space_norm) over () as heat_space_norm,
    heat_water_norm / sum(heat_water_norm) over () as heat_water_norm,
    cop_space, cop_water
from joined
order by idx
