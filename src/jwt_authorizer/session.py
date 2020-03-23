"""Session storage for JWT Authorizer.

Stores an oauth2_proxy session suitable for retrieval with a ticket using our
patched version of oauth2_proxy.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from binascii import Error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import redis
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from jwt_authorizer.util import add_padding

if TYPE_CHECKING:
    from flask import Flask
    from typing import Optional

__all__ = [
    "Session",
    "SessionStore",
    "Ticket",
    "parse_ticket",
]

logger = logging.getLogger(__name__)


def _new_ticket_id() -> str:
    """Generate a new ticket ID."""
    return os.urandom(20).hex()


def _new_ticket_secret() -> bytes:
    """Generate a new ticket encryption secret."""
    return os.urandom(16)


@dataclass
class Ticket:
    """A class represeting an oauth2_proxy ticket."""

    ticket_id: str = field(default_factory=_new_ticket_id)
    secret: bytes = field(default_factory=_new_ticket_secret)

    def as_handle(self, prefix: str) -> str:
        """Return the handle for this ticket.

        Parameters
        ----------
        prefix : `str`
            Prefix to prepend to the ticket ID.
        """
        return f"{prefix}-{self.ticket_id}"

    def encode(self, prefix: str) -> str:
        """Return the encoded ticket, suitable for putting in a cookie.

        Parameters
        ----------
        prefix : `str`
            Prefix to prepend to the ticket ID.
        """
        secret_b64 = base64.urlsafe_b64encode(self.secret).decode().rstrip("=")
        return f"{prefix}-{self.ticket_id}.{secret_b64}"


def parse_ticket(prefix: str, ticket: str) -> Optional[Ticket]:
    """Parse an oauth2_proxy ticket string into a Ticket.

    Parameters
    ----------
    prefix : `str`
        The expected prefix for the ticket.
    ticket : `str`
        The encoded ticket string.

    Returns
    -------
    decoded_ticket : Optional[`Ticket`]
        The decoded Ticket, or None if there was an error.
    """
    full_prefix = f"{prefix}-"
    if not ticket.startswith(full_prefix):
        logger.error("Error decoding ticket: Ticket not in expected format")
        return None
    trimmed_ticket = ticket[len(full_prefix) :]
    if "." not in trimmed_ticket:
        logger.error("Error decoding ticket: Ticket not in expected format")
        return None
    ticket_id, secret_b64 = trimmed_ticket.split(".")
    try:
        int(ticket_id, 16)  # Check hex
        secret = base64.b64decode(
            add_padding(secret_b64), altchars=b"-_", validate=True
        )
        if secret == b"":
            raise ValueError("ticket secret is empty")
        return Ticket(ticket_id=ticket_id, secret=secret)
    except (ValueError, Error) as e:
        logger.error("Error decoding ticket: %s", str(e))
        return None


@dataclass
class Session:
    """An oauth2_proxy session.

    Tokens are currently stored in Redis as a JSON dump of a dictionary.  This
    class represents the deserialized form of a session.
    """

    token: str
    email: str
    user: str
    created_at: datetime
    expires_on: datetime


class SessionStore:
    """Stores oauth2_proxy sessions and retrieves them by ticket.

    Parameters
    ----------
    prefix : `str`
        Prefix used for storing oauth2_proxy session state.
    redis : `redis.Redis`
        A Redis client configured to talk to the backend store that holds the
        (encrypted) tokens.
    key : `bytes`
        Encryption key for the individual components of the stored session.
    """

    def __init__(self, prefix: str, key: bytes, redis: redis.Redis) -> None:
        self.prefix = prefix
        self.key = key
        self.redis = redis

    def get_session(self, ticket: Ticket) -> Optional[Session]:
        """Retrieve and decrypt the session for a ticket.

        Parameters
        ----------
        ticket : `Ticket`
            The ticket corresponding to the token.

        Returns
        -------
        session : `Session` or `None`
            The corresponding session, or `None` if no session exists for this
            ticket.
        """
        handle = ticket.as_handle(self.prefix)
        encrypted_session = self.redis.get(handle)
        if not encrypted_session:
            return None

        return self._decrypt_session(ticket.secret, encrypted_session)

    def _decrypt_session(
        self, secret: bytes, encrypted_session: bytes
    ) -> Session:
        """Decrypt an oauth2_proxy session.

        Parameters
        ----------
        secret : `bytes`
            Decryption key.
        encrypted_session : `bytes`
            The encrypted session.

        Returns
        -------
        session : `Sesssion`
            The decrypted sesssion.
        """
        cipher = Cipher(
            algorithms.AES(secret), modes.CFB(secret), default_backend()
        )
        decryptor = cipher.decryptor()
        session_dict = json.loads(
            decryptor.update(encrypted_session) + decryptor.finalize()
        )
        return Session(
            token=self._decrypt_session_component(session_dict["IDToken"]),
            email=self._decrypt_session_component(session_dict["Email"]),
            user=self._decrypt_session_component(session_dict["User"]),
            created_at=self._parse_session_date(session_dict["CreatedAt"]),
            expires_on=self._parse_session_date(session_dict["ExpiresOn"]),
        )

    def _decrypt_session_component(self, encrypted_str: str) -> str:
        """Decrypt a component of an encrypted oauth2_proxy session.

        Parameters
        ----------
        encrypted_str : `str`
            The encrypted field with its IV prepended.

        Returns
        -------
        component : `str`
            The decrypted value.
        """
        encrypted_bytes = base64.b64decode(encrypted_str)
        iv = encrypted_bytes[:16]
        cipher = Cipher(
            algorithms.AES(self.key), modes.CFB(iv), default_backend()
        )
        decryptor = cipher.decryptor()
        field = decryptor.update(encrypted_bytes[16:]) + decryptor.finalize()
        return field.decode()

    @staticmethod
    def _parse_session_date(date_str: str) -> datetime:
        """Parse a date from a session record.

        Parameters
        ----------
        date_str : `str`
            The date in string format.

        Returns
        -------
        date : `datetime`
            The parsed date.

        Notes
        -----
        This date may be written by oauth2_proxy instead of us, in which case
        it will use a Go date format that includes fractional seconds down to
        the nanosecond.  Python doesn't have a date format that parses this,
        so the fractional seconds portion will be dropped, leading to an
        inaccuracy of up to a second.
        """
        date_str = re.sub("[.][0-9]+Z$", "Z", date_str)
        date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
        return date.replace(tzinfo=timezone.utc)


def get_redis_client(app: Flask) -> redis.Redis:
    """Get a Redis client from the Flask application pool.

    Exists primarily to be overridden by tests.

    Parameters
    ----------
    app : `flask.Flask`
        The Flask application.

    Returns
    -------
    redis_client : `redis.Redis`
        A Redis client.
    """
    return redis.Redis(connection_pool=app.redis_pool)


def create_session_store(app: Flask) -> SessionStore:
    """Create a TokenStore from a Flask app configuration.

    Parameters
    ----------
    app : `flask.Flask`
        The Flask application.

    Returns
    -------
    session_store : `SessionStore`
        A TokenStore created from that Flask application configuration.
    """
    redis_client = get_redis_client(app)
    prefix = app.config["OAUTH2_STORE_SESSION"]["TICKET_PREFIX"]
    secret_str = app.config["OAUTH2_STORE_SESSION"]["OAUTH2_PROXY_SECRET"]
    secret = base64.urlsafe_b64decode(secret_str)
    return SessionStore(prefix, secret, redis_client)
