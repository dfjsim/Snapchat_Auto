"""One copy per distinct content in the Memories report's ``media/`` folder.

Memories are grouped when they are the same media object (shared ``ZMEDIAID``) and/or when their
recovered media is byte-identical, so two snaps of a group routinely recover the very same bytes from
the very same cache file. Publishing a copy per snap wrote those bytes two or three times, and the
group's file table — which lists one row per distinct content — then linked only one of them, leaving
the rest in the folder referenced by no page at all.

So the media is published once and every Memory that recovered it links to that one copy, which is
what the app data says. These tests pin both halves: the folder holds one file per distinct content,
and the page still says which Memories each file belongs to — the file's *name* carries whichever
snap it was written for first, and that must not be read as ownership.

Every input here is synthetic. No extraction data is required or used.
"""
import hashlib
import os

from scripts import memories_media_report as memories_report

TOKEN = "EXAMPLETOKEN0123456AB"          # shaped like a CDN token; not one from any extraction
URL = f"https://cf-st.sc-cdn.net/d/{TOKEN}?bo=EXAMPLEBO&uc=00"
CACHE_KEY = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
OTHER_TOKEN = "EXAMPLETOKEN0123456CD"
OTHER_URL = f"https://cf-st.sc-cdn.net/d/{OTHER_TOKEN}?bo=EXAMPLEBO&uc=00"
OTHER_KEY = hashlib.sha256(OTHER_TOKEN.encode()).hexdigest()[:32]
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4 + b"mp42isom" + b"\x55" * 4000
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x22" * 3000
USER = "11111111-2222-3333-4444-555555555555"


# --------------------------------------------------------------- the choke point

def test_identical_bytes_are_written_once_and_the_second_caller_gets_the_first_name(tmp_path):
    published = {}
    first = memories_report._save_media(str(tmp_path), "SNAP-1_full_abcd.png", PNG, published)
    second = memories_report._save_media(str(tmp_path), "SNAP-2_full_abcd.png", PNG, published)

    assert first["out"] == "SNAP-1_full_abcd.png"
    assert second["out"] == "SNAP-1_full_abcd.png"
    assert second["bytes"] == first["bytes"] and second["dim"] == first["dim"]
    assert os.listdir(tmp_path) == ["SNAP-1_full_abcd.png"]


def test_the_two_callers_get_separate_stubs_to_fill_in(tmp_path):
    """Each Memory's entry carries its own role, provenance and cache key, so handing both the same
    dict would make one Memory's fields overwrite the other's."""
    published = {}
    first = memories_report._save_media(str(tmp_path), "SNAP-1_full_abcd.png", PNG, published)
    second = memories_report._save_media(str(tmp_path), "SNAP-2_full_abcd.png", PNG, published)
    first["role"] = "full"
    second["role"] = "thumbnail"

    assert first is not second and first["role"] == "full"


def test_different_bytes_are_still_separate_files(tmp_path):
    published = {}
    memories_report._save_media(str(tmp_path), "a.png", PNG, published)
    memories_report._save_media(str(tmp_path), "b.mp4", MP4, published)

    assert sorted(os.listdir(tmp_path)) == ["a.png", "b.mp4"]


def test_zero_length_files_are_never_de_duplicated(tmp_path):
    """Every empty file has the same MD5, and `assign_groups` excludes zero-byte media from its own
    byte-identity merge for that reason — so two of them are not two copies of one media, and
    collapsing them would point one group's page at another group's file."""
    published = {}
    first = memories_report._save_media(str(tmp_path), "SNAP-1_full_abcd.jpg", b"", published)
    second = memories_report._save_media(str(tmp_path), "SNAP-2_full_efgh.jpg", b"", published)

    assert first["out"] != second["out"]
    assert len(os.listdir(tmp_path)) == 2


def test_a_copy_already_on_disk_under_the_new_name_is_removed(tmp_path):
    """The poster worker writes its own output, so a de-duplicated poster would otherwise leave the
    second copy in the folder referenced by no page — the defect this exists to remove."""
    published = {}
    memories_report._save_media(str(tmp_path), "SNAP-1_poster.jpg", PNG, published)
    (tmp_path / "SNAP-2_poster.jpg").write_bytes(PNG)

    entry = memories_report._save_media(str(tmp_path), "SNAP-2_poster.jpg", PNG, published)

    assert entry["out"] == "SNAP-1_poster.jpg"
    assert os.listdir(tmp_path) == ["SNAP-1_poster.jpg"]


def test_repeating_the_same_name_does_not_delete_the_file_it_returns(tmp_path):
    published = {}
    memories_report._save_media(str(tmp_path), "SNAP-1_full_abcd.png", PNG, published)
    entry = memories_report._save_media(str(tmp_path), "SNAP-1_full_abcd.png", PNG, published)

    assert entry["out"] == "SNAP-1_full_abcd.png"
    assert (tmp_path / "SNAP-1_full_abcd.png").read_bytes() == PNG


def test_without_a_map_every_call_writes_its_own_file(tmp_path):
    """The default is unchanged: a caller that keeps no map gets exactly the old behaviour."""
    memories_report._save_media(str(tmp_path), "a.png", PNG)
    memories_report._save_media(str(tmp_path), "b.png", PNG)

    assert sorted(os.listdir(tmp_path)) == ["a.png", "b.png"]


# --------------------------------------------------------------- through collect_media

def _app(tmp_path):
    d = tmp_path / "app" / "Documents" / f"com.snap.file_manager_3_SCContent_{USER}"
    d.mkdir(parents=True)
    return str(tmp_path / "app"), d


def _memory(snap_id, url=URL, **over):
    """A Memory whose cached media is stored in the clear, so no key is needed to recover it."""
    m = {"snap_id": snap_id, "user_hash": "aa" * 32, "key": None, "iv": None, "is_meo": False,
         "key_wrapped": False, "key_source": None, "media_url": url, "overlay_url": None,
         "thumb_url": None, "width": 720, "height": 1280, "media_files": []}
    m.update(over)
    return m


def test_two_memories_sharing_a_cache_file_publish_one_copy(tmp_path):
    """The real case: grouped Memories point at the same media object, so both resolve the same
    CACHE_KEY and both recover the same bytes."""
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    outdir = str(tmp_path / "out")
    mems = {"SNAP-1": _memory("SNAP-1"), "SNAP-2": _memory("SNAP-2")}

    memories_report.collect_media(mems, app, outdir)

    files = [f for m in mems.values() for f in m["media_files"]]
    assert len(files) == 2                                     # both Memories recovered it
    assert len({f["out"] for f in files}) == 1                 # from one file on disk
    assert len(os.listdir(outdir)) == 1


def test_each_memory_keeps_its_own_provenance_for_the_shared_file(tmp_path):
    """Only the copy on disk is shared. What is merged must be the bytes, never the record of where
    each Memory's bytes came from."""
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    mems = {"SNAP-1": _memory("SNAP-1"), "SNAP-2": _memory("SNAP-2")}

    memories_report.collect_media(mems, app, str(tmp_path / "out"))

    one, two = mems["SNAP-1"]["media_files"][0], mems["SNAP-2"]["media_files"][0]
    assert one is not two
    assert one["cache_key"] == two["cache_key"] == CACHE_KEY
    assert one["hashes"] == two["hashes"]                       # same bytes, so the same hashes
    one["role"] = "changed"
    assert two["role"] == "full"


def test_unrelated_media_is_not_collapsed(tmp_path):
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    (scdir / OTHER_KEY).write_bytes(PNG)
    outdir = str(tmp_path / "out")
    mems = {"SNAP-1": _memory("SNAP-1"), "SNAP-2": _memory("SNAP-2", url=OTHER_URL)}

    memories_report.collect_media(mems, app, outdir)

    assert len(os.listdir(outdir)) == 2


def test_every_published_file_is_referenced_by_a_memory(tmp_path):
    """The defect in one line: a file in the folder that no Memory's media_files names is a file no
    page can link to."""
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    (scdir / OTHER_KEY).write_bytes(PNG)
    outdir = str(tmp_path / "out")
    mems = {"SNAP-1": _memory("SNAP-1"), "SNAP-2": _memory("SNAP-2"),
            "SNAP-3": _memory("SNAP-3", url=OTHER_URL)}

    memories_report.collect_media(mems, app, outdir)

    referenced = {f["out"] for m in mems.values() for f in m["media_files"]}
    assert set(os.listdir(outdir)) == referenced


# --------------------------------------------------------------- the poster frame off a shared video

def _fake_poster_run(recorded):
    """Stand in for the extraction subprocess: write a still at each destination asked for.

    Each one is given the bytes of its own source, so two different videos produce two different
    frames — as they do in reality, and as they must here for the de-duplication under test to be
    answering the question the test is asking.
    """
    def run_jobs(jobs, *args, **kwargs):
        recorded.extend(jobs)
        for src, dst, _complete in jobs:
            with open(dst, "wb") as fh:
                fh.write(PNG + open(src, "rb").read())
        return {src: True for src, _dst, _complete in jobs}, []
    return run_jobs


def test_a_shared_video_is_decoded_once_and_both_memories_get_that_poster(tmp_path, monkeypatch):
    """Two Memories referencing one video file would otherwise each decode it — the same frame
    extracted twice, published twice, and one of the two copies linked by no page."""
    jobs = []
    monkeypatch.setattr(memories_report.poster_worker, "run_jobs", _fake_poster_run(jobs))
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    outdir = str(tmp_path / "out")
    mems = {"SNAP-1": _memory("SNAP-1"), "SNAP-2": _memory("SNAP-2")}

    memories_report.collect_media(mems, app, outdir)

    assert len(jobs) == 1, "the same video was handed to the decoder twice"
    posters = [f for m in mems.values() for f in m["media_files"] if f.get("generated")]
    assert len(posters) == 2                                   # both Memories have a poster
    assert len({f["out"] for f in posters}) == 1               # and it is one file
    assert sorted(os.listdir(outdir)) == ["SNAP-1_full_" + CACHE_KEY[:8] + ".mp4",
                                          "SNAP-1_poster.jpg"]


def test_a_video_only_one_memory_holds_still_gets_its_own_poster(tmp_path, monkeypatch):
    jobs = []
    monkeypatch.setattr(memories_report.poster_worker, "run_jobs", _fake_poster_run(jobs))
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    (scdir / OTHER_KEY).write_bytes(MP4[:-8] + b"\x66" * 8)     # a different video
    mems = {"SNAP-1": _memory("SNAP-1"), "SNAP-2": _memory("SNAP-2", url=OTHER_URL)}

    memories_report.collect_media(mems, app, str(tmp_path / "out"))

    assert len(jobs) == 2
    posters = {sid: [f["out"] for f in m["media_files"] if f.get("generated")]
               for sid, m in mems.items()}
    assert posters == {"SNAP-1": ["SNAP-1_poster.jpg"], "SNAP-2": ["SNAP-2_poster.jpg"]}


def _identical_poster_run(recorded):
    """Stand in for the extraction subprocess, writing the SAME frame for every video.

    Two different videos yielding byte-identical frames is ordinary — two cached copies of one video
    where only one is truncated will do it, and so will any two videos that open on the same picture.
    The other fixture here gives each source its own bytes, which is what let this case through.
    """
    def run_jobs(jobs, *args, **kwargs):
        recorded.extend(jobs)
        for _src, dst, _complete in jobs:
            with open(dst, "wb") as fh:
                fh.write(PNG)
        return {src: True for src, _dst, _complete in jobs}, []
    return run_jobs


def test_two_videos_with_the_same_frame_do_not_cost_a_third_memory_its_poster(tmp_path, monkeypatch):
    """The report went missing entirely over this. The frames de-duplicate, so the second one's file is
    removed — and a further Memory sharing that video then read a file that had just been deleted,
    which raised out of the whole report. The frame is published once per VIDEO now, before any Memory
    is handed one, because the worker's output only exists until `_save_media` has seen it.
    """
    jobs = []
    monkeypatch.setattr(memories_report.poster_worker, "run_jobs", _identical_poster_run(jobs))
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    (scdir / OTHER_KEY).write_bytes(MP4[:-8] + b"f" * 8)      # a different video, same frame
    outdir = str(tmp_path / "out")
    mems = {"SNAP-1": _memory("SNAP-1"),
            "SNAP-2": _memory("SNAP-2", url=OTHER_URL),
            "SNAP-3": _memory("SNAP-3", url=OTHER_URL)}          # shares SNAP-2's video

    memories_report.collect_media(mems, app, outdir)             # used to raise FileNotFoundError

    posters = {sid: [f["out"] for f in m["media_files"] if f.get("generated")]
               for sid, m in mems.items()}
    assert all(posters.values()), f"a Memory lost its poster: {posters}"
    assert len({name for names in posters.values() for name in names}) == 1, "one frame, one file"
    assert "SNAP-2_poster.jpg" not in os.listdir(outdir), "the de-duplicated copy is still removed"


def test_an_unreadable_frame_costs_a_thumbnail_and_nothing_else(tmp_path, monkeypatch):
    """A poster is a derived artifact, not device data. Reporting one Memory without a generated still
    is a small loss; losing the report is not."""
    def run_jobs(jobs, *args, **kwargs):
        return {src: True for src, _dst, _complete in jobs}, []   # claims success, writes nothing
    monkeypatch.setattr(memories_report.poster_worker, "run_jobs", run_jobs)
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    mems = {"SNAP-1": _memory("SNAP-1")}

    memories_report.collect_media(mems, app, str(tmp_path / "out"))

    assert [f["out"] for f in mems["SNAP-1"]["media_files"]], "the video itself is still published"
    assert not [f for f in mems["SNAP-1"]["media_files"] if f.get("generated")]


def test_the_whole_poster_stage_cannot_cost_the_report(tmp_path, monkeypatch):
    """Belt as well as braces: whatever goes wrong in there, the media that was recovered is reported."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("the decoder went away")
    monkeypatch.setattr(memories_report, "_add_posters", boom)
    app, scdir = _app(tmp_path)
    (scdir / CACHE_KEY).write_bytes(MP4)
    mems = {"SNAP-1": _memory("SNAP-1")}

    memories_report.collect_media(mems, app, str(tmp_path / "out"))

    assert [f["role"] for f in mems["SNAP-1"]["media_files"]] == ["full"]


# --------------------------------------------------------------- what the page says about it

def _with_file(snap_id, media_id="MEDIA-1", **file_over):
    m = {"snap_id": snap_id, "user_hash": "u" * 12,
         "media_type": None, "format": "", "media_format": None,
         "media_url": None, "overlay_url": None, "thumb_url": None,
         "create_utc": "", "created_sort": 0,
         "duration": None, "width": None, "height": None, "camera": "",
         "has_location": False, "times": {}, "entry_times": {},
         "snap_other": {}, "entry_other": {}, "urls": {},
         "ids": {"ZMEDIAID": media_id, "ZSNAPID": snap_id},
         "key": None, "iv": None, "is_meo": False, "key_wrapped": False, "key_source": None,
         "media_refs": [], "latitude": None, "longitude": None, "address": None,
         "wal": None, "prior_rows": [], "map": None}
    f = {"out": "SNAP-1_full_abcd.mp4", "path": "media/SNAP-1_full_abcd.mp4", "bytes": 4000,
         "dim": "", "snap_dim": "", "role": "full", "source": "SCContent", "ext": "mp4",
         "src": [], "hashes": [("", "d" * 32, "e" * 64)], "cache_key": CACHE_KEY, "in_cc": False,
         "how": "", "complete": True, "why_incomplete": ""}
    f.update(file_over)
    m["media_files"] = [f]
    return m


def _group_detail(members):
    return memories_report._render_group_detail(members, True, [], [], None, {}, {})


def test_the_referencing_memories_are_named_per_file():
    members = [_with_file("SNAP-1"), _with_file("SNAP-2")]
    out = _group_detail(members)

    assert "one file on disk, recovered under 2 of the 2 memories shown" in out
    assert "href='#mem-SNAP-1'" in out and "href='#mem-SNAP-2'" in out


def test_a_file_only_one_member_recovered_says_so():
    """Without this the table cannot tell a file the whole group recovered from one a single snap of
    it did — both are one row."""
    members = [_with_file("SNAP-1"),
               _with_file("SNAP-2", out="SNAP-2_full_efgh.png", role="thumbnail",
                          hashes=[("", "f" * 32, "a" * 64)])]
    out = _group_detail(members)

    assert "recovered under 1 of the 2 memories shown" in out
    assert "one file on disk" not in out


def test_a_differing_role_is_named_against_the_memory_that_holds_it():
    """The same bytes can be one snap's full media and another's thumbnail; the row shows one role,
    so the other has to be said where it belongs."""
    members = [_with_file("SNAP-1"), _with_file("SNAP-2", role="thumbnail")]
    out = _group_detail(members)

    assert "as thumbnail" in out


def test_the_sharing_is_explained_once_per_table_not_once_per_row():
    """It explains what the column says, not anything about one file — and the same paragraph on
    every row of every group page is a page several times the size it needs to be."""
    one, two = _with_file("SNAP-1"), _with_file("SNAP-2")
    two["media_files"].append(dict(two["media_files"][0], out="SNAP-2_thumbnail_efgh.png",
                                   role="thumbnail", hashes=[("", "f" * 32, "a" * 64)]))
    out = _group_detail([one, two])

    assert out.count("memories shown") == 2                    # a line on each of the two rows
    assert out.count(memories_report.SHARED_MEDIA_BASIS[:60]) == 1


def test_a_single_memory_page_says_nothing_about_sharing():
    out = _group_detail([_with_file("SNAP-1")])

    assert "memories shown" not in out and "mrefs" not in out


def test_a_file_named_after_an_absent_memory_is_not_attributed_to_it():
    """In a partial extract the surviving copy can carry the snap id of an omitted member — the first
    writer's name. The name is a name; the per-file line must still say only what this extract holds,
    and the sharebar is what accounts for the snap the name mentions."""
    group_of = {"SNAP-1": ["SNAP-1", "SNAP-2"], "SNAP-2": ["SNAP-1", "SNAP-2"]}
    kept = _with_file("SNAP-2")                                # its file is named for SNAP-1
    assert kept["media_files"][0]["out"].startswith("SNAP-1")

    out = memories_report._render_group_detail([kept], True, [], [], None, {}, {},
                                              group_of=group_of)

    assert "recovered under 1 of the 1 memories shown" not in out    # single page: no sharing line
    assert "href='#mem-SNAP-1'" not in out                          # never attributed to the absent
    assert "1 of 2</b> memories grouped here" in out                # but the group's size is stated
    assert "mem-SNAP-1" in out                                      # and the absent snap is named


def test_a_partial_group_page_links_only_to_the_memories_it_shows():
    """Every in-page link has to resolve. A Memory the extract leaves out has no anchor on the page,
    so the file table must not link to it — the sharebar is what accounts for it instead."""
    group_of = {"SNAP-1": ["SNAP-1", "SNAP-2"], "SNAP-2": ["SNAP-1", "SNAP-2"]}
    out = memories_report._render_group_detail([_with_file("SNAP-1")], True, [], [], None, {}, {},
                                               group_of=group_of)

    assert "href='#mem-SNAP-2'" not in out
    assert "1 of 2</b> memories grouped here" in out            # stated, not silently dropped


# --------------------------------------------------------------- the two helpers agree

def test_one_table_row_per_file_on_disk():
    members = [_with_file("SNAP-1"), _with_file("SNAP-2"),
               _with_file("SNAP-3", out="SNAP-3_full_efgh.png", hashes=[("", "f" * 32, "a" * 64)])]

    files = memories_report._dedup_media(members)
    refs = memories_report._media_refs(members)

    assert [f["out"] for f in files] == ["SNAP-1_full_abcd.mp4", "SNAP-3_full_efgh.png"]
    assert set(refs) == {memories_report._media_key(f) for f in files}
    assert [sid for sid, _role in refs["d" * 32]] == ["SNAP-1", "SNAP-2"]
    assert [sid for sid, _role in refs["f" * 32]] == ["SNAP-3"]


def test_a_memory_holding_the_same_content_twice_is_named_once():
    """Two cache keys can hold the same bytes, which is one row and one file — not one Memory listed
    against itself twice."""
    member = _with_file("SNAP-1")
    member["media_files"].append(dict(member["media_files"][0], role="thumbnail",
                                      cache_key=OTHER_KEY))
    refs = memories_report._media_refs([member])

    assert refs["d" * 32] == [("SNAP-1", "full")]
