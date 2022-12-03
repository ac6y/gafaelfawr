"""Test configuration parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gafaelfawr.config import Config, Settings
from gafaelfawr.exceptions import InvalidTokenError
from gafaelfawr.models.token import Token

from .support.config import build_config


def parse_config(path: Path, fix_token: bool = False) -> None:
    """Parse the configuration file and see if any exceptions are thrown.

    Parameters
    ----------
    path : `pathlib.Path`
        The path to the configuration file to test.
    fix_token : `bool`, optional
        Whether to fix an invalid ``bootstrap_token`` before checking the
        configuration file.  Some examples have intentionally invalid tokens.
    """
    with path.open("r") as f:
        settings = yaml.safe_load(f)

    # Avoid errors from an invalid bootstrap token in one of the examples.
    if fix_token and "bootstrap_token" in settings:
        settings["bootstrap_token"] = str(Token())

    Settings.parse_obj(settings)


def test_config_examples() -> None:
    """Check that all of the example configuration files validate."""
    examples_path = Path(__file__).parent.parent / "examples"
    for config_path in examples_path.iterdir():
        if config_path.name.endswith(".yaml"):
            parse_config(config_path, fix_token=True)


def test_config_no_provider() -> None:
    config_path = Path(__file__).parent / "config" / "no-provider.yaml"
    with pytest.raises(ValidationError):
        parse_config(config_path)


def test_config_both_providers() -> None:
    config_path = Path(__file__).parent / "config" / "both-providers.yaml"
    with pytest.raises(ValidationError):
        parse_config(config_path)


def test_config_invalid_admin() -> None:
    config_path = Path(__file__).parent / "config" / "bad-admin.yaml"
    with pytest.raises(ValidationError):
        parse_config(config_path)


def test_config_invalid_loglevel() -> None:
    config_path = Path(__file__).parent / "config" / "bad-loglevel.yaml"
    with pytest.raises(ValidationError):
        parse_config(config_path)


def test_config_invalid_scope() -> None:
    config_path = Path(__file__).parent / "config" / "bad-scope.yaml"
    with pytest.raises(ValidationError):
        parse_config(config_path)


def test_config_missing_scope() -> None:
    config_path = Path(__file__).parent / "config" / "missing-scope.yaml"
    with pytest.raises(ValidationError):
        parse_config(config_path)


def test_config_invalid_token(tmp_path: Path) -> None:
    bootstrap_token_file = tmp_path / "bootstrap-bad"
    bootstrap_token_file.write_bytes(b"bad-token")
    config_path = build_config(
        tmp_path, "bad-token", bootstrap_token_file=str(bootstrap_token_file)
    )
    with pytest.raises(InvalidTokenError):
        Config.from_file(config_path)
