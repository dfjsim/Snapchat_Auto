"""The Snapchatters the app cached that are NOT contacts, and the three username fields.

``primary.docobjects``' ``snapchatter`` table holds every Snapchatter record the app has cached.
On the test devices the friends list explained a handful of its rows and every other row was named
in a ``snapchatters__displaysuggestion`` page — a Quick Add suggestion. A tool that reads that table
as the friends list reports those people as friends. This report lists them apart, says why the
app cached each, and keeps them out of the contacts table, out of the selection, and out of a
partial report.

The store here is synthetic: the tables the reader looks at, with FlatBuffers documents built by
``tests.test_flatbuffers_doc.build``.
"""
import os
import sqlite3

from scripts import contacts_report
from scripts.data import sqlite_open
from tests.test_flatbuffers_doc import build
from tests.test_index_render_split import _friends, _subset

OWNER = "00000000-0000-4000-8000-000000000000"
FRIEND = "11111111-1111-4111-8111-111111111111"
QUICK = "22222222-2222-4222-8222-222222222222"
FEED = "33333333-3333-4333-8333-333333333333"
NOBODY = "44444444-4444-4444-8444-444444444444"


def _store(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table snapchatter (rowid integer primary key autoincrement, p blob, userId string);
        create table index_snapchatterusername (rowid integer primary key, username string);
        create table index_snapchattermutableUsername (rowid integer primary key, mutableUsername string);
        create table index_snapchatterlegacyUsername (rowid integer primary key, legacyUsername string);
        create table snapchatters__displaysuggestion (rowid integer primary key autoincrement, p blob, page integer);
        create table friendsfeed__iteminfo (rowid integer primary key autoincrement, p blob, feedId string);
    """)
    people = [(OWNER, "owner", "owner", "owner", "Me"),
              (FRIEND, "friend1", "friend1", "friend1", "Friend One"),
              (QUICK, "stranger", "stranger", "stranger_old", "Stranger"),
              (FEED, "teamsnap", "teamsnap_m", "teamsnap", "Team Snap"),
              (NOBODY, "nobody", "nobody", "nobody", "No One")]
    for n, (uid, user, mutable, legacy, display) in enumerate(people, start=1):
        doc = build({0: uid, 1: user, 2: display, 14: mutable, 15: legacy})
        conn.execute("insert into snapchatter (rowid, p, userId) values (?, ?, ?)", (n, doc, uid))
        conn.execute("insert into index_snapchatterusername values (?, ?)", (n, user))
        conn.execute("insert into index_snapchattermutableUsername values (?, ?)", (n, mutable))
        conn.execute("insert into index_snapchatterlegacyUsername values (?, ?)", (n, legacy))
    # a suggestion page naming the stranger, and a feed item naming Team Snap — the UUID as text
    conn.execute("insert into snapchatters__displaysuggestion (p, page) values (?, 1)",
                 (b"\x00\x10" + QUICK.encode() + b"\x00 more",))
    conn.execute("insert into friendsfeed__iteminfo (p, feedId) values (?, 'f1')",
                 (b"feed:" + FEED.encode(),))
    conn.commit()
    conn.close()
    return path


def test_non_contacts_are_classified_by_the_table_that_names_them(tmp_path):
    rows = contacts_report.load_snapchatters(_store(os.path.join(tmp_path, "primary.docobjects")),
                                             contact_ids=[FRIEND], owner_user_id=OWNER)
    by_id = {r["user_id"]: r for r in rows}
    assert set(by_id) == {QUICK, FEED, NOBODY}                  # owner and friend excluded
    assert by_id[QUICK]["why"] == contacts_report.WHY_QUICK_ADD
    assert by_id[FEED]["why"] == "named in friendsfeed__iteminfo"
    assert by_id[NOBODY]["why"] == contacts_report.WHY_UNNAMED
    assert by_id[QUICK]["display_name"] == "Stranger"          # read from the FlatBuffers document
    assert by_id[QUICK]["legacy_username"] == "stranger_old"
    assert by_id[FEED]["mutable_username"] == "teamsnap_m"
    assert all(r["wal"] == sqlite_open.BOTH for r in rows)


def test_identifiers_carry_all_three_username_fields(tmp_path):
    ids = contacts_report.load_identifiers(_store(os.path.join(tmp_path, "primary.docobjects")))
    assert ids[FEED.lower()] == {"username": "teamsnap", "mutable_username": "teamsnap_m",
                                 "legacy_username": "teamsnap", "superseded": []}
    contacts = contacts_report.apply_identifiers(
        [{"user_id": FEED, "username": "", "display": "", "conv_id": "", "is_owner": False},
         {"user_id": QUICK, "username": "stranger", "display": "", "conv_id": "", "is_owner": False}],
        ids)
    assert contacts[0]["username"] == "teamsnap" and contacts[0]["mutable_differs"] is True
    assert contacts[0]["legacy_username"] == ""                 # equal to the username: no rename
    assert contacts[1]["legacy_username"] == "stranger_old" and contacts[1]["mutable_differs"] is False


def _stage(tmp_path, name, store):
    outdir = os.path.join(tmp_path, name, "Contacts")
    os.makedirs(outdir)
    return contacts_report.index(
        _friends([(OWNER, "owner", "Me", ""), (FRIEND, "friend1", "Friend One", "conv-1")]),
        outdir, owner_user_id=OWNER, friends_source="app_group_plist_storage",
        report_dir=os.path.join(tmp_path, name), primary=store,
        account={"username": "owner", "user_id": OWNER, "laguna_id": "l-1",
                 "identifier": "i-1", "encryption_key": "k" * 44,
                 "initialization_vector": "v" * 24}), outdir


def test_full_report_lists_them_apart_and_a_partial_report_leaves_them_out(tmp_path):
    store = _store(os.path.join(tmp_path, "primary.docobjects"))
    stage, outdir = _stage(tmp_path, "full", store)
    assert [r["user_id"] for r in stage["snapchatters"]] == [NOBODY, QUICK, FEED]  # by username
    assert len(stage.model) == 2                                # the contacts table is unchanged
    doc = open(contacts_report.render(stage, outdir), encoding="utf-8").read()
    assert 'class="others"' in doc and "Quick Add suggestion" in doc and "Stranger" in doc
    assert "3</b> Snapchatter(s) the app cached" in doc
    assert "ct-" + QUICK not in doc                             # no anchor: not a contact
    assert 'Mutable username' in doc and "teamsnap_m" in doc

    # the owner's expanded row carries the user.plist values, the friend's does not
    detail = open(os.path.join(outdir, "data", "detail-0.js"), encoding="utf-8").read()
    assert "Laguna ID" in detail and "l-1" in detail
    assert detail.count("Account — Documents/user.plist") == 1      # the owner only
    assert "index_snapchattermutableUsername" in detail

    stage_b, outdir_b = _stage(tmp_path, "partial", store)
    partial = open(contacts_report.render(stage_b, outdir_b, closure=_subset(stage_b, ["ct-" + FRIEND])),
                   encoding="utf-8").read()
    assert 'class="others"' not in partial and "Stranger" not in partial
    assert "not part of this extract" in partial
