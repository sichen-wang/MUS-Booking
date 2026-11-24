"""琴房：学校给出的名称形如“MPC327 管弦乐学部琴房（UP）”，页面上只显示编号和钢琴类型。"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

PIANOS = {"UP": "立式钢琴", "GP": "三角钢琴", "DOUBLE GP": "双三角钢琴"}

_CODE = re.compile(r"^\s*([A-Za-z]+\s*\d+[A-Za-z]?)")
_TAIL = re.compile(r"[（(]\s*([^（）()]+?)\s*[）)]\s*$")


@dataclass(frozen=True, slots=True)
class Room:
    id: str
    name: str

    @property
    def code(self) -> str:
        found = _CODE.match(self.name)
        return found.group(1).replace(" ", "") if found else self.name.strip()

    @property
    def piano(self) -> str:
        found = _TAIL.search(self.name)
        if not found:
            return ""
        raw = found.group(1)
        return PIANOS.get(re.sub(r"\s+", " ", raw).upper(), raw)


def ranked(rooms: Iterable[Room], used: Iterable[str]) -> list[Room]:
    """常用的琴房排在前面，其余按编号排列。"""
    counts = Counter(used)
    return sorted(rooms, key=lambda room: (-counts[room.id], _natural(room.code)))


def _natural(code: str) -> tuple[str, int, str]:
    found = re.match(r"([A-Za-z]*)(\d*)(.*)", code)
    assert found is not None
    letters, digits, rest = found.groups()
    return letters.upper(), int(digits or 0), rest
