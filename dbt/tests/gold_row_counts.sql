-- Every gold fact has exactly N rows (the load profile has N per profile).
select 'dim_time' as tbl, count(*) as n from {{ ref('dim_time') }} having count(*) <> {{ var('n_qh') }}
union all
select 'fact_price_qh', count(*) from {{ ref('fact_price_qh') }} having count(*) <> {{ var('n_qh') }}
union all
select 'fact_heat_qh', count(*) from {{ ref('fact_heat_qh') }} having count(*) <> {{ var('n_qh') }}
union all
select 'fact_load_profile_qh:' || profile_id, count(*)
from {{ ref('fact_load_profile_qh') }}
group by profile_id
having count(*) <> {{ var('n_qh') }}
