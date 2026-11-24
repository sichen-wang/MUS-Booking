import mus_booking


def test_version_is_set():
    assert mus_booking.__version__.count(".") == 2
