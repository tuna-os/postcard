import re
from dataclasses import dataclass

from .models.email import Email

_REPLY_PREFIXES = re.compile(r"^(\s*(re|fwd|fw)\s*:\s*)+", re.IGNORECASE)

# Stored, never translated. _normalize_subject blanks it so that unrelated
# subject-less messages don't all thread into one conversation -- which means
# whatever writes it has to use this exact string. Translating it at the point
# of storage would defeat the check for every non-English locale.
NO_SUBJECT = "(no subject)"


@dataclass(frozen=True, slots=True)
class ThreadingKey:
    """The only fields threading reads: a folder's rethread builds these
    straight from its rows instead of hydrating every mail first."""

    id: int
    message_id: str
    in_reply_to: str
    references: str
    subject: str


def group(emails: list[Email]) -> dict[int, int]:
    """Map each email id to a stable conversation id (the smallest id in its
    group). Links by Message-ID / In-Reply-To / References, with a same-subject
    fallback."""
    return group_keys(
        [
            ThreadingKey(
                id=mail.id,
                message_id=mail.message_id,
                in_reply_to=mail.in_reply_to,
                references=mail.references,
                subject=mail.subject,
            )
            for mail in emails
        ]
    )


def group_keys(keys: list[ThreadingKey]) -> dict[int, int]:
    """Group threading keys the way group() groups emails."""
    # Union-find over message tokens: each token points at another token in its
    # set, and the set's representative points at itself.
    parents: dict[str, str] = {}

    def find(token: str) -> str:
        parents.setdefault(token, token)
        while parents[token] != token:
            parents[token] = parents[parents[token]]  # path halving
            token = parents[token]
        return token

    def union(left: str, right: str) -> None:
        parents[find(left)] = find(right)

    # Every email gets a token: its Message-ID, or a synthetic one so a message
    # with no Message-ID still stands on its own.
    tokens = {key.id: key.message_id.strip() or f"eid:{key.id}" for key in keys}

    for key in keys:
        token = tokens[key.id]
        if key.in_reply_to.strip():
            union(token, key.in_reply_to.strip())
        for ref in key.references.split():
            union(token, ref)

    by_subject: dict[str, str] = {}
    for key in keys:
        subject = _normalize_subject(key.subject)
        if not subject:
            continue
        if subject in by_subject:
            union(tokens[key.id], by_subject[subject])
        else:
            by_subject[subject] = tokens[key.id]

    root_min_id: dict[str, int] = {}
    for key in keys:
        root = find(tokens[key.id])
        if root not in root_min_id or key.id < root_min_id[root]:
            root_min_id[root] = key.id

    return {key.id: root_min_id[find(tokens[key.id])] for key in keys}


def _normalize_subject(subject: str) -> str:
    text = subject.strip()
    if text.lower() == NO_SUBJECT:
        return ""
    return _REPLY_PREFIXES.sub("", text).strip().lower()
