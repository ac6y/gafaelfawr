"""Tests for the command-line interface.

Be careful when writing tests in this framework because the click command
handling code spawns its own async worker pools when needed.  None of these
tests can therefore be async, and should instead run coroutines using the
``event_loop`` fixture when needed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
import structlog
from click.testing import CliRunner
from cryptography.fernet import Fernet
from safir.database import initialize_database
from safir.datetime import current_datetime
from safir.testing.slack import MockSlackWebhook
from sqlalchemy.ext.asyncio import AsyncEngine

from gafaelfawr.cli import main
from gafaelfawr.config import Config, OIDCClient
from gafaelfawr.constants import CHANGE_HISTORY_RETENTION
from gafaelfawr.exceptions import InvalidGrantError
from gafaelfawr.factory import Factory
from gafaelfawr.models.admin import Admin
from gafaelfawr.models.history import TokenChange, TokenChangeHistoryEntry
from gafaelfawr.models.oidc import OIDCAuthorizationCode, OIDCScope
from gafaelfawr.models.token import Token, TokenData, TokenType, TokenUserInfo
from gafaelfawr.schema import Base
from gafaelfawr.storage.history import TokenChangeHistoryStore
from gafaelfawr.storage.token import TokenDatabaseStore

from .support.config import configure


async def _initialize_database(engine: AsyncEngine, config: Config) -> None:
    """Initialize the database."""
    logger = structlog.get_logger("gafaelfawr")
    await initialize_database(engine, logger, schema=Base.metadata, reset=True)


def test_audit(
    tmp_path: Path,
    config: Config,
    engine: AsyncEngine,
    event_loop: asyncio.AbstractEventLoop,
    mock_slack: MockSlackWebhook,
) -> None:
    logger = structlog.get_logger("gafaelfawr")
    now = current_datetime()
    token_data = TokenData(
        token=Token(),
        username="some-user",
        token_type=TokenType.session,
        scopes=["user:token"],
        created=now,
        expires=now + timedelta(days=7),
    )

    async def setup() -> None:
        await initialize_database(engine, logger, schema=Base.metadata)
        async with Factory.standalone(config, engine) as factory:
            token_db_store = TokenDatabaseStore(factory.session)
            async with factory.session.begin():
                await token_db_store.add(token_data)

    event_loop.run_until_complete(setup())
    runner = CliRunner()
    result = runner.invoke(main, ["audit"], catch_exceptions=False)
    assert result.exit_code == 0

    alerts = [
        f"Token `{token_data.token.key}` for `some-user` found in database"
        " but not Redis",
    ]
    expected_alert = (
        "Gafaelfawr data inconsistencies found:\n• " + "\n• ".join(alerts)
    )
    assert mock_slack.messages == [
        {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": expected_alert,
                        "verbatim": True,
                    },
                }
            ]
        }
    ]

    mock_slack.messages = []
    runner = CliRunner()
    result = runner.invoke(main, ["audit", "--fix"], catch_exceptions=False)
    assert result.exit_code == 0
    assert len(mock_slack.messages) == 1

    mock_slack.messages = []
    runner = CliRunner()
    result = runner.invoke(main, ["audit"], catch_exceptions=False)
    assert result.exit_code == 0
    assert len(mock_slack.messages) == 0


def test_delete_all_data(
    tmp_path: Path,
    engine: AsyncEngine,
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    redirect_uri = "https://example.com/"
    clients = [
        OIDCClient(
            client_id="some-id",
            client_secret="some-secret",
            return_uri=redirect_uri,
        )
    ]
    config = configure(tmp_path, "github-oidc-server", oidc_clients=clients)
    logger = structlog.get_logger("gafaelfawr")

    async def setup() -> OIDCAuthorizationCode:
        await initialize_database(engine, logger, schema=Base.metadata)
        async with Factory.standalone(config, engine) as factory:
            token_service = factory.create_token_service()
            user_info = TokenUserInfo(username="some-user")
            token = await token_service.create_session_token(
                user_info, scopes=[], ip_address="127.0.0.1"
            )
            oidc_service = factory.create_oidc_service()
            return await oidc_service.issue_code(
                client_id="some-id",
                redirect_uri=redirect_uri,
                token=token,
                scopes=[OIDCScope.openid],
            )

    code = event_loop.run_until_complete(setup())
    runner = CliRunner()
    result = runner.invoke(main, ["delete-all-data"], catch_exceptions=False)
    assert result.exit_code == 0

    async def check_data() -> None:
        async with Factory.standalone(config, engine) as factory:
            admin_service = factory.create_admin_service()
            expected = [Admin(username=u) for u in config.initial_admins]
            assert await admin_service.get_admins() == expected
            token_service = factory.create_token_service()
            bootstrap = TokenData.bootstrap_token()
            assert await token_service.list_tokens(bootstrap) == []
            oidc_service = factory.create_oidc_service()
            with pytest.raises(InvalidGrantError):
                await oidc_service.redeem_code(
                    grant_type="authorization_code",
                    client_id="some-id",
                    client_secret="some-secret",
                    redirect_uri="https://example.com/",
                    code=str(code),
                )

    event_loop.run_until_complete(check_data())


def test_generate_key() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["generate-key"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "-----BEGIN PRIVATE KEY-----" in result.output


def test_generate_session_secret() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main, ["generate-session-secret"], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert Fernet(result.output.rstrip("\n").encode())


def test_generate_token() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["generate-token"], catch_exceptions=False)

    assert result.exit_code == 0
    assert Token.from_str(result.output.rstrip("\n"))


def test_help() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["-h"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Commands:" in result.output

    result = runner.invoke(main, ["help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Commands:" in result.output

    result = runner.invoke(main, ["help", "run"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Options:" in result.output
    assert "Commands:" not in result.output

    result = runner.invoke(
        main, ["help", "unknown-command"], catch_exceptions=False
    )
    assert result.exit_code != 0
    assert "Unknown help topic unknown-command" in result.output


def test_init(
    engine: AsyncEngine, config: Config, event_loop: asyncio.AbstractEventLoop
) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["init"], catch_exceptions=False)
    assert result.exit_code == 0

    async def check_database() -> None:
        async with Factory.standalone(config, engine) as factory:
            admin_service = factory.create_admin_service()
            expected = [Admin(username=u) for u in config.initial_admins]
            assert await admin_service.get_admins() == expected
            token_service = factory.create_token_service()
            bootstrap = TokenData.bootstrap_token()
            assert await token_service.list_tokens(bootstrap) == []

    event_loop.run_until_complete(check_database())


def test_maintenance(
    engine: AsyncEngine, config: Config, event_loop: asyncio.AbstractEventLoop
) -> None:
    now = current_datetime()
    token_data = TokenData(
        token=Token(),
        username="some-user",
        token_type=TokenType.session,
        scopes=["read:all", "user:token"],
        created=now - timedelta(minutes=60),
        expires=now - timedelta(minutes=30),
    )
    new_token_data = TokenData(
        token=Token(),
        username="some-user",
        token_type=TokenType.session,
        scopes=["read:all", "user:token"],
        created=now - timedelta(minutes=60),
        expires=now + timedelta(minutes=30),
    )
    old_history_entry = TokenChangeHistoryEntry(
        token=Token().key,
        username="other-user",
        token_type=TokenType.session,
        scopes=[],
        expires=now - CHANGE_HISTORY_RETENTION + timedelta(days=10),
        actor="other-user",
        action=TokenChange.create,
        ip_address="127.0.0.1",
        event_time=now - CHANGE_HISTORY_RETENTION - timedelta(minutes=1),
    )

    async def initialize() -> None:
        async with Factory.standalone(config, engine) as factory:
            async with factory.session.begin():
                token_store = TokenDatabaseStore(factory.session)
                await token_store.add(token_data)
                await token_store.add(new_token_data)
                history_store = TokenChangeHistoryStore(factory.session)
                await history_store.add(old_history_entry)

    event_loop.run_until_complete(initialize())
    runner = CliRunner()
    result = runner.invoke(main, ["maintenance"], catch_exceptions=False)
    assert result.exit_code == 0

    async def check_database() -> None:
        async with Factory.standalone(config, engine) as factory:
            async with factory.session.begin():
                token_store = TokenDatabaseStore(factory.session)
                assert await token_store.get_info(token_data.token.key) is None
                assert await token_store.get_info(new_token_data.token.key)
                history_store = TokenChangeHistoryStore(factory.session)
                history = await history_store.list(username="other-user")
                assert history.entries == []

    event_loop.run_until_complete(check_database())


def test_openapi_schema(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["openapi-schema"])
    assert result.exit_code == 0
    assert json.loads(result.output)
    assert "Return to Gafaelfawr documentation" not in result.output
    schema = result.output

    result = runner.invoke(
        main,
        ["openapi-schema", "--output", str(tmp_path / "openapi.json")],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert not result.output
    assert (tmp_path / "openapi.json").read_text() == schema

    result = runner.invoke(
        main, ["openapi-schema", "--add-back-link"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "Return to Gafaelfawr documentation" in result.output
