"""Tests for the gafaelfawr.util package."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from gafaelfawr.keypair import RSAKeyPair
from gafaelfawr.util import (
    add_padding,
    base64_to_number,
    format_datetime_for_logging,
    is_bot_user,
    normalize_timedelta,
    number_to_base64,
    to_camel_case,
)


def test_add_padding() -> None:
    assert add_padding("") == ""
    assert add_padding("Zg") == "Zg=="
    assert add_padding("Zgo") == "Zgo="
    assert add_padding("Zm8K") == "Zm8K"
    assert add_padding("Zm9vCg") == "Zm9vCg=="


def test_base64_to_number() -> None:
    keypair = RSAKeyPair.generate()
    for n in (
        0,
        1,
        65535,
        65536,
        2147483648,
        4294967296,
        18446744073709551616,
        keypair.public_numbers().e,
        keypair.public_numbers().n,
    ):
        n_b64 = number_to_base64(n).decode().rstrip("=")
        assert base64_to_number(n_b64) == n

    assert base64_to_number("AQAB") == 65537


def test_format_datetime_for_logging() -> None:
    assert format_datetime_for_logging(None) is None
    date = datetime.fromisoformat("2022-09-16T12:03:45.123+00:00")
    assert format_datetime_for_logging(date) == "2022-09-16 12:03:45+00:00"


def test_is_bot_user() -> None:
    assert is_bot_user("bot-user")
    assert not is_bot_user("some-user")
    assert not is_bot_user("bot")
    assert not is_bot_user("botuser")
    assert not is_bot_user("bot-in!valid")


def test_normalize_timedelta() -> None:
    assert normalize_timedelta(None) is None
    assert normalize_timedelta(10) == timedelta(seconds=10)

    with pytest.raises(ValueError):
        normalize_timedelta("not an int")  # type: ignore[arg-type]


def test_number_to_base64() -> None:
    assert number_to_base64(0) == b"AA"
    assert number_to_base64(65537) == b"AQAB"


def test_to_camel_case() -> None:
    assert to_camel_case("foo") == "foo"
    assert to_camel_case("minimum_lifetime") == "minimumLifetime"
    assert to_camel_case("replace_403") == "replace403"
    assert to_camel_case("foo_bar_baz") == "fooBarBaz"
