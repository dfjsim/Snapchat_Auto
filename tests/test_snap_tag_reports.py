"""Where the Snapchat app's tag and an edit's source files reach the reports — the shared renderer,
the search text, the Memories index, a chat attachment and the Android Memories page.

The tag is decoded from a synthetic value (test_snap_media_tag) and the files are built here; no
extraction data is used. What is pinned: the tag is shown decoded in a block of its own and searchable
by its user agent and lens id; an edit's source files are shown apart, labelled as theirs, and their
dates never become this file's.
"""
import os
import re

from scripts import conversations_report as cr
from scripts import memories_media_report as mr
from scripts import report_ui
from scripts.data import media_meta, snap_media_tag
from tests.test_media_meta import _asset, _box, _edit_xmp, _video, _xmp_uuid
from tests.test_memory_time_sources import _recover
from tests.test_snap_media_tag import IOS_UA, _tag

UTC = lambda seconds: f"U{int(seconds)}"                    # noqa: E731 - a recognisable formatter


def _tagged_meta(tmp_path):
    return media_meta.extract(_video(tmp_path / "t.mp4", _box(b"udta", _asset(b"dscp", _tag()))))


# --------------------------------------------------------------------------- the shared renderer

def test_the_tag_is_a_block_of_its_own_with_where_it_was_read(tmp_path):
    meta = _tagged_meta(tmp_path)
    html = report_ui.embedded_meta_html(meta, report_ui.file_time_rows(meta, UTC), label="t.mp4")

    assert "Snapchat app tag" in html and "iPhone15,2" in html and "iOS 17.0" in html
    assert "Lens id" in html and "12345678901" in html
    assert "not necessarily this device" in html
    assert "moov › udta › dscp — byte offset" in html
    assert "Snapchat app writes a tag" in html                # its «?»


def test_without_popovers_the_block_carries_no_question_mark(tmp_path):
    meta = _tagged_meta(tmp_path)
    html = report_ui.embedded_meta_html(meta, [], popover=False)

    assert "Snapchat app tag" in html and 'class="hint"' not in html


def test_the_tag_is_searchable_by_user_agent_lens_and_encoded_text(tmp_path):
    meta = _tagged_meta(tmp_path)
    terms = report_ui.embedded_search_terms(meta, [], media_meta.STRUCTURAL)

    assert IOS_UA in terms and "12345678901" in terms and _tag() in terms
    assert "snapchat app tag lens" in terms


def test_an_edit_s_source_files_are_shown_apart_and_their_dates_are_not_searchable(tmp_path):
    meta = media_meta.extract(_video(tmp_path / "e.mp4", after=_xmp_uuid(_edit_xmp("An edit"))))
    times = report_ui.file_time_rows(meta, UTC)
    html = report_ui.embedded_meta_html(meta, times)
    terms = report_ui.embedded_search_terms(meta, times, media_meta.STRUCTURAL)

    assert "Source files this file was edited from — 4" in html
    assert "describe those source files, not this one" in html
    assert "clip.mov" in html and "used 2 times in the edit" in html
    assert "the source file's fix, not this file's" in html
    assert "not set (1904-01-01T00:00:00Z)" in html
    # the source files' own times are formatted into their rows, never into the file's
    clip_epoch = 1577934245                                      # 2020-01-02 03:04:05 UTC
    assert f"U{clip_epoch}" in html
    assert f"U{clip_epoch}" not in [t["shown"] for t in times]
    assert "clip.mov" in terms and "Phone camera" in terms
    assert f"U{clip_epoch}" not in terms


def test_a_source_file_s_own_tag_is_shown_with_it_not_as_the_file_s(tmp_path):
    xmp = _edit_xmp("An edit").replace(
        b'xmp:CreatorTool="Phone camera">',
        b'xmp:CreatorTool="Phone camera"><exif:UserComment><rdf:Alt><rdf:li xml:lang="x-default">'
        + _tag().encode() + b"</rdf:li></rdf:Alt></exif:UserComment>")
    meta = media_meta.extract(_video(tmp_path / "e.mp4", after=_xmp_uuid(xmp)))
    clip = next(s for s in meta["xmp_sources"] if s["instance_id"] == "xmp.iid:clip")

    assert "snapchat" not in meta                              # not this file's tag
    assert clip["snapchat"][0]["device"] == "iPhone15,2"
    assert clip["snapchat"][0]["field"] == "uuid XMP › xmpMM:Pantry › exif:UserComment"
    html = report_ui.embedded_meta_html(meta, report_ui.file_time_rows(meta, UTC))
    assert "the app that wrote that source file" in html
    assert IOS_UA in report_ui.xmp_source_search_terms(meta)


# --------------------------------------------------------------------------- the Memories index

def _tag_record():
    tag = snap_media_tag.decode(_tag())
    return dict(tag, field="moov › udta › dscp", offset=100, also=[], names=["dscp"])


def test_the_memories_index_flags_filters_and_lists_the_tag(tmp_path):
    mems, out, _root = _recover(tmp_path)
    mems["SNAP-1"]["media_files"][0]["meta"]["snapchat"] = [_tag_record()]
    report, _l, _g = mr.generate_report(mems, out, True, tz_label="UTC", run_id="R")
    page = open(report, encoding="utf-8").read()
    index = open(os.path.join(out, "data", "index.js"), encoding="utf-8").read()
    detail = open(os.path.join(out, "data", "detail-0.js"), encoding="utf-8").read()

    assert "APP TAG" in index and re.search(r'"stag":\s*"y"', index)
    assert "with the Snapchat app's tag &mdash; 1" in page
    assert 'me==="tag"?m.stag==="y"' in page
    assert IOS_UA.lower() in index.lower() and "12345678901" in index
    assert "Snapchat app tag · lens id" in detail


def test_a_memory_without_a_tag_carries_no_flag(tmp_path):
    mems, out, _root = _recover(tmp_path)
    mr.generate_report(mems, out, True, tz_label="UTC", run_id="R")
    index = open(os.path.join(out, "data", "index.js"), encoding="utf-8").read()

    assert "APP TAG" not in index and "stag" not in index


# --------------------------------------------------------------------------- a chat attachment

def test_a_chat_attachment_shows_what_it_says_about_itself_without_popovers(tmp_path):
    cache = tmp_path / "cacheFiles"
    cache.mkdir()
    video = _video(tmp_path / "t.mp4", _box(b"udta", _asset(b"dscp", _tag())))
    (cache / "abc").write_bytes(open(video, "rb").read())
    att = cr.publish_attachment(str(cache), str(tmp_path / "media"), "abc")
    att["file_times"] = report_ui.file_time_rows(att["meta"], UTC)
    html = cr._attachment_detail(att, "../")

    assert att["meta"]["snapchat"][0]["device"] == "iPhone15,2"
    assert "Embedded metadata" in html and "Snapchat app tag" in html
    block = html[html.index("Embedded metadata"):]
    assert 'class="hint"' not in block                         # explained once, in the header


def test_the_conversation_css_carries_the_embedded_block_styles(tmp_path):
    cr.write_assets(str(tmp_path))
    css = open(os.path.join(tmp_path, "assets", "ui.css"), encoding="utf-8").read()
    assert ".metafile" in css and ".snaptag" in css


def test_the_basis_texts_say_whose_data_it_is():
    assert "the sender's device" in report_ui.SNAP_TAG_BASIS
    assert "describe THOSE files" in report_ui.XMP_SOURCES_BASIS
