{#- Expand an hourly relation to quarter-hours: each hour becomes 4 rows.
    repeat_cols are copied (prices, COP); split_cols are divided by 4 (energy per hour -> per
    quarter-hour). ts_col is a naive UTC timestamp on the hour. -#}
{% macro expand_hourly_to_qh(relation, ts_col='ts_utc', repeat_cols=[], split_cols=[]) -%}
select
    h.{{ ts_col }} + to_minutes(q.minute_offset) as {{ ts_col }}
    {%- for c in repeat_cols %}, h.{{ c }} as {{ c }}{% endfor %}
    {%- for c in split_cols %}, h.{{ c }} / 4.0 as {{ c }}{% endfor %}
from {{ relation }} as h
cross join (select unnest([0, 15, 30, 45]) as minute_offset) as q
{%- endmacro %}
