"""Display names for people refs on the owner's approval card (M41-R2, OI-M60-002).

``people.resolve`` gives the model refs (``person:<id>``, ``contact:<n>``); a tool such as
``builtin.telegram.send_message`` receives the ref, so the core-rendered approval text would
otherwise show ``to="person:3e6c…"``. :class:`MemoryRefNames` turns a ref back into the name the
user knows, with the same lookup the executing tool uses:

- ``person:<id>`` (canonical lowercase/uppercase UUID) → an *active* person of the run's owner;
- ``contact:<n>`` → the phone contact cached for this owner and this run.

Only the display name and the relation are returned, passed through ``minimize``: never a
phone number, e-mail, handle or id. Unknown, foreign or malformed refs are simply absent (the
card then keeps the raw ref).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection

from app.modules.memory.service import MemoryService
from app.modules.memory.text import minimize
from app.modules.memory.tools import ContactsCache

PERSON_REF = re.compile(r"^person:([0-9a-fA-F-]{36})$")
CONTACT_REF = re.compile(r"^contact:[0-9]{1,3}$")
MAX_REFS = 8
CONTACT_NOTE = "контакт телефона"


def _person_id(ref: str) -> str | None:
    match = PERSON_REF.match(ref)
    if match is None:
        return None
    raw = match.group(1).lower()
    try:
        canonical = str(uuid.UUID(raw))
    except ValueError:
        return None
    # The executing tool compares the canonical form; anything else resolves to nobody there.
    return canonical if canonical == raw else None


def _label(name: str, note: str | None) -> str:
    name = minimize(name).strip()
    note = minimize(note).strip() if note else ""
    return f"{name} ({note})" if note else name


class MemoryRefNames:
    """``RefNameResolver`` for the tool router: owner-only, read-only, no model involved."""

    def __init__(self, service: MemoryService, contacts: ContactsCache) -> None:
        self._service = service
        self._contacts = contacts

    async def ref_names(
        self, *, user_id: str, run_id: str, refs: Collection[str]
    ) -> dict[str, str]:
        wanted = list(dict.fromkeys(refs))[:MAX_REFS]
        names: dict[str, str] = {}
        people: dict[str, list[str]] = {}
        for ref in wanted:
            person_id = _person_id(ref)
            if person_id is not None:
                people.setdefault(person_id, []).append(ref)
            elif CONTACT_REF.match(ref):
                contact = self._contacts.get(user_id=user_id, run_id=run_id, ref=ref)
                if contact is not None and contact.display_name.strip():
                    names[ref] = _label(contact.display_name, CONTACT_NOTE)
        if people:
            for person in await self._service.people_by_ids(
                user_id=user_id, person_ids=list(people)
            ):
                if person.display_name.strip():
                    for ref in people.get(str(person.id), ()):
                        names[ref] = _label(person.display_name, person.relation)
        return names
