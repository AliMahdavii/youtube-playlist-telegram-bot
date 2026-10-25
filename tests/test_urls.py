import unittest

from playlist import (
    InvalidURLError,
    NoPlaylistError,
    UnsupportedPlaylistError,
    build_video_url,
    parse_playlist_url,
)

PID = "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


class ParsePlaylistUrlTests(unittest.TestCase):
    def test_standard_playlist_url(self):
        self.assertEqual(parse_playlist_url(f"https://www.youtube.com/playlist?list={PID}"), PID)

    def test_video_url_with_playlist(self):
        url = f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PID}"
        self.assertEqual(parse_playlist_url(url), PID)

    def test_selected_video_and_index_are_ignored(self):
        url = f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PID}&index=15&t=30s"
        self.assertEqual(parse_playlist_url(url), PID)

    def test_list_parameter_before_video(self):
        self.assertEqual(parse_playlist_url(f"https://www.youtube.com/watch?list={PID}&v=dQw4w9WgXcQ"), PID)

    def test_short_domain(self):
        self.assertEqual(parse_playlist_url(f"https://youtu.be/dQw4w9WgXcQ?list={PID}"), PID)

    def test_mobile_and_music_hosts(self):
        self.assertEqual(parse_playlist_url(f"https://m.youtube.com/playlist?list={PID}"), PID)
        self.assertEqual(parse_playlist_url(f"https://music.youtube.com/playlist?list={PID}"), PID)

    def test_missing_scheme(self):
        self.assertEqual(parse_playlist_url(f"youtube.com/playlist?list={PID}"), PID)

    def test_video_without_playlist(self):
        with self.assertRaises(NoPlaylistError):
            parse_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        with self.assertRaises(NoPlaylistError):
            parse_playlist_url("https://youtu.be/dQw4w9WgXcQ")

    def test_playlist_path_without_list(self):
        with self.assertRaises(InvalidURLError):
            parse_playlist_url("https://www.youtube.com/playlist")

    def test_invalid_urls(self):
        bad = [
            "",
            "hello world",
            "not a url",
            "https://example.com/playlist?list=" + PID,
            "https://youtube.com.evil.com/playlist?list=" + PID,
            "https://evil.com/?u=youtube.com/playlist?list=" + PID,
            "ftp://www.youtube.com/playlist?list=" + PID,
            "javascript:alert(1)",
            "https://www.youtube.com/",
            "https://www.youtube.com/channel/UCabc?list=" + PID,
            "https://www.youtube.com/playlist?list=PL123;rm -rf /",
            "https://www.youtube.com/playlist?list=$(whoami)abcdefgh",
            "https://www.youtube.com/playlist?list=short",
            "https://www.youtube.com/playlist?list=" + "A" * 200,
            "https://www.youtube.com/playlist?list=" + PID + "x" * 3000,
        ]
        for text in bad:
            with self.subTest(text=text[:60]):
                with self.assertRaises(InvalidURLError):
                    parse_playlist_url(text)

    def test_mix_playlists_are_unsupported(self):
        with self.assertRaises(UnsupportedPlaylistError):
            parse_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ")

    def test_none_input(self):
        with self.assertRaises(InvalidURLError):
            parse_playlist_url(None)  # type: ignore[arg-type]


class BuildVideoUrlTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(build_video_url("dQw4w9WgXcQ"), "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_invalid(self):
        for bad in ("", "short", "dQw4w9WgXcQ&x=1", "dQw4w9WgXc!", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    build_video_url(bad)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
