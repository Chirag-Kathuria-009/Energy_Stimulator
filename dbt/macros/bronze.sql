{#- Readers for the bronze files. Kept as macros so the SQL models stay short and the tests can
    point them at temporary files. -#}

{% macro bronze_smard_price() -%}
read_json(
    '{{ var("bronze_dir") }}/smard/{{ var("smard_price_filter") }}/{{ var("smard_resolution") }}/*.json',
    columns = {'series': 'DOUBLE[][]'},
    filename = true
)
{%- endmacro %}

{% macro bronze_opsd() -%}
read_csv(
    '{{ var("bronze_dir") }}/opsd/household_data_15min.csv',
    header = true,
    sample_size = -1,
    types = {'utc_timestamp': 'VARCHAR'
        {%- for key, col in var("opsd_channels").items() %}, '{{ col }}': 'DOUBLE'{% endfor -%}
    }
)
{%- endmacro %}

{% macro bronze_when2heat() -%}
{#- The file uses ';' as separator and a decimal comma (3,19). -#}
read_csv(
    '{{ var("bronze_dir") }}/when2heat/when2heat.csv',
    delim = ';',
    decimal_separator = ',',
    header = true,
    sample_size = -1,
    types = {'utc_timestamp': 'VARCHAR'
        {%- for key, col in var("when2heat_columns").items() %}, '{{ col }}': 'DOUBLE'{% endfor -%}
    }
)
{%- endmacro %}

{% macro staging_bdew_long() -%}
read_csv('{{ var("staging_dir") }}/bdew_profiles_long.csv', header = true)
{%- endmacro %}
