setup:
	uv sync

ingest:
	uv run ets ingest

transform:
	uv run ets seed-holidays
	uv run ets transform

simulate:
	uv run ets simulate
	uv run ets herding

app:
	uv run streamlit run app/streamlit_app.py

test:
	uv run pytest -q
	uv run ruff check .

all: ingest transform simulate

.PHONY: setup ingest transform simulate app test all
