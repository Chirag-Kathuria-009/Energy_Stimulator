-- A year of day-ahead prices without a single negative quarter-hour means the data is wrong.
select 'no negative price in the simulation year' as problem
where (select count(*) from {{ ref('fact_price_qh') }} where price_ct_kwh < 0) = 0
