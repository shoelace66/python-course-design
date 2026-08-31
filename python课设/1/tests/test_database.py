from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from music_analyzer.audio import analyze_wav, generate_demo_wav
from music_analyzer.database import MusicDatabase, file_sha256


class DatabaseTests(unittest.TestCase):
    def test_insert_export_import_and_delete_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = generate_demo_wav(root / "demo.wav", seconds=3)
            result = analyze_wav(audio_path)
            database = MusicDatabase(root / "source.db")
            analysis_id = database.insert_analysis(
                file_name="中文演示音频.wav",
                stored_name="demo.wav",
                file_hash=file_sha256(audio_path),
                file_size=audio_path.stat().st_size,
                source="test",
                result=result,
                elapsed_ms=123,
            )

            record = database.get_analysis(analysis_id)
            self.assertIsNotNone(record)
            self.assertEqual(record["file_name"], "中文演示音频.wav")
            self.assertEqual(database.statistics()["analysis_count"], 1)
            self.assertIn("waveform", record["feature_data"])

            target = MusicDatabase(root / "target.db")
            report = target.import_records(database.all_records(), "roundtrip.json")
            self.assertEqual(report["success"], 1)
            self.assertEqual(target.statistics()["analysis_count"], 1)
            restored = target.all_records()[0]
            self.assertEqual(restored["classification"], record["classification"])
            self.assertIn("chroma", restored["feature_data"])

            duplicate = target.import_records(database.all_records(), "roundtrip.json")
            self.assertEqual(duplicate["skipped"], 1)
            self.assertEqual(target.statistics()["analysis_count"], 1)

            poisoned = target.import_records(
                [{"file_name": "bad.wav", "feature_data": {"curve": [float("nan")]}}],
                "poisoned.json",
            )
            self.assertEqual(poisoned["failed"], 1)
            self.assertEqual(target.statistics()["analysis_count"], 1)

            stored_name = database.delete_analysis(analysis_id)
            self.assertEqual(stored_name, "demo.wav")
            self.assertEqual(database.statistics()["analysis_count"], 0)

    def test_sqlite_backup_is_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = MusicDatabase(root / "source.db")
            backup = database.backup_to(root / "backup.db")
            copied = MusicDatabase(backup)
            self.assertEqual(copied.statistics()["analysis_count"], 0)


if __name__ == "__main__":
    unittest.main()
