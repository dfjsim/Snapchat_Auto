"""Splitting each generator into index() then render() must not change a full run.

Every generator now works in two halves so a partial run can decide what to include before anything
is decrypted or published. The risk that comes with that is a second code path: the filtered one could
drift from the full one, and the drift would show up as a report quietly missing rows.

Two properties are pinned here, per generator:

* **The identity element.** Rendering with a closure that includes *everything* must produce byte-for-
  byte what rendering with no closure produces. If those two ever differ, the filtered path is not the
  same path.
* **The filter.** Rendering with a closure over a subset must produce exactly that subset.

The corpus byte-diff is the other half of this gate (a full run before and after the split, differing
only in the two known timestamp lines); this file is what catches a drift without a 20 GB extraction.

Every input is synthetic.
"""
import os
import re

from scripts import contacts_report, partial_report


def _contact(user_id, username, display, conv_id=""):
    return {"display": display, "username": username, "user_id": user_id,
            "legacy_username": "", "conv_id": conv_id, "is_owner": False,
            "added": "", "added_sort": 0, "kind": "Friend",
            "raw": username, "label": f"{display} ({username})"}


class _FriendsFrame:
    """The smallest thing `normalize_contacts` accepts: a frame-like with the columns it looks for."""

    def __init__(self, rows):
        self._rows = rows
        self.columns = ["Display name", "Username", "User ID", "Conversation ID"]

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        for n, row in enumerate(self._rows):
            yield n, row

    def fillna(self, _value):
        return self

    def astype(self, _dtype):
        return self


def _friends(rows):
    return _FriendsFrame([
        {"Display name": display, "Username": username, "User ID": uid, "Conversation ID": conv}
        for uid, username, display, conv in rows])


CONTACTS = [("u-0001", "alice-test", "Alice Test", "aaaa0000-0000-4000-8000-000000000001"),
            ("u-0002", "bob-test", "Bob Test", "aaaa0000-0000-4000-8000-000000000002"),
            ("u-0003", "carol-test", "Carol Test", "")]


def _everything(stage):
    """A closure that includes every row of a stage — the identity element of the filter."""
    selection = {"schema": 2,
                 "selections": {stage.kind: {row_id: 1 for row_id in stage.sel.rows}}}
    indexes = {stage.kind: stage.sel}
    return partial_report.expand(indexes, partial_report.resolve(indexes, selection),
                                 {**partial_report.default_options(), "relations": {}})


def _subset(stage, row_ids):
    selection = {"schema": 2, "selections": {stage.kind: {row_id: 1 for row_id in row_ids}}}
    indexes = {stage.kind: stage.sel}
    return partial_report.expand(indexes, partial_report.resolve(indexes, selection),
                                 {**partial_report.default_options(), "relations": {}})


def _anchors(report_path):
    """The row anchors a rendered report's data file carries."""
    data = os.path.join(os.path.dirname(report_path), "data", "index.js")
    with open(data, encoding="utf-8") as fh:
        return re.findall(r'\["([^"]+)",\[', fh.read())


# --------------------------------------------------------------------------- Contacts

def _contacts_stage(tmp_path, name="Contacts"):
    outdir = str(tmp_path / name)
    stage = contacts_report.index(_friends(CONTACTS), outdir, report_dir=str(tmp_path),
                                  friends_source="app_group_plist_storage", identifiers={})
    return stage, outdir


def test_contacts_index_finds_every_contact_and_its_identifiers(tmp_path):
    stage, _outdir = _contacts_stage(tmp_path)
    assert len(stage.sel) == len(CONTACTS)
    # each contact is findable by user id and by username, not only by its anchor
    assert stage.sel.keys[("uid", "u-0001")] == {"ct-u-0001"}
    assert stage.sel.keys[("user", "bob-test")] == {"ct-u-0002"}


def test_contacts_render_with_an_all_inclusive_closure_matches_no_closure(tmp_path):
    """The identity element: the filtered path and the full path must be the same path."""
    stage_a, out_a = _contacts_stage(tmp_path, "A")
    full = contacts_report.render(stage_a, out_a)

    stage_b, out_b = _contacts_stage(tmp_path, "B")
    filtered = contacts_report.render(stage_b, out_b, closure=_everything(stage_b))

    assert open(full, encoding="utf-8").read() == open(filtered, encoding="utf-8").read()
    assert (open(os.path.join(out_a, "data", "index.js"), encoding="utf-8").read()
            == open(os.path.join(out_b, "data", "index.js"), encoding="utf-8").read())


def test_contacts_render_with_a_closure_writes_only_the_included_rows(tmp_path):
    stage, outdir = _contacts_stage(tmp_path)
    report = contacts_report.render(stage, outdir,
                                    closure=_subset(stage, ["ct-u-0001", "ct-u-0003"]))
    assert sorted(_anchors(report)) == ["ct-u-0001", "ct-u-0003"]


def test_contacts_main_is_still_one_call_and_renders_everything(tmp_path):
    outdir = str(tmp_path / "Contacts")
    report = contacts_report.main(_friends(CONTACTS), outdir, report_dir=str(tmp_path),
                                  friends_source="app_group_plist_storage", identifiers={})
    assert len(_anchors(report)) == len(CONTACTS)
