"""Token data types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Mapping, Optional, Set

__all__ = ["Token", "VerifiedToken"]


@dataclass(eq=True, frozen=True)
class Token:
    """Holds an encoded JWT.

    Notes
    -----
    Tokens come in two forms: the encoded form, with is suitable for passing
    in HTTP calls and includes a signature that may not be validated; and the
    validated and decoded form, which is a dict of claims.

    This class represents a token that we have in at least encoded form, but
    which may not be validated.  The child class ValidatedToken represents the
    other case.
    """

    encoded: str
    """The encoded form of a JWT."""


@dataclass(frozen=True)
class VerifiedToken(Token):
    """Holds a verified JWT.

    Holds a JWT whose signature has been checked and whose claims have been
    decoded.
    """

    claims: Mapping[str, Any]
    """The claims contained in the token."""

    username: str
    """The value of the claim named by the username_claim config setting."""

    uid: str
    """The value of the claim named by the uid_claim config setting."""

    scope: Set[str]
    """The scope claim from the token, split on whitespace."""

    jti: str = "UNKNOWN"
    """The jti (JWT ID) claim from the token, or UNKNOWN if there was none."""

    email: Optional[str] = None
    """The email claim from the token."""