# samples-downloader

Downloads tracks from a Spotify playlist as `.m4a` files, named
`Artist - Title` so they drop straight into a DAW or a samples folder. Skips
anything already on disk, so `make pull` is safe to re-run.

## Setup

One-time, per machine:

```sh
uv venv --python 3.13
uv pip install pytubefix requests python-dotenv
cp .env.example .env
```

Then fill in these four values in `.env`:

| Variable | Where to get it |
| --- | --- |
| `SPOTIFY_CLIENT_ID` | Developer dashboard → your app → Settings |
| `SPOTIFY_CLIENT_SECRET` | Same page |
| `SPOTIFY_PLAYLIST_ID` | Last path segment of a playlist URL: `open.spotify.com/playlist/<THIS>` |
| `SAMPLES_DIR` | Wherever you want the files, e.g. `~/Music/samples` |

All four are required and none has a built-in default, so a fresh clone will
tell you exactly what's missing instead of guessing at a playlist or a
directory. `SAMPLES_DIR` is created if it doesn't exist. `.env` is gitignored
and never read until you create it.

If the playlist is private, add your Spotify user to the app's allowlist in
dashboard → Settings → User Management. The app uses the
`client_credentials` grant, which can only read public or explicitly-shared
playlists. A 401 or 403 on the playlist request is almost always this.

## Usage

Pull every track in the playlist that isn't already downloaded:

```sh
make pull
```

Check what a run would do without writing anything:

```sh
make dry-run           # first 3 missing tracks
make dry-run N=20      # first 20
```

Download a few for real, to prove the pipeline works end to end:

```sh
make test-pull         # first 3 missing tracks
```

Download a single YouTube video, by URL:

```sh
make one URL="https://www.youtube.com/watch?v=dQw4w9WgXcQ"
make one URL="https://www.youtube.com/watch?v=dQw4w9WgXcQ" NAME="Artist - Title"
```

Or call the script directly:

```sh
.venv/bin/python sample_downloader.py                                  # whole playlist
.venv/bin/python sample_downloader.py url "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
.venv/bin/python sample_downloader.py url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --name "Artist - Title"
.venv/bin/python sample_downloader.py url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --dir /tmp/scratch
```

The `url` form never contacts Spotify, so no credentials are needed. It does
still need somewhere to write — `--dir`, or `SAMPLES_DIR` in `.env`.

## Development

Run the tests and the linter. Neither needs network access or credentials:

```sh
make test         # unit tests, no I/O
make lint         # report style and likely-bug findings
make format       # autofix, then format in place
make check        # lint + tests, what to run before committing
```

Tests live in `test_sample_downloader.py` and cover the pure helpers —
filename generation, the dedup index, URL validation, and the matching
heuristic. They run in well under a second and are safe to run at any time.

Linting and formatting use [ruff](https://docs.astral.sh/ruff/), configured in
`ruff.toml`. It runs through `uvx`, so it is not a dependency of the app and
does not appear in `uv pip list`.

Ruff is also wired into the editor: with the ruff LSP installed, diagnostics and
format-on-save show up inline. Without it, `make format` covers the same ground
from the command line.

Two things that are deliberately not linted:

- **Line length** (`E501` is off). A few long comment and assertion lines read
  better unwrapped, and the formatter handles anything it can wrap.
- **Type correctness.** Ruff checks annotations, not whether they're accurate.
  Several functions are unannotated, so mypy would flag more than it could
  currently confirm.

`make test-pull` is separate from `make test` on purpose: it does real network
calls and writes real files, so it belongs to manual verification rather than
the test suite.

## Configuration

Everything is an environment variable, read from the real environment first and
from `.env` second. An exported variable always wins, so you can override one
value for a single run:

```sh
DRY_RUN=1 SAMPLES_DIR=/tmp/scratch make pull
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `SPOTIFY_CLIENT_ID` | — | Required for playlist pulls |
| `SPOTIFY_CLIENT_SECRET` | — | Required for playlist pulls |
| `SPOTIFY_PLAYLIST_ID` | — | Required for playlist pulls |
| `SAMPLES_DIR` | — | Required. Where files land |
| `DURATION_TOLERANCE` | `0.15` | How far a YouTube result's length may differ from Spotify's, as a fraction. Lower is stricter |
| `ADDED_AFTER` | — | ISO timestamp; only consider tracks added after it |
| `LIMIT` | — | Stop after N download attempts |
| `DRY_RUN` | — | `1` resolves and reports, downloads nothing |

## How tracks get matched

For each track, the app searches YouTube for `"artist title"` and scores every
result on two things:

- **Duration**, as a hard gate. A result more than `DURATION_TOLERANCE` off is
  discarded, not ranked lower. This is what stops a 10-minute mix or a
  30-second preview from being downloaded for a track called "Intro".
- **Title similarity**, to pick between survivors. A 102-second official audio
  upload and a 104-second alternate are both plausible on length alone; the
  title decides.

If nothing lands within tolerance, the closest result is still downloaded but
logged as `No result within N%... Review this one`. Those are the rows to
listen to before you trust a run. Anything scoring below ~0.85 deserves a
listen too.

Filenames are `Artist - Title.m4a`, sanitized for the filesystem. That's also
the dedup key, so a track downloads exactly once.

## Behaviour worth knowing

- **`make pull` exits non-zero if any track fails.** A `make: Error 1` at the
  end usually means a few unavailable YouTube videos, not a broken run. Read
  the summary line for the real counts.
- **Skips match loosely.** Existing files are matched after stripping
  parentheticals, so `Artist - Title (Official Video).m4a` is recognised as the
  same track as `Artist - Title`. The single-URL form only matches exactly, so
  pass `--name` matching your existing files to avoid a duplicate.
- **A failed download leaves nothing behind.** Partial files are written to a
  `.part.m4a` sidecar and renamed into place only on success, so a truncated
  file can never be mistaken for a completed one on the next run.
- **Pagination is followed.** Spotify caps pages at 100 tracks. The app follows
  the `next` URL, so playlists over 100 aren't silently truncated.
- **Non-tracks are skipped.** Podcast episodes and local files in the playlist
  are logged and passed over.
- **A first run can be a long one.** The app has no way to know how much of a
  playlist you already hold elsewhere, so it will try every track. A 100-track
  playlist takes roughly 15–25 minutes. Start with `make test-pull` or
  `make dry-run` to see the scope before committing to it.
