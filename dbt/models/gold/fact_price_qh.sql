select
    t.idx,
    t.ts_utc,
    p.price_ct_kwh,
    p.price_eur_mwh,
    p.source_resolution,
    p.is_estimated
from {{ ref('dim_time') }} as t
left join {{ ref('silver_price_qh') }} as p using (ts_utc)
order by t.idx
