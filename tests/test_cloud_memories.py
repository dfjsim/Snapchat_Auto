"""The Memories side of the cloud download: candidates, the request's scopes and date rules, and
what comes back — attached apart from the device's media, and compared with the device's cache files.

Every input is synthetic; nothing is requested from anywhere (the fetcher is scripted).
"""
import datetime
import hashlib
import os
import random

import pytest

from scripts import cloud_download as cd
from scripts import cloud_memories as cm
from scripts import memories_media_report as mr

UTC = datetime.timezone.utc
AUTH = cd.Authority("Consent of the account holder, 2026-10-01", True, "2026-10-02T10:00:00Z")
URL = "https://cf-st.sc-cdn.net/d/TOKEN?bo=x"


def _memory(sid, *, files=(), media_type=0, key=True, urls=None, raw=None, memdata=()):
    m = mr._bare_memory(sid, {"userHash": "aa" * 32})
    m.update({"media_files": list(files), "media_type": media_type,
              "key": b"\x01" * 32 if key else None, "iv": b"\x02" * 16 if key else None,
              "urls": {"ZMEDIADOWNLOADURL": URL} if urls is None else urls,
              "raw_times": raw or {}, "memdata": list(memdata)})
    return m


def _f(role, ext="jpg", complete=True, generated=False):
    return {"role": role, "ext": ext, "complete": complete, "generated": generated}


# ----------------------------------------------------------------------------------- candidates

def test_candidate_states():
    assert cm.candidate(_memory("A"))["state"] == "missing"
    assert cm.candidate(_memory("A", files=[_f("thumbnail")]))["state"] == "missing"
    assert cm.candidate(_memory("A", files=[_f("full")]))["state"] == ""
    part = cm.candidate(_memory("A", files=[_f("full", complete=False)]))
    assert part["state"] == "incomplete" and "only part" in part["reasons"][0]
    still = cm.candidate(_memory("A", media_type=1, files=[_f("full", "jpg")]))
    assert still["state"] == "incomplete" and "only a still" in still["reasons"][0]
    assert "transcoded" in cm.candidate(_memory("A", files=[_f("transcoded", "mp4")]))["reasons"][0]
    overlay = cm.candidate(_memory("A", files=[_f("full")],
                                   urls={"ZMEDIADOWNLOADURL": URL, "ZOVERLAYDOWNLOADURL": URL}))
    assert overlay["state"] == "incomplete" and overlay["roles"] == ["overlay"]
    assert cm.candidate(_memory("A", urls={"ZMEDIAREDIRECTURI": "s3://x"}))["state"] == "nourl"
    m = _memory("A")
    m["cloud_files"] = [{"role": "media"}]
    assert cm.candidate(m)["state"] == "retrieved"


# ---------------------------------------------------------------------------------- date rules

def _cocoa(y, mo, d, h=12):
    return (datetime.datetime(y, mo, d, h, tzinfo=UTC)
            - datetime.datetime(2001, 1, 1, tzinfo=UTC)).total_seconds()


CAP, CRE = "ZGALLERYSNAP.ZCAPTURETIMEUTC", "ZGALLERYSNAP.ZCREATETIMEUTC"
FIELDS = [CAP, CRE, "ZGALLERYENTRY.ZCREATETIMEUTC"]


def test_rules_parse_and_match():
    rules = cm.parse_rules("ZCAPTURETIMEUTC|2024-03-01|2024-03-31|include;"
                           "ZCREATETIMEUTC||2024-02-29|exclude", UTC, FIELDS)
    assert [r.fields for r in rules] == [(CAP,), (CRE,)]
    assert rules[0].end == datetime.datetime(2024, 3, 31, 23, 59, 59, tzinfo=UTC)
    inside = {CAP: datetime.datetime(2024, 3, 10, tzinfo=UTC),
              CRE: datetime.datetime(2024, 3, 10, tzinfo=UTC)}
    assert cm.passes(rules, inside)
    saved_before = dict(inside, **{CRE: datetime.datetime(2024, 2, 1, tzinfo=UTC)})
    assert not cm.passes(rules, saved_before)                   # an exclude rule matched
    assert not cm.passes(rules, {CRE: inside[CRE]})              # no capture time: include fails
    assert cm.passes([], {})                                     # no rules: everything passes


def test_one_range_for_several_timestamps_and_for_all():
    rules = cm.parse_rules(f"{CAP},ZGALLERYENTRY.ZCREATETIMEUTC|2024-03-01|2024-03-31|include", UTC,
                           FIELDS)
    assert rules[0].fields == (CAP, "ZGALLERYENTRY.ZCREATETIMEUTC")
    entry_only = {"ZGALLERYENTRY.ZCREATETIMEUTC": datetime.datetime(2024, 3, 2, tzinfo=UTC)}
    assert cm.passes(rules, entry_only)
    star = cm.parse_rules("*|2024-03-01||include", UTC, FIELDS)
    assert cm.passes(star, {"anything": datetime.datetime(2024, 4, 1, tzinfo=UTC)})
    with pytest.raises(ValueError):
        cm.parse_rules("ZNOSUCHTIME|2025-01-01||include", UTC, FIELDS)
    with pytest.raises(ValueError):
        cm.parse_rules(f"{CAP}|not a date||include", UTC, FIELDS)
    with pytest.raises(ValueError):
        cm.parse_rules(f"{CAP}|2025-01-01||keep", UTC, FIELDS)


def test_dates_are_read_in_the_report_timezone_across_dst():
    from zoneinfo import ZoneInfo
    toronto = ZoneInfo("America/Toronto")
    summer = cm.parse_when("2024-07-15 08:30", toronto)
    winter = cm.parse_when("2024-12-10 08:30", toronto)
    assert summer.hour == 12 and winter.hour == 13                # EDT vs EST


def test_memory_instants_include_the_memdata_time():
    m = _memory("A", raw={CAP: _cocoa(2024, 3, 10)},
                memdata=[{"created_ms": 1_710_000_000_000, "uuid": "U"}])
    inst = cm.memory_instants(m)
    assert inst[CAP] == datetime.datetime(2024, 3, 10, 12, tzinfo=UTC)
    assert inst[cm.MEMDATA_FIELD].year == 2024


# ------------------------------------------------------------------------------------- planning

def test_scopes_dates_and_what_is_left_out():
    mems = {"A": _memory("A", raw={CAP: _cocoa(2024, 3, 10)}),                   # missing, in range
            "B": _memory("B", raw={CAP: _cocoa(2024, 1, 1)}),                    # missing, too early
            "C": _memory("C", files=[_f("full")], raw={CAP: _cocoa(2024, 3, 11)}),
            "D": _memory("D", key=False, raw={CAP: _cocoa(2024, 3, 12)}),        # no usable key
            "E": _memory("E", urls={}, raw={CAP: _cocoa(2024, 3, 13)})}          # nothing to ask
    for m in mems.values():
        m["cloud"] = cm.candidate(m)
    rules = cm.parse_rules(f"{CAP}|2024-03-01|2024-03-31|include", UTC, FIELDS)
    request = cm.CloudRequest(AUTH, scopes={"missing", "snaps"}, snap_ids={"C", "ZZZ"},
                              date_rules=rules)
    jobs, plan = cm.plan_jobs(mems, request)
    assert sorted(j.snap_id for j in jobs) == ["A", "C"]          # C only because it was pasted
    assert plan["dropped_by_dates"] == 1 and plan["no_key"] == 1 and plan["unknown_ids"] == ["ZZZ"]
    assert next(j for j in jobs if j.snap_id == "C").reason == "requested by snap id"


def test_pasted_ids():
    ids, rejected = cm.snap_ids_from_text(
        "mem-0a0b0c0d-1111-4222-8333-444455556666\n0A0B0C0D-1111-4222-8333-444455556666, nope")
    assert ids == ["0A0B0C0D-1111-4222-8333-444455556666"] and rejected == ["nope"]


def test_a_request_without_authority_or_scope_is_refused():
    assert cm.CloudRequest(cd.Authority("", False)).problems()
    assert "nothing to retrieve" in " ".join(cm.CloudRequest(AUTH, scopes=set()).problems())
    assert cm.CloudRequest(AUTH, scopes={"snaps"}).problems()      # snaps scope with no ids
    assert cm.CloudRequest(AUTH).problems() == []


# --------------------------------------------------------------------------- what comes back

class _Fetcher:
    user_agent = "test"

    def __init__(self, body):
        self.body = body

    def fetch(self, url, *, timeout, max_bytes, dest):
        with open(dest, "wb") as fh:
            fh.write(self.body)
        return cd.Response(200, url, [], {}, dest, len(self.body),
                           hashlib.sha256(self.body).hexdigest(), hashlib.md5(self.body).hexdigest())


def _retrieve(tmp_path, mems, plain):
    store = cd.Store(str(tmp_path / "run"))
    engine = cd.Engine(store, _Fetcher(b"ENC" + plain), cd.Control(cd.Pace(delay_s=0, jitter_s=0)),
                       AUTH, sleep=lambda s: None, rng=random.Random(1))
    jobs = [cd.Job(sid, "media", [("ZMEDIADOWNLOADURL", URL)], "missing") for sid in mems]
    engine.run(jobs, decrypt=lambda job, raw: {"result": "ok", "ext": "mp4", "data": raw[3:]})
    return str(tmp_path / "run")


def test_attach_keeps_server_copies_apart_and_finds_identical_cache_files(tmp_path):
    plain = b"\x00\x00\x00\x18ftypmp42" + b"\x07" * 3000
    mems = {"A": _memory("A")}
    run = _retrieve(tmp_path, mems, plain)
    media = tmp_path / "media"
    assert cm.attach(mems, run, str(media)) == 1
    m = mems["A"]
    assert m["media_files"] == [] and len(m["cloud_files"]) == 1
    f = m["cloud_files"][0]
    assert (media / "cloud" / os.path.basename(f["out"])).read_bytes() == plain
    assert f["authority"]["note"].startswith("Consent") and f["encrypted"]["bytes"] == len(plain) + 3
    # a cache file on the device with the same bytes is proven identical; one of the same size is not
    same, other = tmp_path / ("a" * 32), tmp_path / ("b" * 32)
    same.write_bytes(plain)
    other.write_bytes(b"\x01" * len(plain))
    found = cm.find_identical(mems, {"a" * 32: [str(same)], "b" * 32: [str(other)]}, {},
                              resolve=lambda key: (None,))
    assert list(found) == ["a" * 32] and found["a" * 32][0]["what"] == "decrypted"
    assert f["identical_cached"] == [{"cache_key": "a" * 32, "what": "decrypted"}]
    cm.write_manifests(mems, str(tmp_path), found)
    assert (tmp_path / "cloud_media.json").exists() and (tmp_path / "media_by_content.json").exists()
    assert cm.provenance(mems)["sessions"][0]["note"].startswith("Consent")


def test_nothing_is_written_without_retrieved_media(tmp_path):
    mems = {"A": _memory("A")}
    assert cm.attach(mems, str(tmp_path / "run"), str(tmp_path / "media")) == 0
    cm.write_manifests(mems, str(tmp_path), {})
    assert not (tmp_path / "cloud_media.json").exists()
    assert cm.provenance(mems) is None


def test_the_detail_block(tmp_path):
    m = _memory("A")
    m["cloud"] = cm.candidate(m)
    offer = mr._cloud_html([m], "../", "../../", None)
    assert "Get from" in offer and "scCopySnapIds" in offer
    assert mr._cloud_html([m], "../", "../../", object()) == ""     # never in a partial extract
    plain = b"\x00\x00\x00\x18ftypmp42" + b"\x07" * 100
    run = _retrieve(tmp_path, {"A": m}, plain)
    cm.attach({"A": m}, run, str(tmp_path / "media"))
    block = mr._cloud_html([m], "../", "../../", None)
    assert "NOT device evidence" in block and "Consent of the account holder" in block
