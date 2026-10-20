{#- BDEW dynamisation factor for household profiles H25/P25 (DECISIONS.md #5):
      F(t) = -3.92e-10 t^4 + 3.20e-7 t^3 - 7.02e-5 t^2 + 2.10e-3 t + 1.24,  t = day of year.
    BDEW recommends rounding the factor to 4 decimals. -#}
{% macro bdew_dynamisation(day_of_year) -%}
round(
    -3.92e-10 * pow({{ day_of_year }}, 4) + 3.20e-7 * pow({{ day_of_year }}, 3)
    - 7.02e-5 * pow({{ day_of_year }}, 2) + 2.10e-3 * {{ day_of_year }} + 1.24,
    4
)
{%- endmacro %}
