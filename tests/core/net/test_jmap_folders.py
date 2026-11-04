import pytest

from postcard.core.models.folder import FolderRole
from postcard.core.net.jmap_folders import (
    build_tree,
    current_mailbox_state,
    list_mailboxes,
    mailbox_changes,
)
from postcard.core.net.jmap_session import (
    CODE_HTTP,
    JmapAnswer,
    JmapError,
    needs_full_sync,
)


def mailbox(id, name, role=None, parent=None, **counts):
    return {
        "id": id,
        "name": name,
        "parentId": parent,
        "role": role,
        "unreadEmails": counts.get("unread", 0),
        "totalEmails": counts.get("total", 0),
        "sortOrder": counts.get("order", 0),
    }


# A localized mailbox: the role field, not the name, says what it is for.
MAILBOXES = [
    mailbox("m-receipts", "Receipts", None, None, unread=2, total=9, order=5),
    mailbox("m-inbox", "Posteingang", "inbox", None, unread=3, total=40),
    mailbox("m-sent", "Gesendete Objekte", "sent", None, total=12),
    mailbox("m-2025", "2025", None, "m-receipts", total=4),
    mailbox("m-archive", "Archiv", "archive", None, total=100),
]


class FakeJmap:
    def __init__(self, *answers, account_id="A1"):
        self._answers = list(answers)
        self.account_id = account_id
        self.max_objects_in_get = 500
        self.calls = []

    def request(self, calls, cancelled=None):
        self.calls.append(calls)
        return [self._answers.pop(0) for _ in calls]


def mailboxes_answer(items):
    return JmapAnswer("Mailbox/get", {"list": items}, "0")


def test_mailboxes_come_back_parents_before_children():
    folders = list_mailboxes(FakeJmap(mailboxes_answer(MAILBOXES)))  # type: ignore[arg-type]

    assert [(f.id, f.parent_id) for f in folders] == [
        ("m-inbox", ""),
        ("m-sent", ""),
        ("m-archive", ""),
        ("m-receipts", ""),
        ("m-2025", "m-receipts"),
    ]


def test_the_server_s_role_beats_the_mailbox_name():
    folders = list_mailboxes(FakeJmap(mailboxes_answer(MAILBOXES)))  # type: ignore[arg-type]
    roles = {folder.id: folder.role for folder in folders}

    assert roles["m-inbox"] is FolderRole.INBOX
    assert roles["m-sent"] is FolderRole.SENT
    assert roles["m-archive"] is FolderRole.ARCHIVE
    assert roles["m-receipts"] is FolderRole.OTHER


def test_names_and_counts_survive_the_mapping():
    [receipts] = [
        f
        for f in list_mailboxes(FakeJmap(mailboxes_answer(MAILBOXES)))  # type: ignore[arg-type]
        if f.id == "m-receipts"
    ]

    assert (receipts.name, receipts.unread, receipts.total) == ("Receipts", 2, 9)


def test_the_request_names_only_the_properties_a_sync_needs():
    jmap = FakeJmap(mailboxes_answer([]))

    list_mailboxes(jmap)  # type: ignore[arg-type]

    [[call]] = jmap.calls
    assert call.method == "Mailbox/get"
    assert call.arguments["accountId"] == "A1"
    assert "id" in call.arguments["properties"]
    assert "unreadEmails" in call.arguments["properties"]
    assert "ids" not in call.arguments


def test_a_mailbox_whose_parent_is_gone_becomes_a_root():
    folders = build_tree([mailbox("m-orphan", "Orphan", None, "m-missing")])

    assert [(f.id, f.parent_id) for f in folders] == [("m-orphan", "")]


def test_a_parent_cycle_cannot_strand_mailboxes():
    folders = build_tree(
        [
            mailbox("m-a", "A", None, "m-b"),
            mailbox("m-b", "B", None, "m-a"),
        ]
    )

    assert sorted(f.id for f in folders) == ["m-a", "m-b"]


def test_siblings_follow_sort_order_then_name():
    folders = build_tree(
        [
            mailbox("m-b", "b", None, None, order=1),
            mailbox("m-a", "a", None, None, order=2),
            mailbox("m-c", "c", None, None, order=1),
        ]
    )

    assert [f.id for f in folders] == ["m-b", "m-c", "m-a"]


def test_an_empty_mailbox_list_stays_empty():
    assert build_tree([]) == []


def test_a_method_error_reaches_the_caller_typed():
    jmap = FakeJmap(
        JmapAnswer(
            "error",
            {"type": "accountNotFound", "description": "no such A9"},
            "0",
        )
    )

    with pytest.raises(JmapError) as raised:
        list_mailboxes(jmap)  # type: ignore[arg-type]

    assert raised.value.code == "accountNotFound"


def changes_answer(created, updated, destroyed, state, has_more=False):
    return JmapAnswer(
        "Mailbox/changes",
        {
            "created": created,
            "updated": updated,
            "destroyed": destroyed,
            "newState": state,
            "hasMoreChanges": has_more,
        },
        "0",
    )


def test_current_mailbox_state_reads_the_state_alone():
    jmap = FakeJmap(JmapAnswer("Mailbox/get", {"list": [], "state": "b9"}, "0"))

    assert current_mailbox_state(jmap) == "b9"  # type: ignore[arg-type]
    [[call]] = jmap.calls
    assert call.arguments["ids"] == []


def test_current_mailbox_state_without_a_state_is_refused():
    jmap = FakeJmap(JmapAnswer("Mailbox/get", {"list": []}, "0"))

    with pytest.raises(JmapError) as raised:
        current_mailbox_state(jmap)  # type: ignore[arg-type]

    assert raised.value.code == CODE_HTTP


def test_mailbox_changes_fetch_changed_and_report_destroyed():
    jmap = FakeJmap(
        changes_answer(["m-new"], ["m-inbox"], ["m-old"], "b2"),
        mailboxes_answer(
            [
                mailbox("m-new", "Neu", None, None, unread=1, total=1),
                mailbox("m-inbox", "Posteingang", "inbox", None, unread=4),
            ]
        ),
    )

    delta = mailbox_changes(jmap, "b1")  # type: ignore[arg-type]

    assert [(m.id, m.parent_id) for m in delta.changed] == [
        ("m-new", ""),
        ("m-inbox", ""),
    ]
    assert delta.changed[1].role is FolderRole.INBOX
    assert delta.changed[1].unread == 4
    assert delta.destroyed_ids == ["m-old"]
    assert delta.new_state == "b2"
    [[changes], [refresh]] = jmap.calls
    assert changes.arguments["sinceState"] == "b1"
    assert refresh.arguments["ids"] == ["m-new", "m-inbox"]


def test_mailbox_changes_follow_has_more_changes():
    jmap = FakeJmap(
        changes_answer(["m-a"], [], [], "b2", has_more=True),
        mailboxes_answer([mailbox("m-a", "A")]),
        changes_answer([], [], ["m-old"], "b3"),
    )

    delta = mailbox_changes(jmap, "b1")  # type: ignore[arg-type]

    assert [m.id for m in delta.changed] == ["m-a"]
    assert delta.destroyed_ids == ["m-old"]
    assert delta.new_state == "b3"


def test_a_changed_mailbox_keeps_its_parent_from_itself():
    jmap = FakeJmap(
        changes_answer([], ["m-2025"], [], "b2"),
        mailboxes_answer([mailbox("m-2025", "2025", None, "m-receipts", total=4)]),
    )

    delta = mailbox_changes(jmap, "b1")  # type: ignore[arg-type]

    assert [(m.id, m.parent_id) for m in delta.changed] == [("m-2025", "m-receipts")]


def test_an_expired_mailbox_state_raises_for_a_full_list():
    jmap = FakeJmap(
        JmapAnswer(
            "error",
            {"type": "cannotCalculateChanges", "description": "old"},
            "0",
        )
    )

    with pytest.raises(JmapError) as raised:
        mailbox_changes(jmap, "b-old")  # type: ignore[arg-type]

    assert needs_full_sync(raised.value) is True


def test_a_destroyed_only_pass_fetches_nothing():
    jmap = FakeJmap(changes_answer([], [], ["m-old"], "b2"))

    delta = mailbox_changes(jmap, "b1")  # type: ignore[arg-type]

    assert delta.changed == []
    assert delta.destroyed_ids == ["m-old"]
    assert len(jmap.calls) == 1
