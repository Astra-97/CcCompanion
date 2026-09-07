#!/usr/bin/env python3
"""Tests for voice_acoustics: pure analysis core + ffmpeg integration."""

from __future__ import annotations

import shutil
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import voice_acoustics


SR = 16000


def sine(freq: float, seconds: float, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return amp * np.sin(2 * np.pi * freq * t)


class CountSpeechCharsTest(unittest.TestCase):
    def test_counts_cjk_and_alnum_only(self) -> None:
        self.assertEqual(voice_acoustics.count_speech_chars("今天真的太开心啦，哈哈！"), 10)
        self.assertEqual(voice_acoustics.count_speech_chars("hello world 123"), 13)
        self.assertEqual(voice_acoustics.count_speech_chars(""), 0)
        self.assertEqual(voice_acoustics.count_speech_chars(None), 0)


class PitchLabelTest(unittest.TestCase):
    def test_buckets_against_adult_female_range(self) -> None:
        self.assertEqual(voice_acoustics.pitch_label(120.0), "偏低")
        self.assertEqual(voice_acoustics.pitch_label(165.0), "正常")
        self.assertEqual(voice_acoustics.pitch_label(210.0), "正常")
        self.assertEqual(voice_acoustics.pitch_label(255.0), "正常")
        self.assertEqual(voice_acoustics.pitch_label(300.0), "偏高")
        self.assertEqual(voice_acoustics.pitch_label(0.0), "未知")


class AnalyzeSamplesTest(unittest.TestCase):
    def test_detects_normal_pitch_and_pause(self) -> None:
        # 1s 发声 + 0.8s 停顿 + 1.5s 发声，基频 200Hz。
        signal = np.concatenate([
            sine(200.0, 1.0),
            np.zeros(int(SR * 0.8)),
            sine(200.0, 1.5),
        ])
        result = voice_acoustics.analyze_samples(signal, SR, "今天真的太开心啦哈哈")
        self.assertAlmostEqual(result["pitch_hz"], 200.0, delta=2.0)
        self.assertEqual(result["pitch_label"], "正常")
        self.assertEqual(result["pauses"], 1)
        self.assertEqual(result["speech_chars"], 10)
        self.assertGreater(result["voiced_sec"], 2.0)
        self.assertLess(result["voiced_sec"], 3.0)
        self.assertAlmostEqual(result["speech_rate_cps"], round(10 / result["voiced_sec"], 1))

    def test_low_pitch_labelled(self) -> None:
        result = voice_acoustics.analyze_samples(sine(120.0, 2.0), SR)
        self.assertAlmostEqual(result["pitch_hz"], 120.0, delta=2.0)
        self.assertEqual(result["pitch_label"], "偏低")

    def test_high_pitch_labelled(self) -> None:
        result = voice_acoustics.analyze_samples(sine(300.0, 2.0), SR)
        self.assertAlmostEqual(result["pitch_hz"], 300.0, delta=3.0)
        self.assertEqual(result["pitch_label"], "偏高")

    def test_short_pause_not_counted(self) -> None:
        signal = np.concatenate([
            sine(200.0, 1.0),
            np.zeros(int(SR * 0.3)),  # 低于 0.5s 阈值
            sine(200.0, 1.0),
        ])
        result = voice_acoustics.analyze_samples(signal, SR)
        self.assertEqual(result["pauses"], 0)

    def test_leading_and_trailing_silence_not_counted(self) -> None:
        signal = np.concatenate([
            np.zeros(int(SR * 1.0)),
            sine(200.0, 1.0),
            np.zeros(int(SR * 1.0)),
        ])
        result = voice_acoustics.analyze_samples(signal, SR)
        self.assertEqual(result["pauses"], 0)

    def test_silent_audio_raises(self) -> None:
        with self.assertRaises(voice_acoustics.VoiceAcousticsError):
            voice_acoustics.analyze_samples(np.zeros(SR * 2), SR)

    def test_too_short_audio_raises(self) -> None:
        with self.assertRaises(voice_acoustics.VoiceAcousticsError):
            voice_acoustics.analyze_samples(sine(200.0, 0.01), SR)

    def test_no_transcript_means_no_rate(self) -> None:
        result = voice_acoustics.analyze_samples(sine(200.0, 1.0), SR)
        self.assertEqual(result["speech_rate_cps"], 0.0)
        self.assertNotIn("语速", voice_acoustics.format_acoustics_summary(result))


class FormatSummaryTest(unittest.TestCase):
    def test_xhs_ear_style_summary(self) -> None:
        summary = voice_acoustics.format_acoustics_summary({
            "pitch_label": "偏高", "speech_rate_cps": 2.6, "pauses": 0,
        })
        self.assertEqual(summary, "音高偏高，语速2.6字/秒，停顿0次")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class FfmpegIntegrationTest(unittest.TestCase):
    def test_analyze_real_encoded_file(self) -> None:
        import subprocess

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wav_path = Path(tmp.name) / "tone.wav"
        m4a_path = Path(tmp.name) / "tone.m4a"
        samples = (sine(200.0, 2.0) * 32767).astype("<i2")
        import wave

        with wave.open(str(wav_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SR)
            handle.writeframes(samples.tobytes())
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(wav_path), str(m4a_path)],
            check=True,
        )
        result = voice_acoustics.analyze_voice_acoustics(m4a_path, "你好呀")
        self.assertAlmostEqual(result["pitch_hz"], 200.0, delta=4.0)
        self.assertEqual(result["pitch_label"], "正常")
        self.assertGreater(result["speech_rate_cps"], 0)
        self.assertIn("音高正常", result["summary"])

    def test_undecodable_file_raises(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bogus = Path(tmp.name) / "bogus.m4a"
        bogus.write_bytes(b"not-audio")
        with self.assertRaises(voice_acoustics.VoiceAcousticsError):
            voice_acoustics.analyze_voice_acoustics(bogus)


if __name__ == "__main__":
    unittest.main()
