"""Partial-media detection in the Memories report.

An iOS cache holds only the byte ranges the device actually streamed, so a Memory's media is
routinely on disk in incomplete form. These tests pin the three ways that is detected — a missing
PKCS#7 tail, a hole between byte-range shards, and a pack whose header declares more payload than
is present — plus the two changes that hang off it: the pack-matching probe (which must accept and
reject exactly what a full decrypt would) and the OS-level suppression of FFmpeg's decoder noise.

Every input here is synthetic. No extraction data is required or used.
"""
import os

from Crypto.Cipher import AES

from scripts import memories_media_report as memories_report

KEY = bytes(range(32))
IV = bytes(range(16))
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x22" * 3000


def _pkcs7(data):
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _encrypt(plain):
    return AES.new(KEY, AES.MODE_CBC, IV).encrypt(plain)


def _pack(payload):
    header = memories_report.PACK_HEADER_MARKER + len(payload).to_bytes(4, "little")
    return _encrypt(_pkcs7(header + payload))


def _part(tmp_path, name, size):
    path = tmp_path / name
    path.write_bytes(b"\0" * size)
    return str(path)


# --------------------------------------------------------------- SCContent completeness

def test_complete_sccontent_file_reports_a_valid_padding_tail():
    _, stripped, ext, tail_ok = memories_report.decrypt_sccontent(_encrypt(_pkcs7(PNG)), KEY, IV)
    assert (ext, tail_ok, stripped) == ("png", True, PNG)


def test_truncated_sccontent_file_still_decrypts_but_is_flagged():
    cipher = _encrypt(_pkcs7(PNG))
    padded, _, ext, tail_ok = memories_report.decrypt_sccontent(cipher[:-160], KEY, IV)
    assert ext == "png" and tail_ok is False and padded


def test_unaligned_truncation_recovers_the_block_aligned_prefix():
    """A partial cache whose length is not a multiple of 16 used to be discarded outright."""
    cipher = _encrypt(_pkcs7(PNG))
    padded, _, ext, tail_ok = memories_report.decrypt_sccontent(cipher[:-7], KEY, IV)
    assert ext == "png" and tail_ok is False
    assert len(padded) >= len(cipher) - 32


def test_plaintext_file_reports_completeness_as_unknown():
    """No padding to inspect, so we must not claim the file is either complete or truncated."""
    _, _, ext, tail_ok = memories_report.decrypt_sccontent(PNG, KEY, IV)
    assert ext == "png" and tail_ok is None


# --------------------------------------------------------------- shard coverage

def test_contiguous_shards_leave_no_gap(tmp_path):
    parts = [(0, _part(tmp_path, "ck_0-1000", 1000)), (1000, _part(tmp_path, "ck_1000-3000", 2000))]
    coverage = memories_report._part_coverage(parts)
    assert coverage == {"gaps": [], "short": [], "bytes": 3000}


def test_hole_between_shards_is_located(tmp_path):
    parts = [(0, _part(tmp_path, "ck_0-1000", 1000)), (5000, _part(tmp_path, "ck_5000-6000", 1000))]
    assert memories_report._part_coverage(parts)["gaps"] == [(1000, 5000)]


def test_missing_head_reads_as_a_gap_at_offset_zero(tmp_path):
    parts = [(5000, _part(tmp_path, "ck_5000-6000", 1000))]
    assert memories_report._part_coverage(parts)["gaps"] == [(0, 5000)]


def test_shard_shorter_than_its_declared_range_is_flagged(tmp_path):
    parts = [(0, _part(tmp_path, "ck_0-9000", 100))]
    assert memories_report._part_coverage(parts)["short"] == ["ck_0-9000"]


def test_inclusive_end_naming_is_not_mistaken_for_a_short_shard(tmp_path):
    """`<start>-<end>` may be half-open or inclusive; neither convention is a defect."""
    parts = [(0, _part(tmp_path, "ck_0-999", 1000))]
    assert memories_report._part_coverage(parts)["short"] == []


def test_gaps_and_missing_tail_produce_an_examiner_facing_reason():
    complete, why = memories_report._sccontent_completeness({"gaps": [(1000, 5000)], "short": []},
                                                            True)
    assert complete is False and "4,000 bytes missing" in why

    complete, why = memories_report._sccontent_completeness(None, False)
    assert complete is False and "end of the file is not cached" in why

    assert memories_report._sccontent_completeness(None, True) == (True, "")
    assert memories_report._sccontent_completeness(None, None)[0] is None


# --------------------------------------------------------------- caching-media packs

def test_pack_reports_its_declared_payload_length():
    payload, ext, declared = memories_report.decrypt_pack(_pack(PNG), KEY, IV)
    assert (ext, declared, payload) == ("png", len(PNG), PNG)
    assert memories_report._pack_completeness(payload, declared) == (True, "")


def test_pack_missing_chunks_is_detected_from_the_declared_length():
    truncated = _pack(PNG)[:1600]
    payload, _, declared = memories_report.decrypt_pack(truncated, KEY, IV)
    complete, why = memories_report._pack_completeness(payload, declared)
    assert complete is False and "Partially cached" in why


def test_probe_matches_exactly_what_a_full_decrypt_would_accept():
    """The probe reads 32 bytes instead of the whole item; it must not change any verdict."""
    pack = _pack(PNG)
    wrong_key = bytes(range(1, 33))
    assert memories_report.pack_matches(pack[:32], KEY, IV) is True
    assert memories_report.pack_matches(pack[:32], wrong_key, IV) is False
    assert memories_report.pack_matches(pack[:32][:1600], KEY, IV) is True


def test_read_head_stops_once_it_has_enough_bytes(tmp_path):
    first = _part(tmp_path, "chunk-0.pack", 20)
    second = _part(tmp_path, "chunk-1.pack", 100)
    assert len(memories_report._read_head([first, second], 32)) == 32
    assert len(memories_report._read_head([first], 32)) == 20


# --------------------------------------------------------------- decoder noise

def test_quiet_stderr_suppresses_fd_level_writes_and_restores_them():
    """OpenCV's FFmpeg writes to fd 2 from C, so only an fd-level redirect silences it."""
    read_fd, write_fd = os.pipe()
    saved = os.dup(2)
    os.dup2(write_fd, 2)
    try:
        with memories_report._quiet_stderr():
            os.write(2, b"decoder noise")
        os.write(2, b"after")
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(write_fd)
    written = os.read(read_fd, 1000)
    os.close(read_fd)
    assert written == b"after"
