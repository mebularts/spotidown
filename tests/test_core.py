from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from spotidown_core.engine import audio_files_by_identity, create_apple_music_export
from spotidown_core.library import Library, track_identity


class SpotiDownCoreTests(unittest.TestCase):
    def test_clean_filename_hardlink_and_isrc_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "downloads"
            incoming = output / ".incoming"
            incoming.mkdir(parents=True)

            track = {
                "song_id": "1234567890123456789012",
                "isrc": "TRTEST260001",
                "name": "Test Song",
                "artists": ["Test Artist"],
                "duration": 123,
                "list_name": "Test Playlist",
                "list_position": 1,
            }
            temp_file = incoming / "1234567890123456789012 - Test Artist - Test Song.mp3"
            temp_file.write_bytes(b"test-audio")

            with Library(root / "library.sqlite") as library:
                library.register_manifest([track], "spotify:test")
                existing = audio_files_by_identity(output, [track], library, "test")
                final = existing[track_identity(track)]

                self.assertEqual(final.name, "Test Artist - Test Song.mp3")
                self.assertNotIn("1234567890123456789012", final.name)

                export, linked, failed = create_apple_music_export(
                    root / "exports",
                    [track],
                    {track_identity(track)},
                    existing,
                )
                self.assertIsNotNone(export)
                self.assertEqual(linked, 1)
                self.assertEqual(failed, 0)
                export_file = next(path for path in export.iterdir() if path.suffix == ".mp3")
                self.assertEqual(os.stat(final).st_ino, os.stat(export_file).st_ino)

                same_recording = dict(track)
                same_recording["song_id"] = "ABCDEFGHIJKL1234567890"
                library.register_manifest([same_recording], "spotify:test2")
                self.assertEqual(library.path_for_track(same_recording), final)


if __name__ == "__main__":
    unittest.main()
