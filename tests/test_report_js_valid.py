"""Every script a report emits must actually parse as JavaScript.

The reports' JS is written inside Python strings, and several of those are f-strings — so a brace in
the JS has to be doubled. Get that wrong and Python raises at import, which the rest of the suite
catches. Get the *other* case wrong — valid Python that emits broken JS — and nothing here notices:
the report opens to an empty table, or a control silently does nothing, because a browser reports a
syntax error only to its own console.

So this parses what each report actually writes. It needs `node` on PATH and skips without it, which
means it is a guard rather than a gate — but it is the only check in the suite that reads the output
the way a browser does.

Every input is synthetic: empty or placeholder rows, no extraction data. The shells are what matter;
that is where the inline `SCV.init({…})` lives.
"""
import os
import re
import shutil
import subprocess

import pytest

from scripts import (cache_controller_report, cache_media_report, contacts_report,
                     conversations_report, memories_media_report, report_ui)


NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _check_js(source, label):
    """Parse *source* with node, failing with the reported line when it does not."""
    proc = subprocess.run([NODE, "--check", "-"], input=source, text=True,
                          encoding="utf-8", capture_output=True)
    if proc.returncode:
        numbered = "\n".join(f"{n:4d} | {line}"
                             for n, line in enumerate(source.splitlines(), 1))
        pytest.fail(f"{label} is not valid JavaScript:\n{proc.stderr}\n\n{numbered}")


def _check_inline_scripts(html_path):
    """Parse every inline `<script>` in a rendered report page."""
    with open(html_path, encoding="utf-8") as fh:
        doc = fh.read()
    blocks = [b for b in _SCRIPT.findall(doc) if b.strip()]
    assert blocks, f"{html_path} has no inline script to check"
    for n, block in enumerate(blocks):
        _check_js(block, f"{os.path.basename(html_path)} inline script #{n + 1}")
    return len(blocks)


# --------------------------------------------------------------------------- the shared bundle

@needs_node
@pytest.mark.parametrize("name", ["SELECT_JS", "VTABLE_JS", "HINT_JS", "NAV_JS",
                                  "SELECT_TOOLBAR_JS"])
def test_each_shared_script_parses_on_its_own(name):
    _check_js(getattr(report_ui, name), f"report_ui.{name}")


@needs_node
def test_the_bundle_the_conversations_report_writes_parses(tmp_path):
    """`assets/ui.js` is one file several pages load, so a fault in any part breaks all of them."""
    conversations_report.write_assets(str(tmp_path))
    with open(tmp_path / "assets" / "ui.js", encoding="utf-8") as fh:
        _check_js(fh.read(), "Conversations/assets/ui.js")


# --------------------------------------------------------------------------- the report shells

def _contact():
    return {"display": "Alice Test", "username": "alice-test", "user_id": "u-0001",
            "legacy_username": "", "conv_id": "aaaa0000-0000-4000-8000-000000000001",
            "is_owner": False, "added": "", "added_sort": 0, "kind": "Friend",
            "raw": "alice-test", "label": "Alice Test (alice-test)"}


@needs_node
def test_the_contacts_shell_parses(tmp_path):
    path = contacts_report.generate_report([_contact()], str(tmp_path / "Contacts"),
                                           run_id="RUN-1")
    assert _check_inline_scripts(path)


@needs_node
def test_the_library_caches_shell_parses(tmp_path):
    path, _stats = cache_media_report.generate_report(
        [], [], str(tmp_path / "CacheMedia"), "UTC", "../", {"note": ""},
        {"decoded": 0, "files": 0, "bytes": 0}, "app")
    assert _check_inline_scripts(path)


@needs_node
def test_the_cache_controller_shell_parses(tmp_path):
    path, _stats = cache_controller_report.generate_report(
        [], [], str(tmp_path / "CacheController"), "UTC", "../", None, {},
        "cache_controller.db")
    assert _check_inline_scripts(path)


@needs_node
def test_the_memories_shell_parses(tmp_path):
    path, _linked, _located = memories_media_report.generate_report(
        {}, str(tmp_path / "Memories"), False, run_id="RUN-1")
    assert _check_inline_scripts(path)


# --------------------------------------------------------------------------- the store, actually run
#
# `node` lets us do more than parse: the selection store is plain ES5 over `window`/`document`, so it
# can be driven for real with a stub of each. These are the only behavioural tests of it — the Python
# suite can assert what the source says, but not what it does.

_HARNESS = """
const store = {};
global.window = {SCAUTO_RUN: 'R1', SCAUTO_VERSION: '9.9.9', addEventListener() {},
  localStorage: {getItem: k => (k in store ? store[k] : null),
                 setItem: (k, v) => { store[k] = v; }}};
global.document = {addEventListener() {}, querySelectorAll: () => []};
global.__stash = (obj) => { store['scauto-sel:R1'] = JSON.stringify(obj); };
"""


def _run_store_js(body):
    """Run *body* against a stubbed browser with SCSel loaded; return its stdout."""
    source = _HARNESS + report_ui.SELECT_JS + body
    proc = subprocess.run([NODE, "-"], input=source, text=True, encoding="utf-8",
                          capture_output=True)
    if proc.returncode:
        pytest.fail(f"the selection store failed to run:\n{proc.stderr}")
    return proc.stdout


@needs_node
def test_a_bare_message_id_is_not_counted_and_not_saved():
    out = _run_store_js("""
      SCSel.preload({schema: 1, selections: {msg: {'msg-12.0': 1, 'conv-A|msg-3.0': 1}}});
      console.log('kept=' + JSON.stringify(SCSel.ids('msg')));
      console.log('quarantined=' + JSON.stringify(SCSel.legacy('msg')));
      console.log('total=' + SCSel.total());
    """)
    assert 'kept=["conv-A|msg-3.0"]' in out
    assert 'quarantined=["msg-12.0"]' in out
    assert "total=1" in out                      # the unattributable one is not part of the selection


@needs_node
def test_a_localstorage_stash_from_an_older_build_cannot_smuggle_bare_ids_back_in():
    """The same-tab safety net is a second route into the store, and it has to be held to the same
    rule — a stash written before message ids were qualified must not reinstate them."""
    out = _run_store_js("""
      __stash({saved: '2030-01-01T00:00:00Z', dirty: true,
               selections: {msg: {'msg-12.0': 1, 'conv-A|msg-3.0': 1}, mem: {'mem-S1': 1}}});
      SCSel.preload({schema: 2, exported: '2026-01-01T00:00:00Z', selections: {}});
      console.log('kept=' + JSON.stringify(SCSel.ids('msg')));
      console.log('quarantined=' + JSON.stringify(SCSel.legacy('msg')));
      console.log('total=' + SCSel.total());
    """)
    assert 'kept=["conv-A|msg-3.0"]' in out
    assert 'quarantined=["msg-12.0"]' in out
    assert "total=2" in out                      # the qualified message and the Memory


@needs_node
def test_a_tick_records_its_key_record_and_a_save_carries_the_provenance():
    out = _run_store_js("""
      SCSel.preload({schema: 2, selections: {}});
      SCSel.set('cm', 'cm-aaa', true, {sha: 'aaa', raw: ['bbb'], rel: 'Library/Caches/x'});
      SCSel.set('mem', 'mem-S1', true);
      console.log('keys=' + JSON.stringify(SCSel.keys('cm', 'cm-aaa')));
      console.log('nokeys=' + JSON.stringify(SCSel.keys('mem', 'mem-S1')));
      console.log('schema=' + SCSel.schema());
    """)
    assert '"raw":["bbb"]' in out
    assert "nokeys=null" in out                  # a bare tick stays a bare 1, not a fake record
    assert "schema=2" in out


@needs_node
def test_clear_and_count_can_be_scoped_to_one_conversation():
    """Message ids are qualified, so a conversation page's Clear must not empty every chat."""
    out = _run_store_js("""
      SCSel.preload({schema: 2, selections: {msg: {'conv-A|msg-1.0': 1, 'conv-A|msg-2.0': 1,
                                                   'conv-B|msg-1.0': 1}}});
      console.log('inA=' + SCSel.count('msg', 'conv-A|'));
      console.log('all=' + SCSel.count('msg'));
      SCSel.clear('msg', 'conv-A|');
      console.log('after=' + JSON.stringify(SCSel.ids('msg')));
    """)
    assert "inA=2" in out and "all=3" in out
    assert 'after=["conv-B|msg-1.0"]' in out


# --------------------------------------------------------------------------- the selection contract
#
# These are the two pages whose selection ids are *not* their row anchors, which is the whole reason
# `selPrefix` exists. A broken closure here would leave message ticks colliding across chats again.

@needs_node
def test_a_conversation_page_parses_and_qualifies_its_message_ids(tmp_path):
    conv_id = "aaaa0000-0000-4000-8000-000000000001"
    conv = {
        "id": conv_id, "server_id": "", "title": "Test chat", "kind": "Private",
        "page": "pages/x.html", "participants": [], "senders": {}, "types": {},
        "messages": [], "n_messages": 0, "n_attachments": 0, "n_missing": 0,
        "n_files": 0, "n_wal_gone": 0,
        "kind_src": "", "title_src": "", "participants_src": "", "first_sort": 0, "last_sort": 0,
        "activity": {"first": "", "last": "", "source": "messages"},
    }
    conversations_report.write_assets(str(tmp_path))
    rel = conversations_report.render_conversation_page(conv, str(tmp_path), "UTC", "RUN-1")
    path = os.path.join(str(tmp_path), rel.replace("/", os.sep))
    assert _check_inline_scripts(path)

    with open(path, encoding="utf-8") as fh:
        doc = fh.read()
    # the store id is qualified with the conversation; the page anchor is not touched
    assert f'selPrefix:"conv-{conv_id}|"' in doc
    assert f'window.SCAUTO_SELPREFIX="conv-{conv_id}|"' in doc
    assert f'data-id="conv-{conv_id}"' in doc


@needs_node
def test_the_memory_subpage_parses(tmp_path):
    member = {
        "snap_id": "SNAP-0001", "user_hash": "h1", "is_meo": False, "media_type": 0,
        "ids": {"ZMEDIAID": "MEDIA-0001", "ZSNAPID": "SNAP-0001", "ZENTRYID": "ENTRY-0001"},
        "media_files": [], "latitude": None, "longitude": None, "created_sort": 0,
        "times": {}, "entry_times": {}, "snap_other": {}, "entry_other": {}, "urls": {},
        "camera": "", "duration": None, "format": "", "key": "", "iv": "",
        "has_location": False, "wal": None, "media_refs": [],
    }
    pages = str(tmp_path / "pages")
    rel = memories_media_report.render_subpage("g1", [member], pages, False, [], [], None, None,
                                               {}, "UTC", run_id="RUN-1")
    path = os.path.join(str(tmp_path), rel.replace("/", os.sep))
    assert _check_inline_scripts(path)
    with open(path, encoding="utf-8") as fh:
        doc = fh.read()
    # the per-member checkbox carries the identifiers a later run can re-find this Memory by
    assert "data-keys=" in doc and "MEDIA-0001" in doc
