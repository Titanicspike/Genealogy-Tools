"""Collapse runaway repetition in OCR output.

Vision OCR models sometimes get stuck emitting the same few characters until
they hit their token limit ("燒喜 | 燒喜 | 燒喜 | ..." for ~28,000 characters,
or "\\n一" four thousand times). All three engines here do it now and then.

HunyuanOCR ships a fix, clean_repeated_substrings() in its repository's
HunyuanOCR_v1.0/infer/utils.py, but it only looks at outputs of 8,000+
characters and only trims a loop at the very end of the text. This version
handles any length and any position, so it also catches the shorter loops
PaddleOCR-VL and TongGuOCR produce, and loops that end before the text does.

A run of the same unit repeated back to back is cut to one copy. That never
loses searchable text: every character in the run is still there once.
"""

import re

# Longest repeating unit to look for, in characters. A whole table row the
# model keeps re-emitting fits comfortably.
MAX_UNIT = 300
# Back-to-back copies needed before a run counts as a loop. Short units need
# many copies, since short repeats occur legitimately (e.g. "世世", or a few
# "(世)" table headers); a long unit repeated verbatim several times does not.
MIN_SPAN = 40  # characters the run must cover in total
MIN_COPIES = 4

_REPEAT = re.compile(r"(.{1,%d}?)\1{%d,}" % (MAX_UNIT, MIN_COPIES - 1), re.DOTALL)


def collapse_repetitions(text: str | None) -> str | None:
    """text with every runaway repeat cut to a single copy of its unit."""
    if not text:
        return text

    def shorten(match: re.Match) -> str:
        run, unit = match.group(0), match.group(1)
        if len(run) < MIN_SPAN:
            return run
        return unit

    # One pass can leave a loop of loops (a repeated block whose copies each
    # held their own short loop), so repeat until nothing changes.
    while True:
        cleaned = _REPEAT.sub(shorten, text)
        if cleaned == text:
            return cleaned
        text = cleaned
