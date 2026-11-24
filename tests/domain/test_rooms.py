import pytest

from mus_booking.domain import Room, ranked


@pytest.mark.parametrize(
    ("name", "code", "piano"),
    [
        ("MPC327 管弦乐学部琴房（UP）", "MPC327", "立式钢琴"),
        ("MPC321 室内乐琴房（GP）", "MPC321", "三角钢琴"),
        ("MPC524室内乐琴房（Double GP）", "MPC524", "双三角钢琴"),
        ("MPC518 室内乐琴房（Double  GP）", "MPC518", "双三角钢琴"),
        ("MPC319 管弦乐学部琴房", "MPC319", ""),
        ("MPC401 Practice Room (UP)", "MPC401", "立式钢琴"),
        ("MPC402 琴房（Celesta）", "MPC402", "Celesta"),
        ("琴房", "琴房", ""),
    ],
)
def test_code_and_piano_come_from_the_school_name(name, code, piano):
    room = Room("x", name)
    assert room.code == code
    assert room.piano == piano


def test_ranked_puts_used_rooms_first_then_sorts_numerically():
    rooms = [Room(str(n), f"MPC{n} 琴房") for n in (1003, 412, 327, 518)]
    order = ranked(rooms, used=["518", "327", "518"])
    assert [room.code for room in order] == ["MPC518", "MPC327", "MPC412", "MPC1003"]
