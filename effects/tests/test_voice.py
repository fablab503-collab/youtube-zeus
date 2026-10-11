"""voice.py: the voice step without model weights (synthetic signals, fake Demucs / Chatterbox workers).

Run:  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m unittest discover -s effects/tests -p 'test_voice.py' -v
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

import synth
from zfx import voice as V
from zfx.jobs import Job
from zfx.review import approve

SR = 44100


def tone(freq, seconds, sr=SR, amp=0.5, harmonics=1):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * sum(np.sin(2 * np.pi * freq * k * t) / k for k in range(1, harmonics + 1))).astype(np.float32)


def speechy(seconds, f0=150.0, sr=SR, seed=0):
    """Harmonic 'syllables' (4 per second) with pauses: voiced, pitched, with gaps like speech."""
    t = np.arange(int(seconds * sr)) / sr
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 12))
    env = np.clip(np.sin(2 * np.pi * 2.0 * t), 0, None) ** 0.7
    rng = np.random.default_rng(seed)
    return (0.3 * x * env + 0.002 * rng.standard_normal(len(t))).astype(np.float32)


def noise(seconds, sr=SR, seed=0, tilt_db_per_octave=0.0):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(seconds * sr))
    if tilt_db_per_octave:
        x = V.apply_response(x, sr, lambda f: (np.maximum(f, 20.0) / 1000.0) ** (tilt_db_per_octave / 6.02))
    x = x / np.max(np.abs(x)) * 0.3
    return x.astype(np.float32)


class LoudnessTests(unittest.TestCase):
    def test_997hz_sine_full_scale_is_minus_3_lufs(self):
        # BS.1770: a 0 dBFS 997 Hz sine in one channel reads -3.01 LKFS
        self.assertAlmostEqual(V.integrated_loudness(tone(997, 5, 48000, 1.0), 48000), -3.01, delta=0.05)

    def test_matches_ffmpeg_ebur128(self):
        x = np.stack([noise(4, seed=1, tilt_db_per_octave=-3), noise(4, seed=2)]) * 0.5
        with tempfile.TemporaryDirectory() as d:
            p = V.write_wav(Path(d) / "n.wav", x, SR)
            out = subprocess.run([synth.ffmpeg(), "-hide_banner", "-nostats", "-i", str(p), "-af", "ebur128",
                                  "-f", "null", "-"], capture_output=True, text=True).stderr
        ref = float(out.split("Summary:")[1].split("I:")[1].split("LUFS")[0])
        self.assertAlmostEqual(V.integrated_loudness(x, SR), ref, delta=0.2)

    def test_loudness_match_on_tones(self):
        quiet, loud = tone(220, 3, amp=0.05, harmonics=5), tone(1500, 3, amp=0.6)
        target = V.integrated_loudness(loud, SR)
        y, gain = V.match_loudness(quiet, SR, target)
        self.assertGreater(gain, 10)
        self.assertAlmostEqual(V.integrated_loudness(y, SR), target, delta=0.1)
        silent, g = V.match_loudness(np.zeros(SR * 2, np.float32), SR, target)
        self.assertEqual(g, 0.0)
        self.assertFalse(np.any(silent))

    def test_gating_ignores_silence(self):
        x = np.concatenate([tone(1000, 2, amp=0.3), np.zeros(SR * 4, np.float32)])
        alone = V.integrated_loudness(tone(1000, 2, amp=0.3), SR)
        # 4 s of silence would pull an ungated mean down by 10*log10(3) = 4.8 dB; the gates drop it (only the
        # blocks straddling the end of the tone remain, -0.3 dB, as in BS.1770)
        self.assertAlmostEqual(V.integrated_loudness(x, SR), alone, delta=0.5)


class EqTests(unittest.TestCase):
    def test_eq_match_moves_the_spectrum_toward_the_target(self):
        src = noise(4, seed=3)                                   # flat (white)
        target = noise(4, seed=4, tilt_db_per_octave=-2)         # darker, like a different room/mic
        before = V.spectral_distance(src, target, SR)
        eq = V.eq_match(src, target, SR)
        out = V.apply_eq(src, SR, eq["centers_hz"], eq["gains_db"])
        after = V.spectral_distance(out, target, SR)
        self.assertGreater(before, 5.0)
        self.assertLess(after, before * 0.4)
        lows, highs = eq["gains_db"][3], eq["gains_db"][8]    # 250 Hz up, 8 kHz down
        self.assertGreater(lows, highs)
        self.assertTrue(all(abs(g) <= eq["max_db"] for g in eq["gains_db"]))

    def test_no_boost_where_the_voice_has_nothing(self):
        # band-limited 'converted voice' (24 kHz model: nothing above 12 kHz) vs a full-band original
        src = V.apply_response(noise(3, seed=5), SR, lambda f: (f < 11000).astype(float))
        eq = V.eq_match(src, noise(3, seed=6), SR)
        self.assertEqual(eq["gains_db"][-1], 0.0)            # 16 kHz band left alone
        self.assertFalse(eq["valid"][-1])

    def test_eq_and_highpass_keep_length_and_timing(self):
        x = np.zeros(SR, np.float32)
        x[SR // 2] = 1.0
        y = V.apply_eq(x, SR, V.OCTAVE_CENTERS, [0, 0, 3, 3, 0, -3, -3, 0, 0, 0])
        self.assertEqual(len(y), len(x))
        self.assertEqual(int(np.argmax(np.abs(y))), SR // 2)  # zero phase: the impulse stays where it was
        h = V.highpass(tone(30, 1) + tone(1000, 1), SR, 70.0)
        self.assertEqual(len(h), SR)
        f, p = V.ltas(h, SR)
        self.assertLess(p[np.argmin(np.abs(f - 30))], p[np.argmin(np.abs(f - 1000))] / 30)


class DeessAndAnalysisTests(unittest.TestCase):
    def test_deess_is_transparent_when_not_needed(self):
        x = speechy(2)
        y, info = V.deess(x, SR, mode="on")
        self.assertEqual(len(y), len(x))
        self.assertLess(np.max(np.abs(y - x)), 1e-3)          # nothing above the threshold: unity
        y2, info2 = V.deess(x, SR, mode="auto", reference=x)
        self.assertFalse(info2["applied"])
        self.assertTrue(np.array_equal(y2, x))

    def test_deess_tames_hiss(self):
        voice = speechy(2)
        hiss = V.apply_response(noise(2, seed=7), SR, lambda f: ((f > 5000) & (f < 9000)).astype(float))
        env = (np.arange(len(voice)) // (SR // 8)) % 2 == 0   # bursts of 's'
        hissy = voice + 0.6 * hiss * env
        y, info = V.deess(hissy, SR, mode="auto", reference=voice)
        self.assertTrue(info["applied"])
        band = lambda s: V.ltas(s, SR)[1][(V.ltas(s, SR)[0] > 5000) & (V.ltas(s, SR)[0] < 9000)].sum()  # noqa: E731
        self.assertLess(band(y), band(hissy) * 0.8)
        self.assertEqual(len(y), len(hissy))

    def test_f0_of_a_harmonic_tone(self):
        st = V.f0_stats(tone(120, 1.5, harmonics=8), SR)
        self.assertAlmostEqual(st["median_hz"], 120.0, delta=1.5)
        self.assertAlmostEqual(V.f0_stats(speechy(2, f0=210), SR)["median_hz"], 210.0, delta=3)

    def test_trim_silence_keeps_only_speech(self):
        x = np.concatenate([np.zeros(SR), speechy(1.0), np.zeros(SR * 2), speechy(1.0, seed=2), np.zeros(SR)])
        y, segs, speech = V.trim_silence(V.mono(x), SR)
        self.assertEqual(len(segs), 2)
        self.assertLess(len(y), len(x) * 0.6)
        self.assertGreater(speech, 1.0)

    def test_estimate_lag_finds_a_delay_between_different_voices(self):
        a = speechy(3, f0=110)
        b = np.concatenate([np.zeros(int(0.05 * SR), np.float32), speechy(3, f0=200, seed=9)])[: len(a)]
        r = V.estimate_lag(a, b, SR)
        self.assertAlmostEqual(r["lag_ms"], 50.0, delta=6)
        self.assertGreater(r["corr"], 0.8)
        self.assertAlmostEqual(V.estimate_lag(a, a, SR)["lag_ms"], 0.0, delta=1)

    def test_word_error_rate(self):
        self.assertEqual(V.word_error_rate("It's too far, too far.", "it's too far too far"), 0.0)
        self.assertAlmostEqual(V.word_error_rate("you don't see it", "you do see it"), 0.25)


class RemixTests(unittest.TestCase):
    def test_fit_length_pads_or_cuts_at_the_end_only(self):
        x = np.arange(10, dtype=np.float32)
        self.assertTrue(np.array_equal(V.fit_length(x, 6), x[:6]))
        y = V.fit_length(np.stack([x, x]), 12)
        self.assertEqual(y.shape, (2, 12))
        self.assertTrue(np.array_equal(y[:, :10], np.stack([x, x])))
        self.assertFalse(np.any(y[:, 10:]))

    def test_pan_like_keeps_the_stereo_image(self):
        v = speechy(1)
        orig = np.stack([v * 0.8, v * 0.4])
        p = V.pan_like(v, orig)
        self.assertEqual(p.shape, orig.shape)
        self.assertAlmostEqual(float(np.std(p[0]) / np.std(p[1])), 2.0, places=2)

    def test_aac_round_trip_is_sample_accurate(self):
        n = int(6.0137 * SR)
        x = np.stack([speechy(n / SR), speechy(n / SR, f0=180)])[:, :n]
        x[:, 5000:5004] = 0.9
        with tempfile.TemporaryDirectory() as d:
            w = V.write_wav(Path(d) / "m.wav", x, SR)
            V.encode_aac(w, Path(d) / "m.m4a", SR, 2, 192000, "c")
            y, _ = V.read_audio(Path(d) / "m.m4a", sr=SR, channels=2)
            self.assertEqual(V.probe_audio(Path(d) / "m.m4a")["comment"], "c")
        self.assertEqual(y.shape[1], n)
        self.assertEqual(V.sample_offset(x, y), 0)


# ----------------------------------------------------------------------------- whole step with fake workers
def _ff(*args):
    subprocess.run([synth.ffmpeg(), "-y", "-v", "error", *map(str, args)], check=True)


def make_job(home: Path, seconds: float = 2.0) -> Job:
    job = Job.create("/tmp/in.mp4", 0.0, seconds, "depth")
    src = job.dir / "source.mp4"
    with tempfile.TemporaryDirectory() as d:
        w = V.write_wav(Path(d) / "a.wav", np.stack([speechy(seconds), speechy(seconds)]) * 0.8
                        + 0.05 * np.stack([tone(330, seconds), tone(330, seconds)]), SR)
        _ff("-f", "lavfi", "-i", f"testsrc=size=320x240:rate=30:duration={seconds}", "-i", w,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-shortest", src)
    _ff("-i", src, "-vn", "-c:a", "copy", job.dir / "audio.m4a")
    _ff("-i", src, "-c", "copy", "-metadata", "comment=AI-generated with YouTube Zeus Effects (faceswap)",
        job.dir / "final.mp4")
    job.set_artifact("source", src, save=False)
    job.set_artifact("audio", job.dir / "audio.m4a", save=False)
    for name, data in {"work": b"work", "depth": b"depth", "mask": b"mask", "control": b"control",
                       "preview": b"jpg"}.items():
        job.path(name).write_bytes(data)
        job.set_artifact(name, job.path(name), save=False)
    job["has_audio"] = True
    job["status"] = "prepared"
    job.save()
    approve(job)
    job.reload()
    job.set_artifact("final", job.dir / "final.mp4")
    return job


def fake_worker(module, task, task_dir, log_path, report, stage, lo=0.0, hi=1.0, timeout=0):
    """Demucs: vocals = 0.7 x mix, accompaniment = 0.3 x mix. Chatterbox: the voice an octave up (new 'speaker'),
    same envelope and length."""
    if module == "vc_demucs":
        items = []
        for it in task["items"]:
            x, sr = V.read_audio(it["input"])
            out = Path(it["out_dir"])
            V.write_wav(out / "vocals.wav", 0.7 * x, sr)
            V.write_wav(out / "accompaniment.wav", 0.3 * x, sr)
            items.append({"input": it["input"], "device": "cpu", "samples": x.shape[1], "residual_db": -120.0})
        return {"model": "fake", "items": items}
    x, sr = V.read_audio(task["input"], sr=task["sr"], channels=1)
    env = np.abs(x[0])
    y = (np.convolve(env, np.ones(200) / 200, mode="same") * np.sin(2 * np.pi * 300 * np.arange(x.shape[1]) / sr))
    V.write_wav(task["out"], y.astype(np.float32), sr)
    return {"engine": "chatterbox", "device": "cpu", "fake": True}


class VoiceStepTests(unittest.TestCase):
    def setUp(self):
        self.home_ctx = synth.TempHome()
        self.home = self.home_ctx.__enter__()
        self.ref = V.write_wav(self.home / "daniel.wav",
                               np.concatenate([np.zeros(SR // 2), speechy(3.0, f0=115), np.zeros(SR // 2)]), SR)

    def tearDown(self):
        self.home_ctx.__exit__(None, None, None)

    def args(self, **kw):
        base = dict(voice=[str(self.ref)], consent=True, engine="chatterbox", keep_music=True, grade="match",
                    deess="auto", check="off", device="cpu", steps=None, seed=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_refuses_without_consent_before_reading_anything(self):
        with mock.patch.object(V, "run_worker") as worker:
            with self.assertRaises(V.VoiceError) as ctx:
                V.run_voice(self.args(consent=False), "/nonexistent/job")
        self.assertIn("consent", str(ctx.exception))
        self.assertIn(V.CONSENT_TEXT, str(ctx.exception))
        worker.assert_not_called()

    def test_refuses_unapproved_job(self):
        job = Job.create("/tmp/in.mp4", 0.0, 1.0, "depth")
        with self.assertRaises(V.VoiceError) as ctx:
            V.run_voice(self.args(), job.dir)
        self.assertIn("not approved", str(ctx.exception))

    def test_refuses_missing_voice_file(self):
        job = make_job(self.home)
        with mock.patch.object(V, "voice_status", return_value={"ready": True}):
            with self.assertRaises(V.VoiceError):
                V.run_voice(self.args(voice=[str(self.home / "nope.wav")]), job.dir)

    def test_whole_step_with_fake_engines(self):
        job = make_job(self.home)
        orig, sr = V.read_audio(job.dir / "audio.m4a")
        with mock.patch.object(V, "run_worker", side_effect=fake_worker), \
                mock.patch.object(V, "voice_status", return_value={"ready": True}):
            result = V.run_voice(self.args(), job.dir)
        jd = job.dir
        for f in ("stems/original.wav", "stems/vocals.wav", "stems/accompaniment.wav",
                  "stems/vocals-converted-raw.wav", "stems/vocals-converted.wav", "stems/mix-voice.wav",
                  "voice-refs/reference.wav", "voice-refs/ref-1-clean.wav", "audio-voice.m4a", "final-voice.mp4"):
            self.assertTrue((jd / f).is_file(), f)
        # remix = graded voice + original accompaniment, sample for sample, same length as the original
        voice, _ = V.read_audio(jd / "stems/vocals-converted.wav")
        accomp, _ = V.read_audio(jd / "stems/accompaniment.wav")
        mix, _ = V.read_audio(jd / "stems/mix-voice.wav")
        self.assertEqual(mix.shape, orig.shape)
        self.assertLess(np.max(np.abs(mix - (voice + accomp))), 1e-6)
        # loudness of the new voice = the original voice's
        vocals, _ = V.read_audio(jd / "stems/vocals.wav")
        self.assertAlmostEqual(V.integrated_loudness(voice, sr), V.integrated_loudness(vocals, sr), delta=0.15)
        # the new audio decodes to exactly the original's samples, aligned
        new, _ = V.read_audio(jd / "audio-voice.m4a", sr=sr, channels=orig.shape[0])
        self.assertEqual(new.shape, orig.shape)
        self.assertEqual(V.sample_offset(mix, new), 0)
        # video stream-copied from final.mp4 (same packets, same start), AI comments kept and added
        def md5(p):
            return subprocess.run([synth.ffmpeg(), "-v", "error", "-i", str(p), "-map", "0:v:0", "-c", "copy",
                                   "-f", "md5", "-"], capture_output=True, text=True).stdout
        self.assertEqual(md5(jd / "final-voice.mp4"), md5(jd / "final.mp4"))
        s_new, s_old = V._streams(jd / "final-voice.mp4"), V._streams(jd / "final.mp4")
        self.assertAlmostEqual(s_new["video"]["start"], s_old["video"]["start"], places=4)
        self.assertIn("AI-generated voice with YouTube Zeus Effects (chatterbox)", s_new["comment"])
        self.assertIn("(faceswap)", s_new["comment"])
        # job.json
        data = json.loads((jd / "job.json").read_text())
        rec = data["voices"][-1]
        self.assertTrue(rec["consent"])
        self.assertEqual(rec["consent_text"], V.CONSENT_TEXT)
        self.assertTrue(rec["mix"]["same_length"])
        self.assertFalse(rec["mix"]["time_stretch"])
        self.assertEqual(rec["encode"]["offset_samples"], 0)
        self.assertEqual(data["artifacts"]["final_voice"], "final-voice.mp4")
        self.assertEqual(rec["voices"][0]["sha256"], V.sha256_file(self.ref))
        self.assertTrue(any("Short voice sample" in w for w in rec["warnings"]))
        self.assertEqual(result["final_voice"], str(jd / "final-voice.mp4"))
        self.assertAlmostEqual(rec["checks"]["f0"]["reference"]["median_hz"], 115, delta=3)

    def test_without_music_and_without_grade(self):
        job = make_job(self.home)
        with mock.patch.object(V, "run_worker", side_effect=fake_worker), \
                mock.patch.object(V, "voice_status", return_value={"ready": True}):
            V.run_voice(self.args(keep_music=False, grade="none"), job.dir)
        voice, _ = V.read_audio(job.dir / "stems/vocals-converted.wav")
        mix, _ = V.read_audio(job.dir / "stems/mix-voice.wav")
        self.assertLess(np.max(np.abs(mix - voice)), 1e-6)
        rec = json.loads((job.dir / "job.json").read_text())["voices"][-1]
        self.assertEqual(rec["grade"], {"mode": "none"})
        self.assertFalse(rec["options"]["keep_music"])


class CliTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(synth.EFFECTS))
        import zeus_effects
        self.cli = zeus_effects

    def test_arguments_and_defaults(self):
        a = self.cli.build_parser().parse_args(["voice", "--job", "J", "--voice", "a.wav", "--voice", "b.MOV",
                                                "--consent"])
        self.assertEqual((a.command, a.job, a.voice, a.consent), ("voice", "J", ["a.wav", "b.MOV"], True))
        self.assertEqual((a.keep_music, a.grade, a.deess, a.check, a.engine, a.device, a.steps, a.seed),
                         (True, "match", "auto", "auto", "chatterbox", "auto", None, None))
        b = self.cli.build_parser().parse_args(["voice", "--job", "J", "--voice", "a.wav", "--no-keep-music",
                                                "--grade", "none", "--deess", "off", "--check", "on",
                                                "--device", "cpu", "--steps", "6", "--seed", "7"])
        self.assertEqual((b.keep_music, b.grade, b.deess, b.check, b.device, b.steps, b.seed, b.consent),
                         (False, "none", "off", "on", "cpu", 6, 7, False))
        self.assertIs(self.cli.COMMANDS["voice"], self.cli.cmd_voice)
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                self.cli.build_parser().parse_args(["voice", "--job", "J", "--grade", "loud"])
            with self.assertRaises(SystemExit):
                self.cli.build_parser().parse_args(["voice", "--voice", "a.wav"])     # --job is required

    def test_cli_refuses_without_consent(self):
        proc = subprocess.run([sys.executable, str(synth.CLI), "voice", "--job", "/nonexistent", "--voice", "a.wav"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        last = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(last["stage"], "consent")
        self.assertIn("--consent", last["error"])


if __name__ == "__main__":
    unittest.main()
