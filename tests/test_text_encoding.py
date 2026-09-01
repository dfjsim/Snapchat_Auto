"""Text this tool writes, and text it sends between its own processes, is UTF-8 — never the locale's.

Windows examiner workstations run cp1252, and a case folder is not restricted to one codepage. Three
faults in shipped code came out of that, all of them silent:

* `open(path, 'w')` with no encoding writes in cp1252, so a Memory caption holding an emoji raised
  UnicodeEncodeError and cost the whole legacy report. The content is device data — it is not ours
  to restrict to one codepage.
* the poster worker's pipes carry **file paths**, and under cp1252 with `errors="replace"` a path
  holding a character that codepage cannot represent reached the worker as "?", so it was asked to
  open a file that does not exist and the video came back undecodable: a claim about the evidence,
  made from a mangled string.
* and `cv2.imwrite` goes through the ANSI API on Windows, so even a correctly delivered path could
  not be written to — it returns False and writes nothing.

Every input here is synthetic. No extraction data is required or used.
"""
import ast
import pathlib
import subprocess
import sys

import pytest

from scripts.data import poster_worker

REPO = pathlib.Path(__file__).resolve().parent.parent
# A directory name with a character cp1252 has no mapping for. "ł" is ordinary in a Polish name, and
# an examiner's case folder is not restricted to one codepage either.
BEYOND_CP1252 = "case łódź"


def _shipped_modules():
    yield REPO / "Snapchat_Auto.py"
    for folder in ("scripts", "packages"):
        for path in sorted((REPO / folder).rglob("*.py")):
            if "__pycache__" not in str(path):
                yield path


def _text_writes_without_encoding(path):
    """(line, mode) for every `open(..., 'w'/'a'/'x')` in *path* that does not name an encoding."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open"):
            continue
        mode = ""
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        for keyword in node.keywords:
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                mode = str(keyword.value.value)
        if "b" in mode or not any(c in mode for c in "wax"):
            continue
        if "encoding" not in {k.arg for k in node.keywords}:
            found.append((node.lineno, mode))
    return found


def test_nothing_shipped_writes_text_in_whatever_the_locale_happens_to_be():
    """The guard for the whole class. It is what found the legacy Memories report, which is how one
    emoji caption could take that report down on a cp1252 machine and nowhere else."""
    offenders = [f"{path.relative_to(REPO)}:{line} (mode {mode!r})"
                 for path in _shipped_modules()
                 for line, mode in _text_writes_without_encoding(path)]

    assert not offenders, "text written in the locale encoding: " + ", ".join(offenders)


@pytest.mark.parametrize("module, marker", [
    ("scripts/DecryptLocalMemories_iOS.py", "LocalMemories_legacy_report.html"),
    ("scripts/ParseSnapchat_iOS.py", "Communications_legacy_report.html"),
])
def test_a_report_written_as_utf8_says_so_in_the_document(module, marker):
    """Writing UTF-8 without declaring it only trades a crash for mojibake: with no charset the
    browser guesses, and on Windows it guesses windows-1252 often enough to matter. (A source check —
    building either legacy document needs the whole parser.)"""
    source = (REPO / module).read_text(encoding="utf-8")

    assert marker in source
    assert '<meta charset="utf-8">' in source


# --------------------------------------------------------------- the paths sent to the worker

FAKE_WORKER = """
import sys
for stream in (sys.stdin, sys.stdout):
    stream.reconfigure(encoding="utf-8", errors="replace")
seen = open(sys.argv[1], "w", encoding="utf-8")
for line in sys.stdin:
    src = line.rstrip().split(chr(9))[0]
    print(src, file=seen, flush=True)
    print("START " + src, flush=True)
    print("OK " + src, flush=True)
"""


def test_a_path_the_locale_cannot_encode_reaches_the_worker_intact(monkeypatch, tmp_path):
    """Through the REAL _spawn, so it is the shipped Popen arguments under test."""
    worker = tmp_path / "fake_worker.py"
    worker.write_text(FAKE_WORKER, encoding="utf-8")
    seen = tmp_path / "seen.txt"
    # _worker_command returns (command, cwd)
    monkeypatch.setattr(poster_worker, "_worker_command",
                        lambda: ([sys.executable, str(worker), str(seen)], None))
    video = tmp_path / BEYOND_CP1252 / "clip.mp4"
    video.parent.mkdir()
    video.write_bytes(b"\x00" * 16)

    results, _stderr = poster_worker.run_jobs([(str(video), str(video) + ".jpg", False)],
                                              file_timeout=10, budget=30)

    assert results == {str(video): True}
    assert seen.read_text(encoding="utf-8").strip() == str(video), \
        "the worker was told to open a different path than the caller named"


def test_the_worker_opens_the_file_it_was_told_to_open(tmp_path):
    """The other end of the same agreement, and the only assertion that can catch it.

    An echo test cannot: cp1252 mangling is byte-symmetric, so a path the child decoded wrongly comes
    back through the parent's utf-8 decode looking perfect. What does not survive is the child
    actually FINDING the file — so this gives it a real video and asks for a real frame.
    """
    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    folder = tmp_path / BEYOND_CP1252
    folder.mkdir()
    video, poster = folder / "clip.mp4", folder / "clip_poster.jpg"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (32, 32))
    for n in range(10):
        writer.write(numpy.full((32, 32, 3), n * 20, dtype=numpy.uint8))
    writer.release()
    if not video.exists() or not video.stat().st_size:
        pytest.skip("no mp4 encoder available here")

    job = str(video) + "\t" + str(poster) + "\t1\n"
    proc = subprocess.Popen([sys.executable, "-m", "scripts.data.poster_worker"], cwd=str(REPO),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, encoding="utf-8", errors="replace")
    try:
        proc.stdin.write(job)
        proc.stdin.flush()
        started = proc.stdout.readline().strip()
        answer = proc.stdout.readline().strip()
    finally:
        proc.kill()

    if started.startswith("FATAL "):
        pytest.skip(f"the worker could not start here: {started}")
    assert answer.startswith("OK "), f"the worker never opened the file it was sent: {answer!r}"
    assert poster.exists() and poster.stat().st_size, \
        "no poster frame was written — cv2.imwrite cannot write a non-ASCII path on Windows"
