from pathlib import Path
import unittest

from App.classify import classify_file


class ClassifyTests(unittest.TestCase):
    def test_definite_photo(self):
        self.assertEqual(classify_file(Path("IMG_0001.HEIC")), ("photo", "definite"))

    def test_definite_video(self):
        self.assertEqual(classify_file(Path("VID_0001.MOV")), ("video", "definite"))

    def test_dat_is_retained_as_candidate_video(self):
        self.assertEqual(classify_file(Path("first_birthday.DAT")), ("video", "candidate"))

    def test_sidecar(self):
        self.assertEqual(classify_file(Path("IMG_0001.AAE")), ("sidecar", "companion"))

    def test_non_media_is_ignored(self):
        self.assertIsNone(classify_file(Path("project.sqlite")))


if __name__ == "__main__":
    unittest.main()
