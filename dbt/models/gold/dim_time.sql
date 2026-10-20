{#- One row per quarter-hour of the simulation year. Generated in UTC, which has no gaps or
    repeats, so DST days automatically get 92 or 100 local quarter-hours. idx (0..N-1) is the
    array position used by all Python code. -#}
with spine as (
    select unnest(generate_series(
        timestamp '{{ var("sim_start_utc") }}',
        timestamp '{{ var("sim_end_utc") }}' - interval 15 minute,
        interval 15 minute)) as ts_utc
),

local_time as (
    select ts_utc, (ts_utc at time zone 'UTC') at time zone 'Europe/Berlin' as ts_local
    from spine
),

base as (
    select
        ts_utc,
        ts_local,
        ts_local::date as date_local,
        -- local clock quarter-hour 0..95; the repeated October hour maps to 8..11 twice
        hour(ts_local) * 4 + minute(ts_local) // 15 as qh_of_day,
        isodow(ts_local) as weekday,          -- 1 = Monday .. 7 = Sunday
        month(ts_local) as month
    from local_time
)

select
    (row_number() over (order by b.ts_utc) - 1)::integer as idx,
    b.ts_utc,
    b.ts_local,
    b.date_local,
    b.qh_of_day::integer as qh_of_day,
    b.weekday::integer as weekday,
    (h.date is not null) as is_holiday,
    (count(*) over (partition by b.date_local) <> 96) as is_dst_day,
    b.month::integer as month,
    case
        when b.month in (12, 1, 2) then 'winter'
        when b.month in (3, 4, 5) then 'spring'
        when b.month in (6, 7, 8) then 'summer'
        else 'autumn'
    end as season
from base as b
left join {{ ref('holidays_de') }} as h on h.date = b.date_local
order by b.ts_utc
