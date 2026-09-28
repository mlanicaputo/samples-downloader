PY := .venv/bin/python

# Pure-logic tests. No network, no credentials, safe to run any time.
test:
	$(PY) -m unittest test_sample_downloader -v

# Lint and format. `lint` reports, `format` rewrites in place.
# uvx runs ruff without adding it to the venv.
lint:
	uvx ruff check .

format:
	uvx ruff check . --fix
	uvx ruff format .

# Everything CI should run.
check: lint test

pull:
	$(PY) sample_downloader.py

# Resolve a few tracks and report them, without touching the samples
# directory. Still hits YouTube, so it takes a few seconds per track.
# Widen with `make dry-run N=20`. For every track, use `DRY_RUN=1 make pull`.
dry-run:
	DRY_RUN=1 LIMIT=$(or $(N),3) $(PY) sample_downloader.py

# Download the first few tracks for real, to prove the pipeline end to end.
test-pull:
	LIMIT=$(or $(N),3) $(PY) sample_downloader.py

# Download one YouTube video. `make one URL=...`, optional NAME=..., DIR=...
one:
	$(PY) sample_downloader.py url "$(URL)" $(if $(NAME),--name "$(NAME)",) $(if $(DIR),--dir "$(DIR)",)
