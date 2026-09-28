"""Download every track in a Spotify playlist as an m4a sample.

Run via `make pull`. Configuration is read from the environment, and a .env
file in the working directory (or any parent) is loaded automatically. Real
environment variables take precedence over .env, so you can override a single
value for one run without editing the file.

    SPOTIFY_CLIENT_ID      required
    SPOTIFY_CLIENT_SECRET  required
    SPOTIFY_PLAYLIST_ID    defaults to the TLC playlist
    SAMPLES_DIR            where m4a files land
    DURATION_TOLERANCE      how far a YouTube result may differ in length (0.15 = 15%)
    ADDED_AFTER            only consider tracks added to the playlist after this ISO date
    LIMIT                  stop after N downloads (testing)
    DRY_RUN                1 = resolve and report, download nothing

Override per run, e.g. `DRY_RUN=1 make pull`.

Usage:
    sample_downloader.py                    download the whole playlist
    sample_downloader.py url <URL>          download one YouTube video
    sample_downloader.py url <URL> --name "Artist - Title"

The single-url form never contacts Spotify, so it works without credentials.
"""

from pytubefix import YouTube, Search
from pytubefix.cli import on_progress

import argparse
import difflib
import logging
import os
import re
import sys
import unicodedata

import json
import base64

import requests as re_
from dotenv import load_dotenv
from typing import List, Optional


# Read .env before anything touches os.environ. Values already exported in the
# shell win, so an explicit export still overrides the file.
load_dotenv()


logging.basicConfig(
    level=logging.INFO,                      # Minimum level to log
    format="%(asctime)s - %(levelname)s - %(message)s"  # Format of each log line
)


PLAYLIST_ID = os.environ.get("SPOTIFY_PLAYLIST_ID", "1wmSX8uXxNxYhZUw5YR3CN")
SAMPLES_DIR = os.environ.get("SAMPLES_DIR", "/Users/milo/Music/Logic/samples")

# A YouTube result is only considered a match if its length is within this
# fraction of the Spotify duration. Guards against grabbing a 10 minute mix
# or a 30 second preview when a track is called "Intro".
DURATION_TOLERANCE = float(os.environ.get("DURATION_TOLERANCE", "0.15"))

# macOS caps a single path component at 255 bytes. Leave room for ".m4a".
MAX_FILENAME_STEM = 200

# Characters that are legal in a POSIX filename but hostile to a shell, Finder,
# Logic, or a DAW's file browser.
_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class PlaylistItem:
    """One track from the Spotify playlist."""

    _artists: list[str]
    _name: str
    _duration_ms: int
    _track_id: str
    _isrc: Optional[str]
    _added_at: Optional[str]

    def __init__(self, artists, name, duration_ms=0, track_id="", isrc=None, added_at=None):
        self._artists = artists
        self._name = name
        self._duration_ms = duration_ms
        self._track_id = track_id
        self._isrc = isrc
        self._added_at = added_at

    @property
    def artists(self):
        return self._artists

    @property
    def name(self):
        return self._name

    @property
    def duration_ms(self):
        return self._duration_ms

    @property
    def duration_s(self):
        return self._duration_ms / 1000

    @property
    def track_id(self):
        return self._track_id

    @property
    def isrc(self):
        return self._isrc

    @property
    def added_at(self):
        return self._added_at

    def __str__(self):
        return f"{self.name} {self.artists[0]}" if self.artists else self.name

    def __repr__(self):
        return f"<PlaylistItem {self.name!r} by {self.artists}>"

    def search_term(self) -> str:
        return " ".join([", ".join(self.artists), self._name]).strip(", ")

    def filename_stem(self) -> str:
        """A deterministic, filesystem-safe "Artist - Title" name.

        This is the dedup key. Two runs must produce byte-identical names for
        the same track, so this must not depend on the YouTube result.
        """
        artist = ", ".join(self.artists) if self.artists else "Unknown Artist"
        stem = f"{artist} - {self.name}"
        stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
        stem = _ILLEGAL.sub("", stem)
        stem = _WHITESPACE.sub(" ", stem).strip(" .-")
        return stem[:MAX_FILENAME_STEM].strip(" .-")

    def target_filename(self) -> str:
        return f"{self.filename_stem()}.m4a"


def authenticate():
    """Return a valid Spotify session token.

    client_credentials is an app-only grant, so it can only see playlists that
    are public (or explicitly shared with the app in dev mode). It cannot read
    a private playlist. If this starts returning 401/403, that is why.
    """
    missing = [name for name in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET")
               if not os.environ.get(name)]

    if missing:
        raise KeyError(
            f"{', '.join(missing)} not set. Export it, or add it to a .env file "
            f"in this directory. See .env.example."
        )

    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]

    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    url = "https://accounts.spotify.com/api/token"

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {encoded}"
    }

    data = {
        "grant_type": "client_credentials"
    }

    response = re_.post(
        url,
        headers=headers,
        data=data,
        timeout=30
    )
    response.raise_for_status()

    return response.json()["access_token"]


def objectize_item(item: dict) -> Optional[PlaylistItem]:
    """Turn a Spotify response dict into a PlaylistItem.

    Returns None for entries that are not playable tracks (podcast episodes,
    local files, removed tracks) rather than letting them raise later.
    """
    track = item.get("track")
    if not track:
        logging.warning("Skipping entry with no track (removed or unavailable).")
        return None

    if track.get("type") != "track":
        logging.warning("Skipping non-track entry of type %r: %s", track.get("type"), track.get("name"))
        return None

    if track.get("is_local"):
        logging.warning("Skipping local file (no ISRC): %s", track.get("name"))
        return None

    try:
        artists = [artist["name"] for artist in track["artists"]]
        name = track["name"]
    except (KeyError, IndexError, TypeError):
        logging.error("Failed to objectize item.")
        return None

    return PlaylistItem(
        artists,
        name,
        duration_ms=track.get("duration_ms") or 0,
        track_id=track.get("id") or "",
        isrc=(track.get("external_ids") or {}).get("isrc"),
        added_at=item.get("added_at"),
    )


def get_playlist_items(token: str, playlist_id: str) -> List[PlaylistItem]:
    """Return every track in the playlist, following pagination.

    Spotify caps a page at 100 items and returns a `next` URL. Reading only
    the first page silently truncates a longer playlist, so follow `next`.
    """
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=100"

    headers = {
        "content-type": "json",
        "Authorization": f"Bearer {token}"
    }

    items: List[PlaylistItem] = []
    pages = 0

    while url:
        response = re_.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()

        for raw in payload.get("items", []):
            item = objectize_item(raw)
            if item is not None:
                items.append(item)

        pages += 1
        url = payload.get("next")

    logging.info("Fetched %d tracks across %d page(s)", len(items), pages)
    return items


def normalize_for_compare(text: str) -> str:
    """Reduce a name to a comparable form.

    Strips the cruft YouTube bakes into titles -- "(Official Video)",
    "[Audio]", "Lyric Video" -- so a track we downloaded previously is still
    recognised no matter which upload it came from.
    """
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"\[.*?\]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(text.split())


def build_existing_index(directory: str) -> dict:
    """Map normalized name -> filename for everything already downloaded."""
    index: dict[str, str] = {}

    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return index

    for name in names:
        if not name.lower().endswith(".m4a"):
            continue
        # A sidecar in progress also ends in .m4a and must never count as a
        # completed download.
        if ".part." in name:
            continue
        key = normalize_for_compare(os.path.splitext(name)[0])
        if key:
            index.setdefault(key, name)

    return index


def already_downloaded(item: PlaylistItem, existing_index: dict) -> Optional[str]:
    """Return the existing filename for this track, or None if it is missing.

    Exact match on the predictable schema first, then a normalized match so
    files named by YouTube or by hand still count.
    """
    exact = item.target_filename()
    if os.path.exists(os.path.join(SAMPLES_DIR, exact)):
        return exact

    return existing_index.get(normalize_for_compare(item.filename_stem()))


def score_video(video, item: PlaylistItem) -> Optional[float]:
    """Score a YouTube result against a track. Higher is better, None = reject.

    Duration is a hard gate: a result outside the tolerance is a different
    recording, not a worse one, so it is discarded outright. Among survivors,
    title similarity decides, because a 102s official audio track and a 104s
    alternate upload are both plausible on length alone.
    """
    if item.duration_s <= 0 or not video.length:
        return None

    drift = abs(video.length - item.duration_s) / item.duration_s
    if drift > DURATION_TOLERANCE:
        return None

    similarity = difflib.SequenceMatcher(
        None,
        normalize_for_compare(item.search_term()),
        normalize_for_compare(video.title),
    ).ratio()

    # Length agreement still matters, but only as a tiebreaker, so weight it
    # lightly against the title.
    return similarity - (drift / DURATION_TOLERANCE) * 0.1


def get_search_results(artist: str, song: str) -> list:
    """Return YouTube search results for an artist and song."""
    return Search(f"{artist} {song}").videos


def get_download_url(item: PlaylistItem) -> Optional[tuple]:
    """Pick the best YouTube result for a track.

    Returns (url, title) for the winner, or None if nothing survived.
    """
    search_term = item.search_term()

    logging.info("Searching YouTube for: %s", search_term)

    try:
        results = get_search_results(", ".join(item.artists), item.name)
    except Exception as error:
        logging.error("YouTube search failed for %r: %s", search_term, error)
        return None

    if not results:
        logging.warning("No YouTube results for %r", search_term)
        return None

    scored = []
    for video in results:
        # Reading .length or .title can trigger a lazy fetch that raises when
        # the video has been taken down, so every attribute touch is guarded.
        try:
            score = score_video(video, item)
        except Exception as error:
            logging.debug("Skipping unreadable result: %s", error)
            continue
        if score is not None:
            scored.append((score, video))

    if not scored:
        # Nothing matched on length. Rather than silently dropping the track,
        # take the closest and flag it so it can be reviewed or deleted.
        try:
            closest = min(results, key=lambda v: abs((v.length or 0) - item.duration_s))
            closest_url, closest_title, closest_len = closest.watch_url, closest.title, closest.length
        except Exception as error:
            logging.error("Could not read any result for %r: %s", search_term, error)
            return None

        logging.warning(
            "No result within %.0f%% of %ss for %r. Falling back to %r (%.0fs off). Review this one.",
            DURATION_TOLERANCE * 100,
            item.duration_s,
            search_term,
            closest_title,
            abs(closest_len - item.duration_s),
        )
        return closest_url, closest_title

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best = scored[0][1]

    logging.info("Chose %r (score %.2f) for %r", best.title, scored[0][0], search_term)
    return best.watch_url, best.title


def _discard(path: str) -> None:
    """Remove a partial download, ignoring a missing file."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        logging.warning("Could not clean up %s: %s", path, error)


def download_song(url: str, destination: str):
    """Download the audio at url to destination. Returns the path, or None.

    Forces the m4a stream (itag 140). get_audio_only() with no argument
    currently returns that same stream, but it is not contractual, and a webm
    fallback would break the predictable filename this whole dedup scheme
    depends on.

    Downloads to a .part sidecar and renames only on success. A truncated file
    sitting at the real name would be indistinguishable from a good one on the
    next run and would be skipped forever.
    """
    yt = YouTube(url, on_progress_callback=on_progress)

    try:
        stream = yt.streams.get_audio_only(subtype="mp4")
    except Exception as error:
        logging.error("No m4a stream for %s: %s", url, error)
        return None

    if stream is None:
        logging.error("No m4a stream for %s", url)
        return None

    # Keep the .m4a extension on the sidecar so pytubefix picks the right
    # container; the final name is only claimed once bytes are on disk.
    partial = f"{destination}.part.m4a"

    try:
        stream.download(
            output_path=os.path.dirname(partial),
            filename=os.path.basename(partial),
        )
    except Exception as error:
        logging.error("Download failed for %s: %s", url, error)
        _discard(partial)
        return None

    if not os.path.exists(partial) or os.path.getsize(partial) == 0:
        logging.error("Download produced no data for %s", url)
        _discard(partial)
        return None

    try:
        os.replace(partial, destination)
    except OSError as error:
        logging.error("Could not finalise %s: %s", destination, error)
        _discard(partial)
        return None

    logging.info("Downloaded to %s", destination)
    return destination


def download_playlist_items(items: List[PlaylistItem], limit: Optional[int] = None) -> dict:
    """Download every item that is not already on disk. Never raises."""
    os.makedirs(SAMPLES_DIR, exist_ok=True)

    existing_index = build_existing_index(SAMPLES_DIR)
    dry_run = _env_flag("DRY_RUN")

    stats = {"skipped": 0, "downloaded": 0, "failed": 0, "dry_run": 0}
    attempts = 0

    for item in items:
        destination = os.path.join(SAMPLES_DIR, item.target_filename())

        existing = already_downloaded(item, existing_index)
        if existing is not None:
            logging.info("Skipping %s (have %s)", item.filename_stem(), existing)
            stats["skipped"] += 1
            continue

        if limit is not None and attempts >= limit:
            break
        attempts += 1

        try:
            picked = get_download_url(item)
        except Exception as error:
            logging.error("Could not resolve %r: %s", item, error)
            stats["failed"] += 1
            continue

        if picked is None:
            stats["failed"] += 1
            continue

        url, title = picked

        if dry_run:
            logging.info("[dry run] Would download %s -> %s", title, os.path.basename(destination))
            stats["dry_run"] += 1
            continue

        if download_song(url, destination) is None:
            stats["failed"] += 1
            continue

        stats["downloaded"] += 1
        # Record it so two tracks resolving to the same filename do not both
        # download inside a single run.
        existing_index[normalize_for_compare(item.filename_stem())] = item.target_filename()

    return stats


def filter_added_after(items: List[PlaylistItem], cutoff: str) -> List[PlaylistItem]:
    """Keep only tracks added to the playlist after an ISO timestamp."""
    return [item for item in items if item.added_at and item.added_at > cutoff]


def sanitize_stem(text: str) -> str:
    """Reduce arbitrary text to a safe, bounded filename stem.

    Shared by both download paths so a file named via --name and one named from
    a Spotify track are cleaned identically.
    """
    stem = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    stem = _ILLEGAL.sub("", stem)
    stem = _WHITESPACE.sub(" ", stem).strip(" .-")
    return stem[:MAX_FILENAME_STEM].strip(" .-")


def is_youtube_url(candidate: str) -> bool:
    return bool(re.match(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", candidate.strip()))


def download_single_url(url: str, name: Optional[str] = None, directory: str = None) -> int:
    """Download one YouTube video to `directory`. Returns an exit code.

    No Spotify involvement, so this works with no credentials configured. The
    filename comes from --name when given, otherwise the video's own title.
    """
    destination_dir = directory or SAMPLES_DIR

    if not is_youtube_url(url):
        logging.error("Not a YouTube URL: %s", url)
        logging.error("Expected something like https://www.youtube.com/watch?v=...")
        return 2

    try:
        yt = YouTube(url)
        title = yt.title
    except Exception as error:
        logging.error("Could not read %s: %s", url, error)
        return 1

    logging.info("Found: %s", title)

    stem = sanitize_stem(name) if name else sanitize_stem(title)
    if not stem:
        logging.error("Could not derive a filename from %r. Pass --name.", name or title)
        return 2

    destination = os.path.join(destination_dir, f"{stem}.m4a")

    if os.path.exists(destination):
        logging.info("Already have %s, skipping.", destination)
        return 0

    if _env_flag("DRY_RUN"):
        logging.info("[dry run] Would download %s -> %s", title, os.path.basename(destination))
        return 0

    os.makedirs(destination_dir, exist_ok=True)

    if download_song(url, destination) is None:
        return 1

    return 0


def parse_args(argv: list):
    parser = argparse.ArgumentParser(
        prog="sample_downloader.py",
        description="Download Spotify playlist tracks or a single YouTube video as m4a.",
    )
    subparsers = parser.add_subparsers(dest="command")

    url_parser = subparsers.add_parser("url", help="Download a single YouTube video.")
    url_parser.add_argument("url", help="A YouTube watch, youtu.be, or shorts URL.")
    url_parser.add_argument(
        "--name",
        default=None,
        help="Output filename stem. Defaults to the video's own title.",
    )
    url_parser.add_argument(
        "--dir",
        default=None,
        help=f"Output directory. Defaults to SAMPLES_DIR ({SAMPLES_DIR}).",
    )

    return parser.parse_args(argv)


def run_app() -> int:
    token = authenticate()

    items = get_playlist_items(token, PLAYLIST_ID)

    cutoff = os.environ.get("ADDED_AFTER")
    if cutoff:
        logging.info("Filtering to tracks added after %s", cutoff)
        items = filter_added_after(items, cutoff)
        logging.info("%d track(s) match the cutoff", len(items))

    limit_env = os.environ.get("LIMIT")
    limit = int(limit_env) if limit_env else None
    if limit is not None:
        logging.info("LIMIT=%d set, will stop after %d download attempt(s)", limit, limit)

    stats = download_playlist_items(items, limit=limit)

    logging.info(
        "Done. %d downloaded, %d skipped, %d failed%s",
        stats["downloaded"],
        stats["skipped"],
        stats["failed"],
        f", {stats['dry_run']} would download (dry run)" if _env_flag("DRY_RUN") else "",
    )

    return 0 if stats["failed"] == 0 else 1


def main(argv: Optional[list] = None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.command == "url":
        return download_single_url(args.url, name=args.name, directory=args.dir)

    try:
        return run_app()
    except KeyError as error:
        logging.error("%s", error.args[0] if error.args else error)
        return 2
    except Exception as error:
        logging.error("Fatal: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
