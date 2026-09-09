"""Per-model usage budget: forgiving token/time parsing, the rolling-window
bucket, and that an exhausted server drops out of routing until the window
rolls over (so a frontier model cannot silently burn its allowance)."""
import time

from kaisen.budget import Budget, parse_duration, parse_tokens


# --------------------------------------------------------------------------- #
# parse_tokens
# --------------------------------------------------------------------------- #

def test_parse_tokens_plain_and_separated():
    assert parse_tokens("1000000") == 1_000_000
    assert parse_tokens("1,000,000") == 1_000_000
    assert parse_tokens("1_000_000") == 1_000_000
    assert parse_tokens(1_000_000) == 1_000_000


def test_parse_tokens_suffixes():
    assert parse_tokens("1M") == 1_000_000
    assert parse_tokens("1m") == 1_000_000     # case-insensitive
    assert parse_tokens("2.5M") == 2_500_000
    assert parse_tokens("500K") == 500_000
    assert parse_tokens("1B") == 1_000_000_000


def test_parse_tokens_blank_and_invalid():
    assert parse_tokens("") is None
    assert parse_tokens(None) is None
    assert parse_tokens("  ") is None
    assert parse_tokens("abc") is None
    assert parse_tokens("0") is None           # zero = no limit, not a limit


def test_parse_tokens_word_suffix():
    assert parse_tokens("1000000 tokens") == 1_000_000
    assert parse_tokens("2000 token") == 2000


# --------------------------------------------------------------------------- #
# parse_duration
# --------------------------------------------------------------------------- #

def test_parse_duration_units():
    assert parse_duration("30s") == 30.0
    assert parse_duration("5m") == 300.0
    assert parse_duration("12h") == 12 * 3600.0
    assert parse_duration("3d") == 3 * 86400.0
    assert parse_duration("1w") == 7 * 86400.0


def test_parse_duration_clock_style():
    assert parse_duration("12:00:00") == 12 * 3600.0   # 12 h
    assert parse_duration("01:30:00") == 90 * 60.0      # 90 min
    assert parse_duration("12:00") == 12 * 3600.0       # 12 h
    assert parse_duration("00:30") == 30 * 60.0         # 30 min


def test_parse_duration_bare_number():
    assert parse_duration(90) == 90.0
    assert parse_duration("90") == 90.0


def test_parse_duration_blank_and_invalid():
    assert parse_duration("") is None
    assert parse_duration(None) is None
    assert parse_duration("abc") is None
    assert parse_duration("25:99") is None       # seconds overflow
    assert parse_duration("0") is None


# --------------------------------------------------------------------------- #
# Budget bucket
# --------------------------------------------------------------------------- #

def test_unconfigured_budget_never_exhausted():
    b = Budget({})
    assert b.configured is False
    assert b.exhausted() is False
    b.record(tokens=10 ** 12, generations=999)
    assert b.exhausted() is False


def test_token_limit_exhausts_and_records():
    b = Budget({"max_tokens": "1M", "reset": "1d"})
    assert b.configured is True
    b.record(tokens=999_000)
    assert b.exhausted() is False
    b.record(tokens=1_000)          # reaches 1M
    assert b.exhausted() is True
    st = b.status()
    assert st["tokens_used"] >= 1_000_000
    assert st["max_tokens"] == 1_000_000


def test_generation_limit_exhausts():
    b = Budget({"max_generations": 3, "reset": "1d"})
    b.record(generations=3)
    assert b.exhausted() is True
    assert b.status()["generations_used"] == 3
    assert b.status()["max_generations"] == 3


def test_reset_window_rolls_over():
    b = Budget({"max_tokens": 1000, "reset": "30s"})
    b.record(tokens=1000)
    assert b.exhausted() is True
    # force the window to have elapsed
    b._window_start = time.time() - 31
    assert b.exhausted() is False       # rolled over, usage cleared
    st = b.status()
    assert st["tokens_used"] == 0
    assert st["window_reset_in_s"] is not None


def test_no_reset_is_one_shot_limit():
    b = Budget({"max_tokens": 1000})     # reset absent -> never rolls
    b.record(tokens=1000)
    assert b.exhausted() is True
    b._window_start = time.time() - 999999
    assert b.exhausted() is True         # still capped (no window)


def test_config_round_trip():
    cfg = {"max_tokens": "1M", "max_generations": "50", "reset": "3h"}
    b = Budget(cfg)
    back = Budget(b.config)
    assert back._max_tokens == 1_000_000
    assert back._max_generations == 50
    assert back._window == 3 * 3600.0
