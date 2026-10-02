"""Possible Memory: leads by time and kind — listed with every difference, never a link.

Every input is synthetic.
"""
from scripts import cache_controller_report as cc
from scripts import memory_leads as ml

T = 1_710_000_000.0                 # a file time; Memory times are offsets from it


def _files(kind="video", **points):
    return {"K": {"kind": kind, "points": [(label, T + d) for label, d in points.items()],
                  "ctx19": False}}


def test_the_window_and_the_kind():
    mems = {"A": {"kind": "video", "points": [("created", T + 600)]},       # on the edge
            "B": {"kind": "video", "points": [("created", T + 601)]},       # just outside
            "C": {"kind": "image", "points": [("created", T + 1)]}}         # wrong kind
    found = ml.find_leads(_files(claim=0), mems)["K"]
    assert [lead["snap_id"] for lead in found["leads"]] == ["A"] and found["in_window"] == 1
    assert ml.find_leads(_files(kind=None, claim=0), mems) == {}


def test_ranking_and_every_pair_listed():
    mems = {"FAR": {"kind": "video", "points": [("created", T + 300)]},
            "NEAR": {"kind": "video", "points": [("created", T - 2), ("captured", T + 500)]}}
    found = ml.find_leads(_files(claim=0, birth=-20), mems)["K"]
    assert [lead["snap_id"] for lead in found["leads"]] == ["NEAR", "FAR"]
    near = found["leads"][0]
    assert near["best_delta_s"] == -2 and len(near["pairs"]) == 4     # 2 file × 2 Memory times


def test_the_panel_says_what_it_is_and_never_links():
    entry = {"memory": None, "leads": {"leads": [{"snap_id": "S-1", "best_delta_s": 0.0,
                                                  "pairs": [{"file": "claim, context 19",
                                                             "memory": "ZGALLERYSNAP.ZCREATETIMEUTC",
                                                             "delta_s": 0.0}],
                                                  "kind": "video", "ctx19": True}],
                                       "in_window": 3, "window_s": 600}}
    html = cc._leads_html(entry, "../")
    assert "NOT proven" in html and "possible: S-1" in html and "scCopySnapIds" in html
    assert "context 19" in html and "3 Memory/Memories" in html
    assert entry["memory"] is None                                  # nothing turned into a link
    assert cc._leads_html({"leads": None}, "../") == ""


def test_the_basis_names_the_window():
    assert "10 minutes" in ml.basis(600) and "ZDURATION is not used" in ml.basis()
