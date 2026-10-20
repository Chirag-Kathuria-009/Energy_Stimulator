{#- OPSD household meters -> kWh per quarter-hour (UTC), long format, complete local days only.
    Columns are cumulative kWh readings (DECISIONS.md #3). A gap of up to 4 missing quarter-hours
    between two readings is filled by spreading the energy evenly (is_estimated); readings that
    go backwards (meter reset) or exceed opsd_max_kwh_per_qh are dropped. Only local days with a
    full set of quarter-hours (92/96/100) are kept (DECISIONS.md #15). -#}
with raw as (
    select
        strptime(utc_timestamp, '%Y-%m-%dT%H:%M:%SZ') as ts_utc,
        *
    from {{ bronze_opsd() }}
),

readings as (
{% for key, col in var('opsd_channels').items() %}
{%- set parts = key.rsplit('_', 1) %}
    select ts_utc, '{{ parts[1] }}' as site, '{{ parts[0] }}' as channel, "{{ col }}" as cum_kwh
    from raw
    where "{{ col }}" is not null
{% if not loop.last %}    union all
{% endif %}
{%- endfor %}
),

steps as (
    select
        site, channel, ts_utc as end_ts, cum_kwh,
        lag(ts_utc) over w as prev_ts,
        cum_kwh - lag(cum_kwh) over w as delta_kwh
    from readings
    window w as (partition by site, channel order by ts_utc)
),

spans as (
    select
        site, channel, prev_ts, end_ts, delta_kwh,
        date_diff('minute', prev_ts, end_ts) / 15 as n_qh
    from steps
    where prev_ts is not null
),

expanded as (
    select
        site, channel,
        unnest(generate_series(prev_ts + interval 15 minute, end_ts, interval 15 minute)) as ts_utc,
        delta_kwh / n_qh as kwh,
        n_qh > 1 as is_estimated
    from spans
    where n_qh <= 5
      and delta_kwh >= 0
      and delta_kwh / n_qh <= {{ var('opsd_max_kwh_per_qh') }}
),

with_day as (
    select *, ((ts_utc at time zone 'UTC') at time zone 'Europe/Berlin')::date as date_local
    from expanded
),

day_counts as (
    select
        site, channel, date_local, count(*) as n_have,
        date_diff('minute',
            date_local::timestamp at time zone 'Europe/Berlin',
            (date_local + 1)::timestamp at time zone 'Europe/Berlin') / 15 as n_expected
    from with_day
    group by all
)

select w.ts_utc, w.site, w.channel, w.kwh, w.is_estimated, w.date_local
from with_day as w
join day_counts as c using (site, channel, date_local)
where c.n_have = c.n_expected
order by site, channel, ts_utc
