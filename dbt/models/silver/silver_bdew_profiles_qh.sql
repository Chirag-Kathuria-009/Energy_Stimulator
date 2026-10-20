{#- BDEW H25/P25 as a month x day type x quarter-hour table, kWh per quarter-hour for a household
    consuming 1,000,000 kWh/year (DECISIONS.md #5). Not yet dynamised: that depends on the date and
    is applied in fact_load_profile_qh. Staged from the workbook by ets.profiles.bdew_staging. -#}
select
    profile_id::varchar as profile_id,
    month::integer as month,
    daytype::varchar as daytype,
    qh_of_day::integer as qh_of_day,
    kwh_per_million::double as kwh_per_million
from {{ staging_bdew_long() }}
