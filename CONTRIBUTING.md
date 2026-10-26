# Contributing

Start with the [development setup](docs/contributing/development.md). Install Python 3.12+ and uv, run `uv sync`, then `uv run pytest server/tests -q`, `uv run ruff check server`, `uv run ruff format --check server`, `uv lock --check` and `uv run mkdocs build --strict`. Image tests need a local Docker build (see the development guide).

Only synthetic or independently anonymized fixtures belong in the repository. [Fixture provenance](protocol-fixtures/README.md) separates dated-note transcriptions from constructed test vectors. Never submit raw logs, pairing traffic, Wi-Fi passwords, tokens, setup keys, private APKs, serials or real device identifiers. Manually redact diagnostics before attaching them to issues.

To [add a hood model](docs/contributing/adding-a-hood-model.md), report reviewed compatibility observations, map only understood capabilities and add synthetic profile tests. Unknown capabilities remain read-only until verified.
