"""Text helpers of the Memory Ledger: search terms, name matching, minimization.

Everything here is pure (no database, no keys) so it is unit-tested directly.

- ``search_terms`` turns text into normalized stems; the store indexes keyed
  hashes of them (blind index), never the words themselves.
- ``minimize`` strips identifiers (phones, e-mails, messenger handles, long
  numeric ids) from text that goes to a model provider (Review §7.5).
- ``extract_person`` recognises «Яна — моя жена» / «Yana is my wife» so a
  confirmed fact can create a ``Person`` even when the model did not send one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STOPWORDS = frozenset(
    {
        # ru
        "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все",
        "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по",
        "только", "ее", "её", "мне", "было", "вот", "от", "меня", "еще", "ещё", "нет", "о",
        "из", "ему", "ли", "если", "уже", "или", "ни", "быть", "был", "до", "вас", "нибудь",
        "мой", "моя", "моё", "мое", "мои", "моей", "моего", "моему", "это", "этот", "эта",
        "запомни", "помни", "пожалуйста", "кто", "где", "когда",
        # en
        "the", "a", "an", "and", "or", "is", "are", "was", "of", "to", "in", "on", "my",
        "me", "i", "it", "that", "this", "remember", "please", "be", "for", "with",
    }
)  # fmt: skip
_RU_ENDINGS = tuple(
    sorted(
        (
            "иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ая", "яя", "ое",
            "ее", "ые", "ие", "ой", "ей", "ий", "ый", "ом", "ем", "ам", "ям", "ах", "ях", "ов",
            "ев", "ию", "ия", "ие", "ью", "а", "я", "о", "е", "ы", "и", "у", "ю", "ь", "й",
        ),
        key=len,
        reverse=True,
    )
)  # fmt: skip
_EN_ENDINGS = ("ing", "ies", "es", "ed", "s")
_CYRILLIC = re.compile(r"[а-я]")

PHONE = re.compile(r"(?<![\w+])\+?\d[\d\s().-]{5,}\d(?!\w)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{4,32}\b")
LONG_ID = re.compile(r"(?<!\w)\d{6,}(?!\w)")


def normalize(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split())


def stem(word: str) -> str:
    """A deliberately small stemmer: Russian/English inflection endings.

    Equal stems are what matters (``Яна``/``Яне``/``Яну`` → ``ян``), not
    linguistic accuracy; a 2-letter floor keeps short names searchable.
    """
    word = normalize(word)
    if _CYRILLIC.search(word):
        for ending in _RU_ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 2:
                return word[: -len(ending)]
        return word
    for _ in range(2):  # «meetings» → «meeting» → «meet»
        for ending in _EN_ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                word = word[: -len(ending)]
                break
        else:
            break
    return word


def search_terms(text: str, *, limit: int = 256) -> list[str]:
    """Distinct stems of the meaningful words of ``text``, in order of appearance."""
    seen: dict[str, None] = {}
    for match in _WORD.finditer(normalize(text)):
        word = match.group(0)
        if word in _STOPWORDS or (len(word) < 2 and not word.isdigit()):
            continue
        seen.setdefault(stem(word), None)
        if len(seen) >= limit:
            break
    return list(seen)


_DATE = re.compile(r"^(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})$")


def _phone_like(candidate: str) -> bool:
    digits = sum(ch.isdigit() for ch in candidate)
    return digits >= 7 and not _DATE.match(candidate.strip())


def _redact_phone(match: re.Match[str]) -> str:
    return "[номер скрыт]" if _phone_like(match.group(0)) else match.group(0)


def minimize(text: str) -> str:
    """Remove identifiers before text leaves the core for a model provider."""
    text = EMAIL.sub("[email скрыт]", text)
    text = PHONE.sub(_redact_phone, text)
    text = HANDLE.sub("[ник скрыт]", text)
    return LONG_ID.sub("[id скрыт]", text)


def contains_identifier(text: str) -> bool:
    if EMAIL.search(text) or HANDLE.search(text) or LONG_ID.search(text):
        return True
    return any(_phone_like(m.group(0)) for m in PHONE.finditer(text))


@dataclass(frozen=True, slots=True)
class PersonDraft:
    display_name: str
    relation: str | None = None
    aliases: tuple[str, ...] = ()


_PERSON_PATTERNS = (
    # «Запомни, что Яна — моя жена», «Яна - мой коллега», «Яна это моя сестра»
    re.compile(
        r"^(?:(?:запомни|помни|знай)[,:]?\s+(?:что\s+)?)?"
        r"(?P<name>[A-ZА-ЯЁ][\w-]{1,40}(?:\s+[A-ZА-ЯЁ][\w-]{1,40})?)\s*"
        r"(?:—|–|-|это|—\s*это)\s*(?:моя|мой|моё|мое|мои)\s+(?P<relation>[\w -]{2,40}?)[.!]?$",
        re.IGNORECASE | re.UNICODE,
    ),
    # «Remember that Yana is my wife»
    re.compile(
        r"^(?:remember\s+(?:that\s+)?)?(?P<name>[A-Z][\w-]{1,40}(?:\s+[A-Z][\w-]{1,40})?)"
        r"\s+is\s+my\s+(?P<relation>[\w -]{2,40}?)[.!]?$",
        re.IGNORECASE | re.UNICODE,
    ),
)


def extract_person(text: str) -> PersonDraft | None:
    """Person named in a simple «X — моя Y» statement, else ``None``."""
    candidate = " ".join(text.strip().split())
    for pattern in _PERSON_PATTERNS:
        match = pattern.match(candidate)
        if match is None:
            continue
        name = match.group("name").strip()
        if not name[:1].isupper():
            return None
        relation = normalize(match.group("relation"))
        return PersonDraft(display_name=name, relation=relation or None)
    return None


def name_score(query: str, names: list[str], relation: str | None) -> int:
    """How well a spoken ``query`` matches a person (0 = no match).

    3: whole normalized name/alias equal; 2: every query stem is in one
    name/alias; 1: the query names the relation («жена»).
    """
    wanted = normalize(query)
    if not wanted:
        return 0
    if any(normalize(name) == wanted for name in names):
        return 3
    query_stems = set(search_terms(query))
    if not query_stems:
        return 0
    for name in names:
        if query_stems <= set(search_terms(name)):
            return 2
    if relation and query_stems <= set(search_terms(relation)):
        return 1
    return 0
