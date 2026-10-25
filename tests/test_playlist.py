import unittest

from playlist import (
    EmptyPlaylistError,
    PlaylistExtractionError,
    PlaylistUnavailableError,
    classify_error,
    fetch_playlist,
    fetch_playlist_async,
    parse_playlist_info,
)

PID = "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


def vid(n: int) -> str:
    return f"vid{n:08d}"  # 11 chars


def raw(n: int, title: str | None = None) -> dict:
    return {"id": vid(n), "title": title or f"Video {n}"}


class ParseInfoTests(unittest.TestCase):
    def test_order_is_preserved_and_positions_are_one_based(self):
        # Deliberately non-alphabetical / non-numeric order and misleading titles.
        info = {"title": "Course", "entries": [raw(9, "Episode 3"), raw(2, "Episode 1"), raw(5, "Episode 2")]}
        playlist = parse_playlist_info(info, PID, 100)
        self.assertEqual([e.position for e in playlist.entries], [1, 2, 3])
        self.assertEqual([e.video_id for e in playlist.entries], [vid(9), vid(2), vid(5)])
        self.assertEqual(playlist.entries[0].url, f"https://www.youtube.com/watch?v={vid(9)}")
        self.assertEqual(playlist.total, 3)

    def test_empty_playlist(self):
        for info in ({"entries": []}, {"entries": None}, {}):
            with self.assertRaises(EmptyPlaylistError):
                parse_playlist_info(info, PID, 100)

    def test_unavailable_entries_keep_positions_and_are_reported(self):
        info = {
            "entries": [
                raw(1),
                {"id": vid(2), "title": "[Private video]"},
                {"id": vid(3), "title": "[Deleted video]"},
                None,
                {"title": "No id here"},
                {"id": "bad id!", "title": "Bad"},
                raw(7),
            ]
        }
        playlist = parse_playlist_info(info, PID, 100)
        self.assertEqual([(e.position, e.video_id) for e in playlist.entries], [(1, vid(1)), (7, vid(7))])
        self.assertEqual([(s.position, s.reason) for s in playlist.skipped],
                         [(2, "private video"), (3, "deleted video"), (4, "no data returned"),
                          (5, "no usable video ID"), (6, "no usable video ID")])
        self.assertEqual(playlist.total, 7)

    def test_video_id_fallback_from_url(self):
        info = {"entries": [{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "title": "T"}]}
        self.assertEqual(parse_playlist_info(info, PID, 10).entries[0].video_id, "dQw4w9WgXcQ")

    def test_truncation(self):
        info = {"entries": [raw(i) for i in range(1, 8)]}
        playlist = parse_playlist_info(info, PID, 5)
        self.assertTrue(playlist.truncated)
        self.assertEqual(len(playlist.entries), 5)
        self.assertEqual(playlist.entries[-1].position, 5)

    def test_count_mismatch_flag(self):
        info = {"playlist_count": 10, "entries": [raw(1), raw(2)]}
        self.assertTrue(parse_playlist_info(info, PID, 100).count_mismatch)
        info["playlist_count"] = 2
        self.assertFalse(parse_playlist_info(info, PID, 100).count_mismatch)

    def test_titles_are_sanitised(self):
        info = {"entries": [raw(1, "Line1\nLine2\x00 " + "x" * 500)]}
        title = parse_playlist_info(info, PID, 10).entries[0].title
        self.assertNotIn("\n", title)
        self.assertNotIn("\x00", title)
        self.assertLessEqual(len(title), 200)

    def test_generator_entries_are_supported(self):
        info = {"entries": (raw(i) for i in range(1, 4))}
        self.assertEqual(len(parse_playlist_info(info, PID, 10).entries), 3)


class FetchTests(unittest.TestCase):
    def test_fetch_uses_canonical_playlist_url_and_flat_options(self):
        seen = {}

        def extractor(url, options):
            seen["url"], seen["options"] = url, options
            return {"title": "T", "entries": [raw(1), raw(2)]}

        playlist = fetch_playlist(PID, 100, extractor)
        # Always the playlist URL, never a watch URL, so entry 1 is always first.
        self.assertEqual(seen["url"], f"https://www.youtube.com/playlist?list={PID}")
        self.assertEqual(seen["options"]["extract_flat"], "in_playlist")
        self.assertTrue(seen["options"]["skip_download"])
        self.assertEqual(len(playlist.entries), 2)

    def test_non_dict_result(self):
        with self.assertRaises(PlaylistExtractionError):
            fetch_playlist(PID, 10, lambda url, options: None)


class FetchAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_transient_errors_then_succeeds(self):
        calls = []

        def extractor(url, options):
            calls.append(1)
            if len(calls) < 3:
                raise PlaylistExtractionError("temporary")
            return {"entries": [raw(1)]}

        playlist = await fetch_playlist_async(PID, max_entries=10, timeout=5, retries=3, backoff=0, extractor=extractor)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(playlist.entries), 1)

    async def test_gives_up_after_retries(self):
        def extractor(url, options):
            raise OSError("network down")

        with self.assertRaises(PlaylistExtractionError):
            await fetch_playlist_async(PID, max_entries=10, timeout=5, retries=2, backoff=0, extractor=extractor)

    async def test_permanent_errors_are_not_retried(self):
        calls = []

        def extractor(url, options):
            calls.append(1)
            raise PlaylistUnavailableError("private")

        with self.assertRaises(PlaylistUnavailableError):
            await fetch_playlist_async(PID, max_entries=10, timeout=5, retries=3, backoff=0, extractor=extractor)
        self.assertEqual(len(calls), 1)

    async def test_empty_playlist_not_retried(self):
        calls = []

        def extractor(url, options):
            calls.append(1)
            return {"entries": []}

        with self.assertRaises(EmptyPlaylistError):
            await fetch_playlist_async(PID, max_entries=10, timeout=5, retries=3, backoff=0, extractor=extractor)
        self.assertEqual(len(calls), 1)


class ClassifyErrorTests(unittest.TestCase):
    def test_classification(self):
        self.assertIsInstance(classify_error("ERROR: The playlist does not exist."), PlaylistUnavailableError)
        self.assertIsInstance(classify_error("This playlist is private"), PlaylistUnavailableError)
        self.assertIsInstance(classify_error("Sign in to confirm you’re not a bot"), PlaylistExtractionError)
        self.assertIsInstance(classify_error("HTTP Error 503"), PlaylistExtractionError)


if __name__ == "__main__":
    unittest.main()
