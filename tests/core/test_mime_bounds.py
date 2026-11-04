"""Bounds on MIME and attachment work: limits, refusal, cancellation, cleanup."""

import base64
import dataclasses
import errno
import json
import os
import stat
import subprocess
import tracemalloc
from unittest.mock import patch

import pytest

from postcard.core.lifecycle import Lifecycle
from postcard.core.mime.message_parser import (
    MAX_ATTACHMENT_BYTES,
    MAX_HEADER_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_MIME_PARTS,
    AttachmentTooLargeError,
    MessageTooLargeError,
    MimeLimitError,
    ParseCancelledError,
    TooManyPartsError,
    parse_headers,
    parse_message,
    write_file_atomically,
)
from postcard.core.net.tuta_session import (
    ERROR_CODES,
    MAX_IPC_RESPONSE_BYTES,
    TutaError,
    helper_transport,
)

MiB = 1024 * 1024


def sized_message(total: int, parts: int = 1) -> bytes:
    payload_size = int(total / 1.36 / parts) - 4096
    chunks = [
        b"From: probe@example.com\nSubject: sized\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n'
        b"--B\nContent-Type: text/plain\n\nbody\n"
    ]
    for index in range(parts):
        payload = bytes((index + offset) % 251 for offset in range(payload_size))
        chunks.append(
            b"--B\nContent-Type: application/octet-stream\n"
            b"Content-Transfer-Encoding: base64\n"
            b'Content-Disposition: attachment; filename="big%d.bin"\n\n'
            % index
            + base64.encodebytes(payload)
        )
    chunks.append(b"--B--\n")
    return b"".join(chunks)


def test_synthetic_messages_parse_within_a_bounded_peak():
    for size in (1 * MiB, 10 * MiB, 50 * MiB):
        raw = sized_message(size, parts=2)
        assert size * 0.9 < len(raw) < size

        tracemalloc.start()
        parsed = parse_message(raw)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert [a.filename for a in parsed.attachments] == ["big0.bin", "big1.bin"]
        assert parsed.text_body == "body"
        # Measured ~6.9x on CPython 3.12 (stdlib email tree plus one decode);
        # the bound guards against copy multiplication, not the tree itself.
        assert peak < 10 * len(raw)


def test_a_message_one_byte_over_the_limit_is_refused_not_truncated():
    header = b"Subject: padded\n\n"
    raw = header + b"x" * (MAX_MESSAGE_BYTES - len(header) + 1)
    assert len(raw) == MAX_MESSAGE_BYTES + 1
    with pytest.raises(MessageTooLargeError) as caught:
        parse_message(raw)
    assert caught.value.size_bytes == len(raw)
    assert caught.value.limit_bytes == MAX_MESSAGE_BYTES
    assert str(len(raw)) in str(caught.value)


def test_a_message_exactly_at_the_limit_still_parses():
    header = b"Subject: padded\n\n"
    raw = header + b"x" * (MAX_MESSAGE_BYTES - len(header))
    assert len(raw) == MAX_MESSAGE_BYTES
    parsed = parse_message(raw)
    assert parsed.subject == "padded"
    assert parsed.text_body == "x" * (MAX_MESSAGE_BYTES - len(header))


def test_many_small_attachments_parse_but_a_flood_is_refused():
    chunks = [
        b"From: a@x\nSubject: many\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n'
    ]
    for index in range(MAX_MIME_PARTS):
        chunks.append(
            b"--B\nContent-Type: application/octet-stream\n"
            b"Content-Transfer-Encoding: base64\n"
            b'Content-Disposition: attachment; filename="f%d.bin"\n\naGk=\n' % index
        )
    chunks.append(b"--B--\n")

    with pytest.raises(TooManyPartsError) as caught:
        parse_message(b"".join(chunks))
    assert caught.value.limit == MAX_MIME_PARTS
    assert caught.value.part_count > MAX_MIME_PARTS

    ok_raw = b"".join(chunks[: 1 + 300] + chunks[-1:])
    assert len(parse_message(ok_raw).attachments) == 300


def test_a_single_attachment_over_its_limit_refuses_the_whole_message():
    payload = b"y" * (MAX_ATTACHMENT_BYTES + MiB)
    raw = (
        b"From: a@x\nSubject: one big file\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n'
        b"--B\nContent-Type: text/plain\n\nbody\n"
        b"--B\nContent-Type: application/octet-stream\n"
        b"Content-Transfer-Encoding: base64\n"
        b'Content-Disposition: attachment; filename="huge.bin"\n\n'
        + base64.encodebytes(payload)
        + b"\n--B--\n"
    )
    assert len(raw) < MAX_MESSAGE_BYTES
    with pytest.raises(AttachmentTooLargeError) as caught:
        parse_message(raw)
    assert caught.value.filename == "huge.bin"
    assert caught.value.limit_bytes == MAX_ATTACHMENT_BYTES
    # The refusal UX catches one base class for every size refusal.
    assert isinstance(caught.value, MessageTooLargeError)
    assert isinstance(caught.value, MimeLimitError)


def test_malformed_parts_are_kept_never_dropped():
    truncated = (
        b"From: a@x\nSubject: cut off\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n'
        b"--B\nContent-Type: text/plain\n\nbody\n"
        b"--B\nContent-Type: application/octet-stream\n"
        b"Content-Transfer-Encoding: base64\n"
        b'Content-Disposition: attachment; filename="cut.bin"\n\n'
        b"aGk"  # truncated base64, no closing boundary
    )
    assert [a.filename for a in parse_message(truncated).attachments] == ["cut.bin"]

    unknown_charset = (
        b'Content-Type: text/plain; charset="x-never-heard-of-it"\n\ncaf\xe9\n'
    )
    assert "caf" in parse_message(unknown_charset).text_body

    unknown_encoding = (
        b"From: a@x\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n'
        b"--B\nContent-Type: application/octet-stream\n"
        b"Content-Transfer-Encoding: x-rot13\n"
        b'Content-Disposition: attachment; filename="weird.bin"\n\nhello\n'
        b"--B--\n"
    )
    (kept,) = parse_message(unknown_encoding).attachments
    assert kept.filename == "weird.bin"
    assert kept.content


def test_deeply_nested_and_forwarded_parts_do_not_crash():
    depth = 300
    nested = b"tip\n"
    for _ in range(depth):
        nested = (
            b'MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary="N"\n\n'
            b"--N\nContent-Type: text/plain\n\nnested\n--N\n"
            b"Content-Type: message/rfc822\n\n" + nested + b"--N--\n"
        )
    raw = b"From: a@x\nSubject: deep\n" + nested
    assert parse_message(raw).subject == "deep"

    forwarded = (
        b"From: a@x\nSubject: fwd\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n'
        b"--B\nContent-Type: text/plain\n\nsee attached\n"
        b"--B\nContent-Type: message/rfc822\n"
        b'Content-Disposition: attachment; filename="inner.eml"\n\n'
        b"From: inner@x\nSubject: inner\n\ninner body\n"
        b"--B--\n"
    )
    (attachment,) = parse_message(forwarded).attachments
    assert isinstance(attachment.content, bytes)
    assert b"inner body" in attachment.content


def test_parse_results_are_frozen_for_the_trip_to_the_main_loop():
    parsed = parse_message(b"Subject: hi\n\nbody\n")
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.subject = "changed"  # type: ignore[misc]


def test_cancellation_before_and_during_a_parse():
    raw = sized_message(MiB)
    with pytest.raises(ParseCancelledError):
        parse_message(raw, is_cancelled=lambda: True)
    assert parse_message(raw, is_cancelled=lambda: False).subject == "sized"

    seen = 0

    def cancel_after_five() -> bool:
        nonlocal seen
        seen += 1
        return seen > 5

    many = sized_message(MiB, parts=50)
    with pytest.raises(ParseCancelledError):
        parse_message(many, is_cancelled=cancel_after_five)


def test_headers_parse_without_touching_an_over_limit_body():
    raw = sized_message(10 * MiB)
    headers = parse_headers(raw)
    assert headers.subject == "sized"
    assert headers.from_display == "probe@example.com"
    assert headers.text_body is None
    assert headers.attachments == []

    oversized = b"Subject: padded\nX-Pad: " + b"p" * MAX_HEADER_BYTES + b"\n\nbody\n"
    with pytest.raises(MessageTooLargeError) as caught:
        parse_headers(oversized)
    assert caught.value.limit_bytes == MAX_HEADER_BYTES


def test_an_account_removed_mid_parse_drops_its_completion():
    lifecycle = Lifecycle()
    token = lifecycle.mint(7)
    assert lifecycle.is_live(token)

    lifecycle.invalidate_account(7)
    assert not lifecycle.is_live(token)


def test_a_readded_account_mints_live_tokens_while_old_ones_stay_dead():
    lifecycle = Lifecycle()
    stale = lifecycle.mint(7)
    lifecycle.invalidate_account(7)

    fresh = lifecycle.mint(7)
    assert lifecycle.is_live(fresh)
    assert not lifecycle.is_live(stale)


def test_a_closed_window_drops_every_parse_completion():
    lifecycle = Lifecycle()
    first = lifecycle.mint(7)
    second = lifecycle.mint(9)
    lifecycle.shutdown()
    assert not lifecycle.is_live(first)
    assert not lifecycle.is_live(second)
    assert not lifecycle.is_live(lifecycle.mint(7))


def test_atomic_write_roundtrips_with_owner_only_permissions(tmp_path):
    target = tmp_path / "saved.bin"
    write_file_atomically(target, b"bytes")
    assert target.read_bytes() == b"bytes"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600

    write_file_atomically(target, b"replaced")
    assert target.read_bytes() == b"replaced"


def test_atomic_write_failure_leaves_no_temp_and_keeps_the_target(tmp_path):
    target = tmp_path / "saved.bin"
    target.write_bytes(b"original")
    with (
        patch("os.fsync", side_effect=OSError(errno.ENOSPC, "No space left")),
        pytest.raises(OSError, match="No space left"),
    ):
        write_file_atomically(target, b"doomed")
    assert target.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["saved.bin"]


def test_atomic_write_refuses_a_read_only_directory(tmp_path):
    directory = tmp_path / "locked"
    directory.mkdir()
    target = directory / "saved.bin"
    with (
        patch(
            "tempfile.NamedTemporaryFile",
            side_effect=PermissionError(errno.EACCES, "Permission denied"),
        ),
        pytest.raises(PermissionError),
    ):
        write_file_atomically(target, b"doomed")
    assert list(directory.iterdir()) == []


def test_atomic_write_never_falls_back_to_another_location(tmp_path):
    target = tmp_path / "missing-dir" / "saved.bin"
    with pytest.raises(FileNotFoundError):
        write_file_atomically(target, b"doomed")
    assert list(tmp_path.iterdir()) == []


def test_an_oversized_helper_reply_is_refused_before_json_parses():
    oversized = " " * (MAX_IPC_RESPONSE_BYTES + 1)
    completed = subprocess.CompletedProcess([], 0, oversized)
    with (
        patch("shutil.which", return_value="/app/bin/postcard-tuta"),
        patch("subprocess.run", return_value=completed),
        pytest.raises(TutaError) as caught,
    ):
        helper_transport({"op": "sync"})
    assert caught.value.code == "response_too_large"
    assert caught.value.code in ERROR_CODES


def test_a_normal_sized_helper_reply_still_parses():
    body = {"version": 1, "result": {"pad": "x" * MiB}}
    completed = subprocess.CompletedProcess([], 0, json.dumps(body))
    with (
        patch("shutil.which", return_value="/app/bin/postcard-tuta"),
        patch("subprocess.run", return_value=completed),
    ):
        assert helper_transport({"op": "sync"}) == body


def test_an_oversized_reply_leaks_nothing_into_the_error():
    oversized = "secret-token " + "x" * MAX_IPC_RESPONSE_BYTES
    completed = subprocess.CompletedProcess([], 0, oversized)
    with (
        patch("shutil.which", return_value="/app/bin/postcard-tuta"),
        patch("subprocess.run", return_value=completed),
        pytest.raises(TutaError) as caught,
    ):
        helper_transport({"op": "sync"})
    assert "secret-token" not in str(caught.value)
    assert "secret-token" not in caught.value.code
