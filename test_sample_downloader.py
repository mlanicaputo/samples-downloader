"""Tests for the pure helpers in sample_downloader.

These cover the logic that decides what gets downloaded and what it gets
called, none of which needs the network. Run with `make test`.

Nothing here touches Spotify or YouTube. The download path is exercised
separately by `make test-pull`.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sample_downloader as sd


class FakeVideo:
    """Stands in for a pytubefix stream object.

    Only .title and .length are read by score_video, and .watch_url by the
    caller, so that is all this needs.
    """

    def __init__(self, title, length, watch_url="https://youtu.be/fake"):
        self.title = title
        self.length = length
        self.watch_url = watch_url


def make_item(name, artists=("Some Artist",), duration_ms=200_000):
    return sd.PlaylistItem(list(artists), name, duration_ms=duration_ms)


class NormalizeForCompare(unittest.TestCase):
    """Parentheticals and punctuation must not affect identity."""

    def test_strips_parentheticals(self):
        self.assertEqual(
            sd.normalize_for_compare("Example Artist - Song (Official Video)"),
            sd.normalize_for_compare("Example Artist - Song"),
        )

    def test_strips_brackets(self):
        self.assertEqual(
            sd.normalize_for_compare("Example Artist - Interlude [Audio]"),
            sd.normalize_for_compare("Example Artist - Interlude"),
        )

    def test_is_case_insensitive(self):
        self.assertEqual(
            sd.normalize_for_compare("Example Artist - Example Song"),
            sd.normalize_for_compare("example artist - example song"),
        )

    def test_strips_punctuation(self):
        # Punctuation is dropped, but the words around it are kept. "and" is
        # deliberately not treated as an "&" -- they are different names.
        self.assertEqual(
            sd.normalize_for_compare("Example Artist - Song (Official Video)"),
            sd.normalize_for_compare("Example Artist, Song!"),
        )
        self.assertNotEqual(
            sd.normalize_for_compare("Example Artist & Band - Song"),
            sd.normalize_for_compare("Example Artist and Band - Song"),
        )

    def test_ampersand_is_dropped(self):
        # "&" is punctuation, so it vanishes and the words close up.
        self.assertEqual(
            sd.normalize_for_compare("Earth, Wind & Fire - Boogie"),
            sd.normalize_for_compare("Earth Wind Fire Boogie"),
        )

    def test_drops_accents(self):
        self.assertEqual(
            sd.normalize_for_compare("Beyoncé - Déjà Vu"),
            sd.normalize_for_compare("Beyonce - Deja Vu"),
        )

    def test_nested_parentheticals_fully_removed(self):
        self.assertEqual(
            sd.normalize_for_compare("Example Artist - Song (Official Video (HD))"),
            sd.normalize_for_compare("Example Artist - Song"),
        )

    def test_genuinely_different_titles_stay_different(self):
        self.assertNotEqual(
            sd.normalize_for_compare("Example Artist - Song"),
            sd.normalize_for_compare("Example Artist - Song Remix"),
        )


class SanitizeStem(unittest.TestCase):
    """Filenames must survive the filesystem and stay bounded."""

    def test_keeps_ordinary_text(self):
        self.assertEqual(sd.sanitize_stem("Example Artist - Example Track"), "Example Artist - Example Track")

    def test_removes_path_separators(self):
        # A slash would silently create a subdirectory; a backslash is illegal
        # on Windows. Each needs its own input -- asserting a string contains no
        # "\" when it never had one proves nothing.
        self.assertNotIn("/", sd.sanitize_stem("A/B - Song"))
        self.assertNotIn("\\", sd.sanitize_stem("A\\B - Song"))

    def test_removes_colon(self):
        # A colon is legal on macOS but breaks Finder aliases and Windows.
        self.assertNotIn(":", sd.sanitize_stem("Jay-Z - 4:44"))

    def test_removes_question_and_asterisk(self):
        result = sd.sanitize_stem("Really? What* Ever")
        self.assertNotIn("?", result)
        self.assertNotIn("*", result)

    def test_collapses_whitespace(self):
        self.assertEqual(sd.sanitize_stem("A    B   C"), "A B C")

    def test_tabs_become_spaces_rather_than_vanishing(self):
        # A tab used to be deleted outright, gluing the words either side of
        # it together into "AB". It must be treated as a separator.
        self.assertEqual(sd.sanitize_stem("A\tB"), "A B")
        self.assertEqual(sd.sanitize_stem("A\nB"), "A B")

    def test_newlines_become_spaces(self):
        self.assertEqual(sd.sanitize_stem("Line One\nLine Two"), "Line One Line Two")

    def test_strips_leading_and_trailing_dots(self):
        # A leading dot would make it a hidden file.
        self.assertFalse(sd.sanitize_stem("  ...Hidden...  ").startswith("."))
        self.assertFalse(sd.sanitize_stem("  ...Hidden...  ").endswith("."))

    def test_bounds_length(self):
        # macOS caps a path component at 255 bytes.
        self.assertLessEqual(len(sd.sanitize_stem("A" * 500)), sd.MAX_FILENAME_STEM)

    def test_empty_and_whitespace_only(self):
        self.assertEqual(sd.sanitize_stem(""), "")
        self.assertEqual(sd.sanitize_stem("   "), "")

    def test_drops_non_ascii_entirely(self):
        # normalize+encode ascii drops these; the result must still be usable.
        self.assertEqual(sd.sanitize_stem(""), sd.sanitize_stem("日本語"))


class FilenameStemMatchesSanitize(unittest.TestCase):
    """The playlist path and the --name path must name files identically.

    This is the regression guard for deduplicated sanitization. If these drift,
    a file downloaded via the playlist and one downloaded via `url --name` with
    the same text get different names, and the dedup key stops matching.
    """

    def test_same_text_yields_same_stem(self):
        item = make_item("Example Track", artists=("Example Artist",))
        self.assertEqual(
            item.filename_stem(),
            sd.sanitize_stem("Example Artist - Example Track"),
        )

    def test_hostile_characters_handled_the_same(self):
        item = make_item("What* Ever: Really?", artists=("A/B",))
        self.assertEqual(
            item.filename_stem(),
            sd.sanitize_stem("A/B - What* Ever: Really?"),
        )

    def test_deterministic_across_calls(self):
        item = make_item("Example Song", artists=("Example Artist",))
        self.assertEqual(item.filename_stem(), item.filename_stem())

    def test_is_track_data_only(self):
        # The dedup key must never depend on the YouTube result, or the same
        # track would get a different name on each run.
        item = make_item("Example Song", artists=("Example Artist",))
        self.assertEqual(
            item.filename_stem(), make_item("Example Song", artists=("Example Artist",)).filename_stem()
        )

    def test_missing_artists_falls_back(self):
        item = sd.PlaylistItem([], "Untitled Track")
        self.assertIn("Unknown Artist", item.filename_stem())

    def test_target_filename_extension(self):
        self.assertTrue(make_item("X", artists=("Y",)).target_filename().endswith(".m4a"))

    def test_multiple_artists_joined(self):
        item = make_item("Song", artists=("A", "B"))
        self.assertTrue(item.filename_stem().startswith("A, B - "))


class ScoreVideo(unittest.TestCase):
    """Duration is a hard gate; title similarity breaks ties among survivors.

    These cases encode the reason the matcher exists: a track called "Intro"
    must not match a 10 minute mix, and a 102s official upload must beat a
    104s alternate that YouTube happened to rank first.

    Track names here are invented. Real titles would be a personal detail that
    does not belong in a repo other people are meant to clone.
    """

    def setUp(self):
        self.item = make_item("Example Track", artists=("Example Artist",), duration_ms=203_000)

    def test_exact_length_and_title_scores_high(self):
        video = FakeVideo("Example Artist - Example Track", 203)
        self.assertGreater(sd.score_video(video, self.item), 0.9)

    def test_rejects_far_too_long(self):
        # A 10 minute mix for a 3:23 track.
        self.assertIsNone(
            sd.score_video(FakeVideo("Example Artist - Example Track", 600), self.item)
        )

    def test_rejects_far_too_short(self):
        # A 30 second preview.
        self.assertIsNone(
            sd.score_video(FakeVideo("Example Artist - Example Track", 30), self.item)
        )

    def test_within_tolerance_is_accepted(self):
        # 8% off, inside the default 15% gate.
        self.assertIsNotNone(
            sd.score_video(FakeVideo("Example Artist - Example Track", 187), self.item)
        )

    def test_outside_tolerance_is_rejected(self):
        # 20% off, outside the gate.
        self.assertIsNone(
            sd.score_video(FakeVideo("Example Artist - Example Track", 162), self.item)
        )

    def test_title_beats_a_closer_but_wrong_result(self):
        """The case this heuristic exists for.

        YouTube ranked a 104s upload first, but the 102s upload is the one
        whose title actually matches. Length alone cannot separate them, so
        without the title term this test fails.
        """
        item = make_item("Example Interlude", artists=("Example Artist",), duration_ms=102_173)
        ranked_first = FakeVideo("Example Artist - Something Else (Official Audio)", 104)
        actually_right = FakeVideo("Example Artist - Example Interlude", 102)

        self.assertGreater(
            sd.score_video(actually_right, item),
            sd.score_video(ranked_first, item),
        )

    def test_rejects_zero_length_video(self):
        self.assertIsNone(sd.score_video(FakeVideo("Anything", 0), self.item))

    def test_rejects_missing_length(self):
        self.assertIsNone(sd.score_video(FakeVideo("Anything", None), self.item))

    def test_tolerates_item_without_duration(self):
        # A track with no duration_ms must not crash the scorer.
        item = make_item("Unknown Length", duration_ms=0)
        self.assertIsNone(sd.score_video(FakeVideo("Anything", 200), item))

    def test_cruft_in_title_does_not_hurt_score(self):
        # "(Official Video)" should not read as a mismatched title.
        with_cruft = FakeVideo("Example Artist - Example Track (Official Music Video)", 203)
        without = FakeVideo("Example Artist - Example Track", 203)
        self.assertAlmostEqual(
            sd.score_video(with_cruft, self.item),
            sd.score_video(without, self.item),
            places=6,
        )

    def test_live_video_in_a_mix_is_still_length_gated(self):
        # "Example Track (Extended Mix)" is 10 minutes; length rejects it even
        # though the title looks close.
        self.assertIsNone(
            sd.score_video(
                FakeVideo("Example Artist - Example Track (Extended Mix)", 600), self.item
            )
        )


class IsYoutubeUrl(unittest.TestCase):
    def test_accepts_watch_url(self):
        self.assertTrue(sd.is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_accepts_short_url(self):
        self.assertTrue(sd.is_youtube_url("https://youtu.be/dQw4w9WgXcQ"))

    def test_accepts_mobile_and_shorts(self):
        self.assertTrue(sd.is_youtube_url("https://m.youtube.com/watch?v=x"))
        self.assertTrue(sd.is_youtube_url("https://www.youtube.com/shorts/abc123"))

    def test_accepts_surrounding_whitespace(self):
        self.assertTrue(sd.is_youtube_url("  https://youtu.be/dQw4w9WgXcQ  "))

    def test_rejects_other_hosts(self):
        self.assertFalse(sd.is_youtube_url("https://vimeo.com/12345"))
        self.assertFalse(sd.is_youtube_url("https://example.com/watch?v=x"))

    def test_rejects_lookalike_host(self):
        # Not youtube.com, just a domain that ends in it.
        self.assertFalse(sd.is_youtube_url("https://notyoutube.com/watch?v=x"))

    def test_rejects_junk(self):
        self.assertFalse(sd.is_youtube_url("notaurl"))
        self.assertFalse(sd.is_youtube_url(""))


class BuildExistingIndex(unittest.TestCase):
    """The index decides what counts as already downloaded."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def write(self, name):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(b"\x00")
        return path

    def test_indexes_m4a(self):
        self.write("Example Artist - Example Track.m4a")
        index = sd.build_existing_index(self.dir)
        self.assertIn(sd.normalize_for_compare("Example Artist - Example Track"), index)

    def test_ignores_non_m4a(self):
        self.write("notes.txt")
        self.write("track.mp3")
        self.assertEqual(sd.build_existing_index(self.dir), {})

    def test_ignores_part_sidecars(self):
        # A download in progress must never count as complete.
        self.write("Example Artist - Example Track.m4a.part.m4a")
        self.assertEqual(sd.build_existing_index(self.dir), {})

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(sd.build_existing_index(os.path.join(self.dir, "nope")), {})

    def test_first_of_duplicate_normalized_names_wins(self):
        self.write("Example Artist - Song (Official Video).m4a")
        self.write("Example Artist - Song.m4a")
        index = sd.build_existing_index(self.dir)
        matched = index[sd.normalize_for_compare("Example Artist - Song")]
        self.assertTrue(matched.endswith(".m4a"))
        self.assertIsNotNone(matched)


class AlreadyDownloaded(unittest.TestCase):
    """Exact match first, then the looser normalized match."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.item = make_item("Example Song", artists=("Example Artist",), duration_ms=200_000)

    def write(self, name):
        with open(os.path.join(self.dir, name), "wb") as fh:
            fh.write(b"\x00")

    def test_absent(self):
        index = sd.build_existing_index(self.dir)
        self.assertIsNone(sd.already_downloaded(self.item, index))

    def test_exact_match(self):
        name = self.item.target_filename()
        self.write(name)
        self.assertEqual(sd.already_downloaded(self.item, sd.build_existing_index(self.dir)), name)

    def test_normalized_match_on_youtube_cruft(self):
        # The case that motivated the normalized pass.
        self.write("Example Artist - Example Song (Official Video).m4a")
        found = sd.already_downloaded(self.item, sd.build_existing_index(self.dir))
        self.assertEqual(found, "Example Artist - Example Song (Official Video).m4a")

    def test_different_song_is_not_a_match(self):
        self.write("Example Artist - Other Song (Official Video).m4a")
        self.assertIsNone(sd.already_downloaded(self.item, sd.build_existing_index(self.dir)))

    def test_part_sidecar_does_not_count(self):
        self.write(f"{self.item.target_filename()}.part.m4a")
        self.assertIsNone(sd.already_downloaded(self.item, sd.build_existing_index(self.dir)))


class FilterAddedAfter(unittest.TestCase):
    def test_keeps_later(self):
        items = [make_item("a"), make_item("b")]
        items[0]._added_at = "2024-01-01T00:00:00Z"
        items[1]._added_at = "2025-01-01T00:00:00Z"
        kept = sd.filter_added_after(items, "2024-06-01T00:00:00Z")
        self.assertEqual([i.name for i in kept], ["b"])

    def test_excludes_earlier(self):
        item = make_item("a")
        item._added_at = "2023-01-01T00:00:00Z"
        self.assertEqual(sd.filter_added_after([item], "2024-01-01T00:00:00Z"), [])

    def test_items_without_timestamp_are_dropped(self):
        self.assertEqual(sd.filter_added_after([make_item("a")], "2024-01-01T00:00:00Z"), [])


class ObjectizeItem(unittest.TestCase):
    """Non-track playlist entries must be skipped, not crash the run."""

    def test_track(self):
        item = sd.objectize_item(
            {
                "added_at": "2024-01-01T00:00:00Z",
                "track": {
                    "type": "track",
                    "name": "Song",
                    "artists": [{"name": "Artist"}],
                    "duration_ms": 1000,
                    "id": "abc",
                    "external_ids": {"isrc": "USABC1234567"},
                },
            }
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.name, "Song")
        self.assertEqual(item.artists, ["Artist"])
        self.assertEqual(item.isrc, "USABC1234567")

    def test_podcast_episode_skipped(self):
        self.assertIsNone(
            sd.objectize_item({"track": {"type": "episode", "name": "Ep", "artists": []}})
        )

    def test_local_file_skipped(self):
        self.assertIsNone(
            sd.objectize_item(
                {
                    "track": {
                        "type": "track",
                        "is_local": True,
                        "name": "Local",
                        "artists": [{"name": "A"}],
                    }
                }
            )
        )

    def test_removed_entry_skipped(self):
        self.assertIsNone(sd.objectize_item({"track": None}))

    def test_missing_track_key_skipped(self):
        self.assertIsNone(sd.objectize_item({}))

    def test_missing_artists_skipped(self):
        self.assertIsNone(sd.objectize_item({"track": {"type": "track", "name": "S"}}))

    def test_tolerates_absent_optional_fields(self):
        item = sd.objectize_item(
            {
                "track": {
                    "type": "track",
                    "name": "S",
                    "artists": [{"name": "A"}],
                }
            }
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.duration_ms, 0)
        self.assertIsNone(item.isrc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
