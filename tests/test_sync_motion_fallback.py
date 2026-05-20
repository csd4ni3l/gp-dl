import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gp_dl import sync


class FakeDriver:
    def __init__(self):
        self.current_url = "https://photos.google.com/photo/previous"
        self.visited_urls = []

    def get(self, url):
        self.current_url = url
        self.visited_urls.append(url)


class MotionPhotoFallbackTests(unittest.TestCase):
    def test_failed_motion_download_uses_pre_failure_image_url_for_direct_fallback(
        self,
    ):
        driver = FakeDriver()
        item_url = "https://photos.google.com/photo/AF1Qip-current"
        captured_image_url = "https://lh3.googleusercontent.com/current-photo=d"
        fallback_calls = []

        with (
            tempfile.TemporaryDirectory() as output_dir,
            tempfile.TemporaryDirectory() as temp_dir,
        ):
            output_path = Path(output_dir)
            temp_dir_path = Path(temp_dir)

            def fake_download_motion_photo_still(*args, **kwargs):
                fallback_calls.append(kwargs)
                (temp_dir_path / "current.jpg").write_bytes(b"current image")
                return "current.jpg"

            with (
                patch.object(sync, "MOTION_PHOTO_DIRECT_SAVE_ONLY", False),
                patch.object(sync, "_is_motion_photo_page", return_value=True),
                patch.object(
                    sync, "_photo_image_download_url", return_value=captured_image_url
                ),
                patch.object(
                    sync, "_start_download_with_keyboard_shortcut", return_value=True
                ),
                patch.object(
                    sync, "_motion_photo_page_looks_broken", return_value=True
                ),
                patch.object(
                    sync,
                    "_download_motion_photo_still",
                    side_effect=fake_download_motion_photo_still,
                ),
                patch.object(
                    sync, "_local_album_google_id_files", return_value=({}, [])
                ),
                patch.object(
                    sync, "_local_album_files_by_normalized_name", return_value=({}, [])
                ),
                patch.object(sync, "_record_google_id_file"),
            ):
                downloaded, skipped, failed = sync._download_individual_album_items(
                    driver,
                    [
                        {
                            "google_id": "AF1Qip-current",
                            "url": item_url,
                            "identifiers": "current.jpg",
                        }
                    ],
                    "Album",
                    output_path,
                    temp_dir_path,
                )

        self.assertEqual((downloaded, skipped, failed), (1, 0, 0))
        self.assertEqual(len(fallback_calls), 1)
        self.assertEqual(fallback_calls[0]["image_url"], captured_image_url)
        self.assertEqual(fallback_calls[0]["referer_url"], item_url)


if __name__ == "__main__":
    unittest.main()
