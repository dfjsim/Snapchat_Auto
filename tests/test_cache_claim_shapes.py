"""Which cache_controller EXTERNAL_KEY shapes name a Memory.

The Memories report used to decide this with a substring test over the text *before* the UUID
(`"media" in prefix …`), which got it wrong in both directions:

* `<UUID>_memories_backup_transcoded` puts the UUID first, so the tested prefix was empty and the
  claim was dropped — while being MEDIA_CONTEXT_TYPE 19 full media that decrypts with the Memory's
  own key to a complete MP4 (verified on two devices, including minute-long videos that nothing
  else in the report recovered);
* `https://…/previewmedia/<UUID>` contains "media", so it was indexed as a Memory claim.

Both reports now read one list, so a file the cache_controller report ties to a Memory is a file
the Memories report also finds. Every input here is synthetic.
"""
import pytest

from scripts import memories_media_report as memories_report
from scripts import cache_controller_report as cc_report

U = "12345678-1234-1234-1234-1234567890ab"


@pytest.mark.parametrize("ek,role", [
    (f"snap-media-{U}", "full"),
    (f"g-media-{U}", "full"),
    (f"snap-asset-raw-media-{U}", "full"),
    (f"snap-overlay-{U}", "overlay"),
    (f"snap-rendered-lowres-{U}", "rendered"),
    (f"snap-thumbnail-{U}", "thumbnail"),
    (f"{U}_memories_backup_transcoded", "transcoded"),
])
def test_memory_claim_shapes_are_recognised(ek, role):
    uuid, category, got = memories_report.classify_snap_claim(ek)
    assert (uuid, got) == (U, role)
    assert category.startswith("Memory")


@pytest.mark.parametrize("ek", [
    f"https://bolt-gcdn.sc-cdn.net/previewmedia/{U}",
    f"https://bolt-gcdn.sc-cdn.net/previewmedia/preview_thumbnail/{U}",
    f"https://geofilter.storage.googleapis.com/png/{U}",
    f"1:{U}:28:0:0",                                # a chat message part
    f"content~1:{U}:28:0:0",
    f"resumable-data-SOMEUSER~{U}",
    f"PH-{U}",
    U,                                              # a bare UUID claims nothing about a Memory
])
def test_shapes_that_do_not_name_a_memory_are_not_indexed(ek):
    assert memories_report.classify_snap_claim(ek)[0] is None


def test_both_reports_agree_on_every_shape():
    """The two reports link in opposite directions; disagreeing means the examiner sees a cached
    file tied to a Memory that the Memory's own page does not list (or the reverse)."""
    for ek in (f"snap-media-{U}", f"{U}_memories_backup_transcoded",
               f"https://bolt-gcdn.sc-cdn.net/previewmedia/{U}", f"1:{U}:28:0:0"):
        mine = memories_report.classify_snap_claim(ek)[0]
        theirs = cc_report.classify_external_key(ek, 19)[1]
        assert mine == theirs


def test_the_preview_bucket_still_labels_preview_urls():
    """Dropping them as Memory claims must not drop them from the report altogether."""
    assert cc_report.classify_external_key(
        f"https://bolt-gcdn.sc-cdn.net/previewmedia/{U}", 19)[0] == "Preview"
