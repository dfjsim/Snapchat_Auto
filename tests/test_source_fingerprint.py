"""Fingerprinting what a run read, and deciding whether a later run may trust or reuse it.

This is the check that stands behind a partial report's claim to have been built from the same
evidence as the report the examiner ticked. Two things it must never do: report "verified" when it had
nothing to compare, and allow reuse across builds — a newer Snapchat_Auto may extract more paths or
decrypt media an older one could not, and inheriting the older output would hide exactly that.

Every input is synthetic: temp files with placeholder content, no extraction data.
"""
import json
import os

from scripts import source_fingerprint as sf


def _file(tmp_path, name, content=b"payload"):
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


def _db(tmp_path, name, *, wal=b"log", shm=None):
    path = _file(tmp_path, name, b"database")
    if wal is not None:
        (tmp_path / (name + "-wal")).write_bytes(wal)
    if shm is not None:
        (tmp_path / (name + "-shm")).write_bytes(shm)
    return path


# --------------------------------------------------------------------------- fingerprint

def test_a_fingerprint_carries_both_hashes_and_the_size(tmp_path):
    record = sf.fingerprint(_file(tmp_path, "arroyo.db", b"abc"))
    assert record["present"] and record["bytes"] == 3
    assert record["md5"] == "900150983cd24fb0d6963f7d28e17f72"
    assert record["sha256"].startswith("ba7816bf")


def test_a_missing_artifact_is_recorded_as_missing_not_omitted(tmp_path):
    """"this run had no gallery.encrypteddb" is a finding, and a later run that has one is a
    difference worth showing."""
    record = sf.fingerprint(str(tmp_path / "not-there.db"))
    assert record["present"] is False
    assert record["why"]


def test_no_path_at_all_is_also_recorded(tmp_path):
    assert sf.fingerprint("")["why"] == "not located"


def test_the_zip_can_be_recorded_without_being_hashed(tmp_path):
    record = sf.fingerprint(_file(tmp_path, "extraction.zip"), hash_bytes=False)
    assert record["present"] and record["bytes"]
    assert "sha256" not in record          # tens of GB is not read to restate what artifacts bind


# --------------------------------------------------------------------------- sidecars

def test_a_database_is_fingerprinted_with_its_write_ahead_log(tmp_path):
    """sqlite_open reads every database twice, with the log applied and without, so the log is part
    of what "the same data" means."""
    path = _db(tmp_path, "arroyo.db", wal=b"log", shm=b"shm")
    sources = sf.collect({"arroyo": path})
    record = sources["artifacts"]["arroyo"]
    assert set(record["sidecars"]) == {"-wal", "-shm"}
    assert record["sidecars"]["-wal"]["sha256"] != record["sha256"]


def test_a_database_with_no_sidecar_is_not_an_error(tmp_path):
    path = _db(tmp_path, "arroyo.db", wal=None)
    record = sf.collect({"arroyo": path})["artifacts"]["arroyo"]
    assert record["present"] and "sidecars" not in record


def test_a_plist_is_not_given_sidecars(tmp_path):
    record = sf.collect({"user_plist": _file(tmp_path, "user.plist")})["artifacts"]["user_plist"]
    assert "sidecars" not in record


# --------------------------------------------------------------------------- collect / digest

def test_collect_records_the_tool_version_and_a_digest(tmp_path):
    sources = sf.collect({"arroyo": _db(tmp_path, "arroyo.db")}, keychain_path="")
    assert sources["tool_version"]
    assert sources["digest"] == sf.digest(sources)
    assert sources["artifacts"]["keychain"]["present"] is False


def test_the_digest_does_not_depend_on_ordering_or_on_when_the_run_was_made(tmp_path):
    a = sf.collect({"arroyo": _db(tmp_path, "arroyo.db"),
                    "scdb": _db(tmp_path, "scdb-27.sqlite3")})
    b = dict(a)
    b["artifacts"] = dict(reversed(list(a["artifacts"].items())))
    b["collected"] = "2999-01-01T00:00:00+00:00"
    assert sf.digest(b) == sf.digest(a)


def test_the_digest_moves_when_a_sidecar_moves(tmp_path):
    path = _db(tmp_path, "arroyo.db", wal=b"log-one")
    before = sf.digest(sf.collect({"arroyo": path}))
    (tmp_path / "arroyo.db-wal").write_bytes(b"log-two")
    assert sf.digest(sf.collect({"arroyo": path})) != before


def test_an_unexpected_role_is_recorded_rather_than_dropped(tmp_path):
    sources = sf.collect({"something_new": _file(tmp_path, "x.bin")})
    assert sources["artifacts"]["something_new"]["present"]


def test_sources_round_trip_through_the_report_folder(tmp_path):
    sources = sf.collect({"arroyo": _db(tmp_path, "arroyo.db")})
    reports = tmp_path / "Reports"
    path = sf.write_sources(str(reports), sources)
    assert os.path.isfile(path)
    assert sf.read_sources(str(reports))["digest"] == sources["digest"]
    assert sf.read_sources(str(tmp_path / "nowhere")) is None


# --------------------------------------------------------------------------- verify

def _two_runs(tmp_path):
    path = _db(tmp_path, "arroyo.db", wal=b"log")
    return sf.collect({"arroyo": path}), path


def test_identical_artifacts_verify(tmp_path):
    first, path = _two_runs(tmp_path)
    verdict = sf.verify(first, sf.collect({"arroyo": path}))
    assert verdict.ok and not verdict.problems
    assert "verified" in verdict.summary


def test_changed_bytes_are_reported_as_a_difference(tmp_path):
    first, path = _two_runs(tmp_path)
    with open(path, "wb") as fh:
        fh.write(b"a better extraction of the same device")
    verdict = sf.verify(first, sf.collect({"arroyo": path}))
    assert not verdict.ok
    assert [line["status"] for line in verdict.problems] == [sf.DIFFERS]


def test_a_changed_write_ahead_log_fails_the_verdict_even_though_the_database_matches(tmp_path):
    """A differing log is a real difference in the evidence, not a cosmetic one.

    The log is where sqlite_open recovers the deleted and superseded rows, so two logs can agree on
    every current row and still disagree about what was deleted. It gets its own verdict line so the
    examiner is told *what* differs — that is a diagnosis, not a discount.
    """
    first, path = _two_runs(tmp_path)
    (tmp_path / "arroyo.db-wal").write_bytes(b"a different log")
    verdict = sf.verify(first, sf.collect({"arroyo": path}))

    assert not verdict.ok                     # the whole manifest fails on the log alone
    problems = verdict.problems
    assert len(problems) == 1
    assert problems[0]["what"].endswith("-wal")
    assert problems[0]["status"] == sf.DIFFERS
    # and nothing in the wording invites treating it as harmless
    assert "different evidence" in problems[0]["note"]
    assert "harmless" in problems[0]["note"]
    # the database itself still matched, and says so — the point of the separate line
    assert any(line["status"] == sf.MATCH and line["what"] == "arroyo.db"
               for line in verdict.lines)


def test_reuse_is_refused_when_only_the_write_ahead_log_differs(tmp_path):
    """The case the wording used to invite: current rows identical, log not. Nothing may be reused."""
    first, path = _two_runs(tmp_path)
    (tmp_path / "arroyo.db-wal").write_bytes(b"a different log")
    verdict = sf.verify(first, sf.collect({"arroyo": path}))
    ok, why = sf.reuse_allowed(sf.check_version("1.0.0", "1.0.0"), verdict)
    assert not ok and "not identical" in why


def test_an_artifact_present_then_and_absent_now_is_missing_not_differing(tmp_path):
    first, path = _two_runs(tmp_path)
    os.remove(path)
    verdict = sf.verify(first, sf.collect({"arroyo": path}))

    problems = verdict.problems
    assert [line["status"] for line in problems] == [sf.MISSING_NOW]
    assert problems[0]["what"] == "arroyo.db"
    # the log is still there and still matches, and the verdict says so rather than blaming it too
    assert any(line["status"] == sf.MATCH and line["what"].endswith("-wal")
               for line in verdict.lines)


def test_a_database_and_its_log_both_going_missing_are_two_lines(tmp_path):
    first, path = _two_runs(tmp_path)
    os.remove(path)
    os.remove(str(tmp_path / "arroyo.db-wal"))
    verdict = sf.verify(first, sf.collect({"arroyo": path}))
    assert [line["status"] for line in verdict.problems] == [sf.MISSING_NOW, sf.MISSING_NOW]


def test_an_artifact_absent_then_and_present_now_is_new(tmp_path):
    first = sf.collect({"gallery_encrypteddb": ""})
    now = sf.collect({"gallery_encrypteddb": _file(tmp_path, "gallery.encrypteddb")})
    verdict = sf.verify(first, now)
    assert [line["status"] for line in verdict.problems] == [sf.NEW_NOW]


def test_absent_from_both_runs_is_a_match(tmp_path):
    first = sf.collect({"gallery_encrypteddb": ""})
    verdict = sf.verify(first, sf.collect({"gallery_encrypteddb": ""}))
    assert verdict.ok


def test_nothing_to_compare_is_not_verified(tmp_path):
    """"we could not check" and "it matched" must never read the same."""
    verdict = sf.verify(None, sf.collect({"arroyo": _db(tmp_path, "arroyo.db")}))
    assert not verdict.ok
    assert verdict.comparable is False
    assert "cannot be verified" in verdict.summary


def test_verdict_text_names_what_is_wrong_and_stays_quiet_about_what_is_fine(tmp_path):
    first, path = _two_runs(tmp_path)
    with open(path, "wb") as fh:
        fh.write(b"changed")
    text = sf.verdict_text(sf.verify(first, sf.collect({"arroyo": path})))
    assert "DIFFERS" in text
    assert text.count("\n") == 1                  # the summary plus the one problem


# --------------------------------------------------------------------------- version gate

def test_the_same_build_passes_and_a_different_one_does_not():
    assert sf.check_version("1.2.3+build.7", "1.2.3+build.7").ok
    assert not sf.check_version("1.2.3+build.7", "1.2.3+build.8").ok


def test_the_build_tag_is_part_of_the_comparison():
    """Two builds of the same version differ precisely in the tag, and that is the case where one
    of them decrypts something the other cannot."""
    verdict = sf.check_version("1.5.2+build.20260808", "1.5.2+build.20260901")
    assert not verdict.ok
    assert "20260808" in verdict.summary and "20260901" in verdict.summary


def test_a_selection_recording_no_version_is_not_treated_as_matching():
    verdict = sf.check_version("", "1.2.3")
    assert not verdict.ok and verdict.comparable is False


# --------------------------------------------------------------------------- reuse

def test_reuse_needs_both_the_same_build_and_identical_artifacts(tmp_path):
    first, path = _two_runs(tmp_path)
    same_sources = sf.verify(first, sf.collect({"arroyo": path}))
    same_version = sf.check_version("1.0.0", "1.0.0")

    ok, why = sf.reuse_allowed(same_version, same_sources)
    assert ok and "same build" in why

    ok, why = sf.reuse_allowed(sf.check_version("1.0.0", "2.0.0"), same_sources)
    assert not ok and "different build" in why

    with open(path, "wb") as fh:
        fh.write(b"changed")
    ok, why = sf.reuse_allowed(same_version, sf.verify(first, sf.collect({"arroyo": path})))
    assert not ok and "not identical" in why


def test_reuse_is_refused_when_the_earlier_run_recorded_no_sources(tmp_path):
    ok, why = sf.reuse_allowed(sf.check_version("1.0.0", "1.0.0"),
                               sf.verify(None, sf.collect({})))
    assert not ok and "never recorded" in why


def test_no_reuse_switches_everything_off(tmp_path):
    first, path = _two_runs(tmp_path)
    ok, why = sf.reuse_allowed(sf.check_version("1.0.0", "1.0.0"),
                               sf.verify(first, sf.collect({"arroyo": path})),
                               no_reuse=True)
    assert not ok and "switched off" in why
