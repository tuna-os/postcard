#!/usr/bin/env python3
"""Synthetic mailbox benchmark for issue #14 (query budgets).

Builds seeded mailboxes at 1k/10k/50k mails and times the main-loop work a
sync and a list refresh perform: the batched sync write, the rethread (fresh
and unchanged), a first-page conversation query versus the full list, and a
first-page search versus the full search. Each size runs in its own process
so the peak-RSS numbers don't leak across sizes.

Usage:
    python3 tests/bench/bench_mailbox.py [--size 1000|10000|50000] [--out FILE]

With no flags every size runs, a table prints to stdout, and the JSON record
(size, runtime, hardware) follows it. --out writes that record to a file
instead of printing it. --strict exits nonzero when a budget is missed.

Budgets live in BUDGETS below and mirror the table in
tests/core/store/test_query_budgets.py; both were set from a measured run
(see the hardware field of that run) with headroom for slower machines.
"""

import argparse
import json
import os
import platform
import random
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src")
)

from postcard.core.models.message_header import MessageHeader  # noqa: E402
from postcard.core.store.database import Database  # noqa: E402

SEED = 1403
SIZES = (1000, 10000, 50000)
SYNC_PAGE = 50
LIST_PAGE = 200
SEARCH_WORD = "lighthouse"

# Main-loop ceilings, in milliseconds unless named, at the 50k binding size
# (plus peak RSS for the whole 50k run). Set from the 2026-11-04 baseline on
# Linux x86_64, 12 CPUs, 24 GB RAM, SQLite 3.54.1 via SQLCipher -- that run
# measured batch50 1.2ms, seq50 9.8ms, rethread 292/17.7ms, page/full
# 124/627ms, search page/full 53/138ms, peak RSS 116 MB -- with ~4x headroom;
# the script re-checks them on every run. Mirrored in
# tests/core/store/test_query_budgets.py.
BUDGETS = {
    "sync_page_batch_ms": 5.0,
    "sync_page_sequential_ms": 40.0,
    "rethread_changed_ms": 1200.0,
    "rethread_unchanged_ms": 75.0,
    "conversations_page_ms": 500.0,
    "conversations_full_ms": 2500.0,
    "search_page_ms": 250.0,
    "search_full_ms": 600.0,
    "rss_peak_mb": 500.0,
}

_SUBJECT_WORDS = [
    "lighthouse",
    "dinner",
    "invoice",
    "trip",
    "photos",
    "roadmap",
    "budget",
    "launch",
    "review",
    "notes",
]


def _hardware() -> dict:
    info = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
    }
    try:
        import sqlcipher3  # noqa: F401

        info["sqlcipher"] = True
    except ImportError:
        info["sqlcipher"] = False
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    info["mem_total_kb"] = int(line.split()[1])
                    break
    except OSError:
        pass
    return info


def _headers(rng: random.Random, folder_tag: str, start: int, count: int):
    """Deterministic sync-page headers; every fourth mail answers its parent."""
    headers = []
    for index in range(start, start + count):
        uid = str(index)
        mid = f"<{folder_tag}-{index}@bench>"
        base = f"{rng.choice(_SUBJECT_WORDS)} {rng.choice(_SUBJECT_WORDS)} #{index}"
        if index % 4 == 0 and index > start:
            parent = index - 1
            headers.append(
                MessageHeader(
                    uid=uid,
                    sender="Bob <bob@example.com>",
                    sender_address="bob@example.com",
                    recipient="",
                    recipient_address="",
                    subject=f"Re: thread {folder_tag} {parent}",
                    date="2026-10-01T12:00:00+00:00",
                    is_unread=bool(index % 2),
                    preview="reply here",
                    message_id=mid,
                    in_reply_to=f"<{folder_tag}-{parent}@bench>",
                    references=f"<{folder_tag}-{parent}@bench>",
                )
            )
        else:
            preview = "see the light" if index % 7 == 0 else "plain mail"
            headers.append(
                MessageHeader(
                    uid=uid,
                    sender="Ada <ada@example.com>",
                    sender_address="ada@example.com",
                    recipient="",
                    recipient_address="",
                    subject=f"{base} {SEARCH_WORD}" if index % 11 == 0 else base,
                    date="2026-10-01T12:00:00+00:00",
                    is_unread=bool(index % 3),
                    preview=f"{preview} {SEARCH_WORD}" if index % 13 == 0 else preview,
                    message_id=mid,
                )
            )
    return headers


def _open() -> Database:
    try:
        import sqlcipher3  # noqa: F401

        path = os.path.join(tempfile.mkdtemp(prefix="bench-postcard-"), "bench.db")
        return Database(path, key=b"x" * 32)
    except ImportError:
        return Database(":memory:")


def _timed_median_ms(calls, repeats=3) -> float:
    samples = []
    for call in calls[:repeats]:
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def _run_size(mails: int) -> dict:
    rng = random.Random(SEED)
    db = _open()
    account = db.save_account("me@example.com", "Me", "imap.x", 993, "smtp.x", 587)
    folders = {
        name: db.get_or_create_folder(account.id, name)
        for name in ("INBOX", "Sent", "Archive")
    }
    shares = (("INBOX", 0.7), ("Sent", 0.2), ("Archive", 0.1))
    for name, share in shares:
        folder = folders[name]
        total = int(mails * share)
        for start in range(1, total + 1, 1000):
            chunk = _headers(rng, name.lower(), start, min(1000, total - start + 1))
            db.save_incoming_emails(folder.id, chunk)
    for folder in folders.values():
        db.reassign_conversations(folder.id)
    inbox = folders["INBOX"].id
    inbox_total = int(mails * 0.7)

    sequential_calls = [
        lambda n=n: [
            db.save_incoming_email(inbox, header)
            for header in _headers(rng, f"seq{n}", 10_000_000 + n * 1000, SYNC_PAGE)
        ]
        for n in range(3)
    ]
    batch_calls = [
        lambda n=n: db.save_incoming_emails(
            inbox, _headers(rng, f"bat{n}", 20_000_000 + n * 1000, SYNC_PAGE)
        )
        for n in range(3)
    ]
    # The batch calls run first so the sequential ones don't warm the cache.
    batch_ms = _timed_median_ms(batch_calls)
    sequential_ms = _timed_median_ms(sequential_calls)

    start = time.perf_counter()
    db.reassign_conversations(inbox)
    changed_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    skipped = not db.reassign_conversations(inbox)
    unchanged_ms = (time.perf_counter() - start) * 1000
    assert skipped, "second rethread should skip as unchanged"

    folder_ids = [inbox]
    page_ms = _timed_median_ms(
        [lambda: db.conversations_in_folders(folder_ids, limit=LIST_PAGE)] * 3
    )
    search_page_ms = _timed_median_ms(
        [lambda: db.search_conversations(folder_ids, SEARCH_WORD, limit=LIST_PAGE)] * 3
    )
    # Peak before the full-list builds below, so the record shows what paging
    # saves over hydrating the whole folder.
    after_page_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    full_ms = _timed_median_ms([lambda: db.conversations_in_folders(folder_ids)] * 3)
    search_full_ms = _timed_median_ms(
        [lambda: db.search_conversations(folder_ids, SEARCH_WORD)] * 3
    )

    statements: dict[str, int] = {}
    count = 0

    def trace(_sql: str) -> None:
        nonlocal count
        count += 1

    db._conn.set_trace_callback(trace)
    count = 0
    db.conversations_in_folders(folder_ids, limit=LIST_PAGE)
    statements["conversations_page"] = count
    count = 0
    db.conversations_in_folders(folder_ids)
    statements["conversations_full"] = count
    db._conn.set_trace_callback(None)

    threads = len(db.conversations_in_folders(folder_ids))
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    db.close()
    return {
        "mails": mails,
        "threads": threads,
        "inbox_mails": inbox_total + 2 * 3 * SYNC_PAGE,
        "ops_ms": {
            "sync_page_batch": round(batch_ms, 2),
            "sync_page_sequential": round(sequential_ms, 2),
            "rethread_changed": round(changed_ms, 2),
            "rethread_unchanged": round(unchanged_ms, 3),
            "conversations_page": round(page_ms, 2),
            "conversations_full": round(full_ms, 2),
            "search_page": round(search_page_ms, 2),
            "search_full": round(search_full_ms, 2),
        },
        "statements": statements,
        "rss_after_page_kb": after_page_kb,
        "rss_peak_kb": peak_kb,
    }


def _check_budgets(run: dict) -> list[str]:
    checks = []
    ops = run["ops_ms"]
    pairs = [
        ("sync_page_batch_ms", ops["sync_page_batch"]),
        ("sync_page_sequential_ms", ops["sync_page_sequential"]),
        ("rethread_changed_ms", ops["rethread_changed"]),
        ("rethread_unchanged_ms", ops["rethread_unchanged"]),
        ("conversations_page_ms", ops["conversations_page"]),
        ("conversations_full_ms", ops["conversations_full"]),
        ("search_page_ms", ops["search_page"]),
        ("search_full_ms", ops["search_full"]),
        ("rss_peak_mb", run["rss_peak_kb"] / 1024),
    ]
    for name, value in pairs:
        budget = BUDGETS[name]
        status = "PASS" if value <= budget else "FAIL"
        checks.append(f"{name}: {value:.2f} <= {budget:.2f} {status}")
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="mailbox query budgets, issue #14")
    parser.add_argument("--size", type=int, choices=SIZES, default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    if args.size is not None:
        print(json.dumps(_run_size(args.size)))
        return 0

    runs = []
    for size in SIZES:
        completed = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--size", str(size)],
            capture_output=True,
            text=True,
            check=True,
        )
        runs.append(json.loads(completed.stdout))
    record = {
        "benchmark": "postcard issue #14 query budgets",
        "seed": SEED,
        "hardware": _hardware(),
        "runs": runs,
        "budgets": BUDGETS,
    }

    names = [
        ("sync_page_batch", "batch50"),
        ("sync_page_sequential", "seq50"),
        ("rethread_changed", "re-ch"),
        ("rethread_unchanged", "re-un"),
        ("conversations_page", "page"),
        ("conversations_full", "full"),
        ("search_page", "spage"),
        ("search_full", "sfull"),
    ]
    print(
        f"{'mails':>7} {'threads':>8} "
        + " ".join(f"{short:>10}" for _, short in names)
        + f" {'rss_mb':>8}"
    )
    for run in runs:
        cells = " ".join(f"{run['ops_ms'][name]:>10.2f}" for name, _ in names)
        peak_mb = run["rss_peak_kb"] / 1024
        print(f"{run['mails']:>7} {run['threads']:>8} {cells} {peak_mb:>8.1f}")
    print(f"statements per query: {runs[-1]['statements']}")
    print("budgets at 50k:")
    failed = False
    for line in _check_budgets(runs[-1]):
        print(f"  {line}")
        failed = failed or line.endswith("FAIL")
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(record, handle, indent=2)
    else:
        print(json.dumps(record))
    if failed and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
