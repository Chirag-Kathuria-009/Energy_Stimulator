-- Load profiles sum to 1000 kWh per 1000 kWh/year; heat shapes sum to 1.
select 'load:' || profile_id as series, sum(kwh) as total
from {{ ref('fact_load_profile_qh') }} group by profile_id having abs(sum(kwh) - 1000) > 1e-6
union all
select 'heat_space', sum(heat_space_norm) from {{ ref('fact_heat_qh') }} having abs(sum(heat_space_norm) - 1) > 1e-9
union all
select 'heat_water', sum(heat_water_norm) from {{ ref('fact_heat_qh') }} having abs(sum(heat_water_norm) - 1) > 1e-9
