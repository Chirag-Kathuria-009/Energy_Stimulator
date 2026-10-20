{#- Every local date has 92 (spring DST), 96 or 100 (autumn DST) quarter-hours. -#}
{% test qh_per_local_day(model, column_name) %}
select {{ column_name }} as date_local, count(*) as n_qh
from {{ model }}
group by 1
having count(*) not in (92, 96, 100)
{% endtest %}
