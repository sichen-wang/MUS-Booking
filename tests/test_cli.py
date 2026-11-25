import re

import pytest

from mus_booking import cli
from mus_booking.store import Store, Vault, password_matches


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


def test_init_then_invite_and_admin(env, capsys, monkeypatch):
    assert cli.main(["init"]) == 0
    assert (env / "keys" / "secret.key").is_file()
    assert (env / "data" / "mus-booking.db").is_file()
    assert cli.main(["init"]) == 0
    assert "已存在" in capsys.readouterr().out

    cli.main(["invite"])
    code, link = capsys.readouterr().out.split()
    assert re.fullmatch(r"[A-Z2-9]{4}", code)
    assert link == f"https://example.test/booking/?invite={code}"

    answers = iter(["open sesame", "open sesame", "short", "open sesame", "different"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["admin", "Keeper"]) == 0
    assert "学号一栏填 Keeper" in capsys.readouterr().out
    store = Store.open(env / "data" / "mus-booking.db", Vault.load(env / "keys" / "secret.key"))
    account, secret = store.admin_secret("keeper")
    assert account.name == "Keeper" and password_matches("open sesame", secret)
    store.close()
    assert cli.main(["admin", "12345678"]) == 1
    assert "纯数字" in capsys.readouterr().err
    assert cli.main(["admin", "Keeper"]) == 1
    assert "至少 8 位" in capsys.readouterr().err
    assert cli.main(["admin", "Keeper"]) == 1
    assert "不一致" in capsys.readouterr().err
