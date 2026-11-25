import re

import pytest

from mus_booking import cli


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MUS_BOOKING_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("MUS_BOOKING_KEY", str(tmp_path / "keys" / "secret.key"))
    monkeypatch.setenv("MUS_BOOKING_URL", "https://example.test/booking")
    return tmp_path


def test_commands_refuse_to_run_without_a_key(env, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["invite"])
    assert stopped.value.code == cli.CONFIG_ERROR
    assert "mus-booking init" in capsys.readouterr().err


def test_init_then_invite_and_admin_link(env, capsys):
    assert cli.main(["init"]) == 0
    assert (env / "keys" / "secret.key").is_file()
    assert (env / "data" / "mus-booking.db").is_file()
    assert cli.main(["init"]) == 0
    assert "已存在" in capsys.readouterr().out

    cli.main(["invite"])
    code, link = capsys.readouterr().out.split()
    assert re.fullmatch(r"[A-Z2-9]{4}", code)
    assert link == f"https://example.test/booking/?invite={code}"

    cli.main(["admin-link"])
    assert re.fullmatch(r"https://example\.test/booking/admin/enter/[\w-]+\n", capsys.readouterr().out)
