"""Reading ``-o key=value`` options without an option schema (RFC 0003 §5)."""

from pathlib import Path

import pytest
from qpi_driver.options import Options


def test_a_missing_option_is_its_default():
    """Every accessor takes the fallback, so a device states its default once."""
    options = Options()

    assert options.get_str("base_url", "http://localhost") == "http://localhost"
    assert options.get_int("ticks", 3) == 3
    assert options.get_float("interval", 5.0) == 5.0
    assert options.get_bool("is_dummy") is False
    assert options.get_path("config", "./a.json") == Path("./a.json")


def test_a_given_option_is_read_as_the_accessor_says():
    """The type of a value is whichever accessor reads it; there is no declared one."""
    options = Options({"ticks": "9", "interval": "0.5", "config": "./b.json"})

    assert options.get_int("ticks", 3) == 9
    assert options.get_float("interval", 5.0) == 0.5
    assert options.get_path("config", "./a.json") == Path("./b.json")


def test_values_are_strings_however_they_arrived():
    """The CLI hands over strings; a caller passing something else gets one anyway."""
    assert Options({"ticks": 9}).get_str("ticks") == "9"


def test_a_bad_value_names_the_option_it_belongs_to():
    """A parser's complaint is useless without knowing which option raised it."""
    with pytest.raises(ValueError, match=r"bad value for -o ticks"):
        Options({"ticks": "many"}).get_int("ticks", 3)


def test_a_bad_default_says_it_was_the_default():
    """A device defaulting itself somewhere it may not write must not blame the operator."""
    with pytest.raises(ValueError, match=r"bad value for -o data_dir \(default\)"):
        Options().get_dir("data_dir", "/etc/passwd.d")


def test_get_dir_refuses_a_path_outside_a_safe_location():
    """A directory option is checked, not just converted (RFC 0003 §10)."""
    with pytest.raises(ValueError, match="not in a safe location"):
        Options({"data_dir": "/etc/shadow.d"}).get_dir("data_dir", "./bin/data")


def test_booleans_accept_the_usual_spellings():
    """A boolean-ish option value, however an operator felt like writing it."""
    for true in ("1", "true", "TRUE", " yes ", "On"):
        assert Options({"k": true}).get_bool("k") is True
    for false in ("0", "false", "no", "off", "", "  ", "maybe"):
        assert Options({"k": false}).get_bool("k") is False


def test_require_reports_the_line_that_was_missing():
    """The error is the fix: it shows the -o pair the operator should have typed."""
    with pytest.raises(ValueError) as excinfo:
        Options().require("channels", "a:K")

    assert "missing required option 'channels', e.g. -o channels=a:K" in str(
        excinfo.value
    )


def test_require_returns_the_value_when_it_is_there():
    assert Options({"channels": "a:K"}).require("channels") == "a:K"


def test_unread_reports_what_nothing_looked_at():
    """The device's own code is the schema: a key it never read is a typo."""
    options = Options({"ticks": "9", "tickz": "9", "elephant": "1"})
    options.get_int("ticks", 3)

    assert options.unread() == ("elephant", "tickz")


def test_reading_an_absent_key_still_counts_as_reading_it():
    """Asking about a key is what says the device understands it, given or not."""
    options = Options({"ticks": "9"})
    options.get_str("base_url", "http://localhost")
    options.get_int("ticks", 3)

    assert options.unread() == ()


def test_containment_counts_as_a_read():
    options = Options({"api_key": "s3cret"})

    assert "api_key" in options
    assert options.unread() == ()


def test_remaining_hands_over_everything_left():
    """For a device passing options on to something this SDK has never seen."""
    options = Options({"ticks": "9", "qubits": "5", "mode": "fast"})
    options.get_int("ticks", 3)

    assert options.remaining() == {"qubits": "5", "mode": "fast"}
    assert options.unread() == ()
