from __future__ import annotations

import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from music_analyzer.audio import AudioAnalysisError, analyze_wav, generate_demo_wav


class AudioAnalyzerTests(unittest.TestCase):
    def test_440_hz_tone_is_detected_near_a4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a4.wav"
            sample_rate = 11_025
            pcm = bytearray()
            for index in range(sample_rate * 2):
                value = 0.55 * math.sin(2 * math.pi * 440 * index / sample_rate)
                pcm.extend(struct.pack("<h", int(value * 32767)))
            with wave.open(str(path), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(sample_rate)
                writer.writeframes(bytes(pcm))

            result = analyze_wav(path)
            self.assertGreater(result.avg_pitch_hz, 420)
            self.assertLess(result.avg_pitch_hz, 460)
            self.assertEqual(result.pitch_note, "A4")
            self.assertLess(result.rms_db, 0)
            self.assertGreater(len(result.feature_data["waveform"]), 100)
            self.assertEqual(len(result.feature_data["chroma"]), 12)

            # 完全反相的双声道若直接求平均会变成静音；分析器应自动选取
            # 有效声道，仍能识别 A4。
            anti_phase_path = Path(directory) / "a4_antiphase_stereo.wav"
            stereo_pcm = bytearray()
            for index in range(sample_rate):
                sample = int(0.5 * math.sin(2 * math.pi * 440 * index / sample_rate) * 32767)
                stereo_pcm.extend(struct.pack("<hh", sample, -sample))
            with wave.open(str(anti_phase_path), "wb") as writer:
                writer.setnchannels(2)
                writer.setsampwidth(2)
                writer.setframerate(sample_rate)
                writer.writeframes(bytes(stereo_pcm))
            anti_phase_result = analyze_wav(anti_phase_path)
            self.assertEqual(anti_phase_result.pitch_note, "A4")

    def test_demo_wav_extracts_all_required_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = generate_demo_wav(Path(directory) / "demo.wav", seconds=6, bpm=120)
            result = analyze_wav(path)
            self.assertEqual(result.sample_rate, 22_050)
            self.assertAlmostEqual(result.duration, 6.0, places=1)
            self.assertGreater(result.tempo, 110)
            self.assertLess(result.tempo, 130)
            self.assertNotEqual(result.key_name, "未知")
            self.assertTrue(result.classification)
            self.assertIn("energy", result.feature_data)
            self.assertIn("spectrum", result.feature_data)
            self.assertTrue(result.feature_data["classification_reasons"])

    def test_invalid_wav_gets_readable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.wav"
            path.write_bytes(b"not a wave file")
            with self.assertRaisesRegex(AudioAnalysisError, "有效的 PCM WAV"):
                analyze_wav(path)

    def test_silence_is_classified_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "silence.wav"
            with wave.open(str(path), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(8_000)
                writer.writeframes(b"\x00\x00" * 8_000)
            result = analyze_wav(path)
            self.assertEqual(result.tempo, 0)
            self.assertEqual(result.pitch_note, "未知")
            self.assertEqual(result.classification, "静音或近静音")


if __name__ == "__main__":
    unittest.main()
