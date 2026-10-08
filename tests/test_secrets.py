import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.secrets import read_setting


class SecretFileTests(unittest.TestCase):
    def test_file_and_value_are_mutually_exclusive(self):
        with patch.dict(os.environ, {'FIXTURE': 'value', 'FIXTURE_FILE': '/unused'}, clear=True):
            with self.assertRaises(ValueError):
                read_setting('FIXTURE')

    def test_private_file_read(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / 'credential'
            secret.write_text('fixture-only\n')
            with patch.dict(os.environ, {'FIXTURE_FILE': str(secret)}, clear=True):
                self.assertEqual(read_setting('FIXTURE'), 'fixture-only')

    def test_missing_file_does_not_fall_back(self):
        with patch.dict(os.environ, {'FIXTURE_FILE': '/missing-credential-fixture'}, clear=True):
            with self.assertRaises(FileNotFoundError):
                read_setting('FIXTURE')
