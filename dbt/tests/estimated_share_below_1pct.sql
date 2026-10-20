select share_estimated
from (select avg(is_estimated::integer) as share_estimated from {{ ref('fact_price_qh') }})
where share_estimated >= 0.01
