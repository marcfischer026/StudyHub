import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest import mock  # noqa: E402

from universe.services import transcriber  # noqa: E402
from universe.services.transcriber import Segment, collapse_repeats  # noqa: E402


def segs(*texts: str, step: float = 0.5) -> list[Segment]:
    return [Segment(i * step, i * step + step, t) for i, t in enumerate(texts)]


class CollapseRepeatsTest(unittest.TestCase):
    def test_hallucination_loop_folds_into_one_line(self):
        loop = "um die Umsatz-Vertualität,"
        out = collapse_repeats(segs("50 Prozent,", *[loop] * 20, "Weiter geht's."))
        self.assertEqual([s.text for s in out], ["50 Prozent,", loop, "Weiter geht's."])
        self.assertEqual((out[1].start, out[1].end), (0.5, 10.5))

    def test_punctuation_and_case_do_not_break_the_run(self):
        out = collapse_repeats(segs("Genau.", "genau", "Genau!", "genau,"))
        self.assertEqual(len(out), 1)

    def test_drifting_loop_is_still_one_loop(self):
        out = collapse_repeats(segs("um die Umsatz-Volatilität,", "um die Umsatz-Volatilität,", "die Umsatz-Volatilität,", "um die Umsatz-Volatilität,"))
        self.assertEqual(len(out), 1)

    def test_different_short_lines_are_not_a_loop(self):
        out = collapse_repeats(segs("Ja.", "Nein.", "Ja."))
        self.assertEqual(len(out), 3)

    def test_real_repeats_are_kept(self):
        out = collapse_repeats(segs("Ja, ich weiß,", "ich weiß,", "ich weiß,", "ich muss halt schauen,"))
        self.assertEqual(len(out), 4)
        out = collapse_repeats(segs("auf meine,", "auf meine,", "der Gewinn auf meinen Umsatz,"))
        self.assertEqual(len(out), 3)

    def test_same_line_far_apart_is_not_a_loop(self):
        out = collapse_repeats([Segment(t, t + 1, "Okay.") for t in (0, 30, 60, 90)])
        self.assertEqual(len(out), 4)


class RedoLoopsTest(unittest.TestCase):
    def test_stuck_stretch_is_transcribed_again_without_context(self):
        first = segs("50 Prozent,", *["um die Umsatz-Volatilität,"] * 10, "Danke.")
        redo = [Segment(0.6, 2.0, "um die Umsatz-Volatilität,"), Segment(2.0, 5.0, "die wir vorher berechnet haben.")]
        with mock.patch.object(transcriber, "_run_whisper", return_value=redo) as run:
            out = transcriber._redo_loops(first, Path("x.wav"), Path("/tmp"), mock.Mock(), "de", "", None)
        extra = run.call_args.kwargs["extra"]
        self.assertEqual(extra[:2], ["-mc", "0"])
        self.assertEqual(extra[3], "500")  # -ot: where the loop started, in ms
        self.assertEqual([s.text for s in out], ["50 Prozent,", *[s.text for s in redo], "Danke."])

    def test_relative_times_from_older_whisper_builds_are_shifted(self):
        first = [Segment(100, 101, "Hallo."), *[Segment(101 + i, 102 + i, "so ist das,") for i in range(5)]]
        with mock.patch.object(transcriber, "_run_whisper", return_value=[Segment(0, 3, "so, das wars.")]):
            out = transcriber._redo_loops(first, Path("x.wav"), Path("/tmp"), mock.Mock(), "de", "", None)
        self.assertEqual((out[1].start, out[1].text), (101, "so, das wars."))

    def test_whisper_failure_on_redo_still_folds_the_loop(self):
        first = segs("a", "b", "b", "b", "b", "c")
        with mock.patch.object(transcriber, "_run_whisper", side_effect=transcriber.TranscriptionError("x")):
            out = transcriber._redo_loops(first, Path("x.wav"), Path("/tmp"), mock.Mock(), "de", "", None)
        self.assertEqual([s.text for s in out], ["a", "b", "c"])


class HallucinationFilterTest(unittest.TestCase):
    def test_subtitle_credits_are_dropped_but_real_thanks_kept(self):
        drop = ["Untertitel im Auftrag des ZDF, 2021", "Untertitel der Amara.org-Community", "Vielen Dank fürs Zuschauen!"]
        keep = ["Danke.", "Vielen Dank.", "Die Untertitel stehen im Skript."]
        for text in drop:
            self.assertTrue(transcriber._HALLUCINATION_RE.match(text), text)
        for text in keep:
            self.assertFalse(transcriber._HALLUCINATION_RE.match(text), text)


if __name__ == "__main__":
    unittest.main()
