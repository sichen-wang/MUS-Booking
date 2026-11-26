"""用脱敏的真实页面样本核对解析结果。样本见 samples/，取自 2026 年 11 月的学校系统。"""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from mus_booking.domain import CHINA, Span
from mus_booking.portal import Form, Record, Verdict, classify, pages
from mus_booking.portal.errors import Unexpected
from mus_booking.portal.school import Answers, practice_rooms

SAMPLES = Path(__file__).parent / "samples"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def test_home_gives_user_id_and_name():
    me = pages.home(sample("home.html"))
    assert (me.user_id, me.name) == ("123000001", "测试同学")


def test_details_page_gives_the_entry_of_the_booking_button():
    assert pages.entry(sample("details.html")) == ("Student", "0")


def test_calendar_page():
    found = pages.calendar(sample("calendar.html"))
    assert [room.code for room in found.rooms] == ["MPC321", "MPC322", "MPC334"]
    assert found.rooms[1].piano == "立式钢琴"
    assert found.rule_id == "4b4d6e5c826c425b9a5ed7a02a46656a"
    assert (found.time_limit, found.day_start, found.day_end) == (120, 0, 23 * 60 + 30)
    assert found.server_time == datetime.fromtimestamp(1795552620.143, CHINA)
    assert found.form_url.startswith("'/a/field/client/bookForm?fId=' + fid")


def test_calendar_without_a_rule_is_rejected():
    page = sample("calendar.html").replace("ruleId=4b4d6e5c826c425b9a5ed7a02a46656a", "ruleId=")
    with pytest.raises(Unexpected, match="规则编号"):
        pages.calendar(page)


def test_form_url_is_rendered_like_the_page_script_does():
    template = pages.calendar(sample("calendar.html")).form_url
    url = pages.render(
        template,
        {
            "fid": "room1",
            "startTime": "2026-11-25 23:00",
            "endTime": "2026-11-25 23:30",
            "bookedNum": "0",
            "isFitBook": "false",
        },
    )
    assert url == (
        "/a/field/client/bookForm?fId=room1&bizFieldBookField.startTime=2026-11-25%2023%3A00"
        "&bizFieldBookField.endTime=2026-11-25%2023%3A30&repFlag=0&bookType=0&userTag=Student"
        "&approvalFlag=0&extend2=&bookedNum=0&fitBook=false&isDeptAdmin=false&adMost=0"
    )


def test_unknown_variables_in_the_form_url_are_an_error():
    with pytest.raises(Unexpected):
        pages.render("'/a?x=' + mystery", {})


def test_booking_form_is_serialized_like_the_browser_does():
    form = pages.form_by_id(sample("book_form.html"), "bizFieldBookMainForm")
    pairs = form.pairs()
    names = [name for name, _ in pairs]
    assert len(pairs) == 46
    assert names[:3] == ["id", "user.id", "serialNo"]
    assert "person_select_dialogname" not in names
    values = dict(pairs)
    assert values["bookToken"] == "t0ken0000000000000000000000000001"
    assert values["extend16"] == "0"
    assert names.count("extend16") == 1
    assert values["bizFieldBookField.FId"] == "76e34000bc5348598a705ae483005308"
    assert "extend18" in values


def test_filled_form_has_our_four_answers_in_place():
    form = pages.form_by_id(sample("book_form.html"), "bizFieldBookMainForm")
    blank = Form("room", date(2026, 11, 25), Span(1380, 1410), tuple(form.pairs()))
    filled = blank.filled()
    values = dict(filled)
    assert len(filled) == 46
    assert values["theme"] == values["bizFieldBookField.theme"] == values["bizFieldBookField.useDesc"] == "练琴"
    custom = dict(blank.filled(Answers(phone="13800000000", theme="1", people=2, purpose="合奏排练")))
    assert (custom["theme"], custom["bizFieldBookField.useDesc"]) == ("1", "合奏排练")
    assert (custom["bizFieldBookField.joinNums"], custom["userPhone"]) == ("2", "13800000000")
    assert dict(blank.filled(Answers(theme="1")))["bizFieldBookField.useDesc"] == "1"  # 用途空着就同主题
    assert values["bizFieldBookField.joinNums"] == "1"
    assert values["userPhone"] == "000000"
    assert [name for name, _ in filled] == [name for name, _ in form.pairs()]


def test_adfs_error_and_extra_auth_markers():
    page = '<div id="error"><span id="errorText" for="">用户 ID 或密码不正确。</span></div>'
    assert pages.login_error(page) == "用户 ID 或密码不正确。"
    assert pages.login_error("<html></html>") == ""
    assert pages.asks_extra_auth('<div id="verificationCodeInput">')


@pytest.mark.parametrize(
    ("payload", "verdict"),
    [
        ({"success": True, "message": "保存成功", "obj": {}}, Verdict.BOOKED),
        ({"success": False, "message": "操作过快，请稍后重试!"}, Verdict.TOO_FAST),
        ({"success": False, "message": "您有预订正在处理中，请勿重复提交!"}, Verdict.BUSY),
        ({"success": False, "message": "预订页面已失效，请刷新后重新提交!"}, Verdict.STALE),
        ({"success": False, "message": "至多提前0天预订"}, Verdict.NOT_OPEN),
        ({"success": False, "message": "至多提前1天预订"}, Verdict.NOT_OPEN),
        ({"success": False, "message": "手速太慢，该时间段已经被预订啦"}, Verdict.TAKEN),
        ({"success": False, "message": "本周预订时长已达上限"}, Verdict.REJECTED),
    ],
)
def test_replies_are_classified_by_their_message(payload, verdict):
    reply = classify(200, json.dumps(payload, ensure_ascii=False))
    assert reply.verdict is verdict
    assert reply.message == payload["message"]


def test_a_non_json_reply_is_a_rejection_with_a_readable_message():
    reply = classify(302, "<html>login</html>")
    assert reply.verdict is Verdict.REJECTED
    assert "HTTP 302" in reply.message


def test_my_bookings_records_match_by_room_name_and_times():
    row = json.loads(sample("my_bookings.json"))["data"][0]
    record = Record(row["id"], row["nameZh"], row["startTime"], row["endTime"], row["status"])
    day = date.fromisoformat(row["startTime"][:10])
    span = Span.parse(row["startTime"][11:], row["endTime"][11:])
    assert record.matches(row["nameZh"], day, span)
    assert not record.matches("MPC999 琴房", day, span)
    assert not Record(row["id"], row["nameZh"], row["startTime"], row["endTime"], "4").matches(row["nameZh"], day, span)


def test_practice_rooms_are_found_among_bookable_types():
    rows = [
        {"id": "1", "nameZh": "教师琴房", "bookFlag": 0},
        {"id": "2", "nameZh": "音乐类", "nameEn": "Music Room", "bookFlag": 1},
        {"id": "3", "nameZh": "钢琴房", "nameEn": "Piano Practice Room", "bookFlag": "1"},
        {"id": "4", "nameZh": "学生琴房", "bookFlag": 1},
    ]
    assert practice_rooms(rows)["id"] == "4"
    assert practice_rooms(rows[:3])["id"] == "3"
    assert practice_rooms(rows[:2]) is None
