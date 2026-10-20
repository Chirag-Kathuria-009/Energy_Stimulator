{#- BDEW H25 and P25 on the simulation calendar, kWh per quarter-hour per 1,000 kWh of annual
    consumption (each profile sums to exactly 1000 over the year). Day type: Sunday or national
    holiday = FT; Saturday, 24 Dec and 31 Dec = SA; else WT. The October day repeats 02:00-02:59,
    so both copies get half the value to keep daily energy; the March day simply lacks that hour. -#}
with days as (
    select
        idx, ts_utc, date_local, qh_of_day, month,
        case
            when weekday = 7 or is_holiday then 'FT'
            when weekday = 6 or (month = 12 and day(date_local) in (24, 31)) then 'SA'
            else 'WT'
        end as daytype,
        dayofyear(date_local) as doy,
        count(*) over (partition by date_local, qh_of_day) as copies
    from {{ ref('dim_time') }}
),

dynamised as (
    select
        d.idx, d.ts_utc, p.profile_id,
        -- BDEW: factor rounded to 4 decimals, result rounded to 3 decimals
        round(p.kwh_per_million * {{ bdew_dynamisation('d.doy') }}, 3) / d.copies as raw_kwh
    from days as d
    join {{ ref('silver_bdew_profiles_qh') }} as p
        on p.month = d.month and p.daytype = d.daytype and p.qh_of_day = d.qh_of_day
)

select
    idx,
    profile_id,
    raw_kwh * 1000.0 / sum(raw_kwh) over (partition by profile_id) as kwh,
    false as is_estimated
from dynamised
order by profile_id, idx
