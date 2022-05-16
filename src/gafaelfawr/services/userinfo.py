"""Service and caching layer for user metadata."""

from __future__ import annotations

import re
from typing import List, Optional

from structlog.stdlib import BoundLogger

from ..config import OIDCConfig
from ..constants import BOT_USERNAME_REGEX
from ..dependencies.cache import IdCache
from ..exceptions import (
    InvalidTokenClaimsError,
    MissingClaimsError,
    ValidationError,
)
from ..models.oidc import OIDCVerifiedToken
from ..models.token import TokenData, TokenGroup, TokenUserInfo
from ..storage.firestore import FirestoreStorage
from ..storage.ldap import LDAPStorage

__all__ = ["OIDCUserInfoService", "UserInfoService"]


class UserInfoService:
    """Retrieve user metadata from external systems.

    In some cases, we take user metadata from external systems.  Examples are:

    #. Resolve a unique identifier to a username via LDAP.
    #. Get user group membership from LDAP.
    #. Get UID or GID from LDAP.
    #. Assign and manage UIDs and GIDs via Google Firestore.

    This service manages those interactions.  UID/GID data from Firestore is
    cached.  LDAP data is not cached, since LDAP is supposed to be able to
    handle a very high query load.

    This is the parent class, which is further specialized by authentication
    provider to incorporate some provider-specific logic for extracting user
    information from the upstream authentication details.

    Parameters
    ----------
    cache : `gafaelfawr.dependencies.cache.IdCache`
        The underlying UID and GID cache and locks.
    ldap_storage : `gafaelfawr.storage.ldap.LDAPStorage`, optional
        The underlying LDAP storage for user metadata, if LDAP was
        configured.
    firestore : `gafaelfawr.storage.firestore.FirestoreStorage`, optional
        The underlying Firestore storage for UID and GID assignment, if
        Firestore was configured.
    logger : `structlog.stdlib.BoundLogger`
        Logger to use.
    """

    def __init__(
        self,
        *,
        cache: IdCache,
        ldap_storage: Optional[LDAPStorage],
        firestore: Optional[FirestoreStorage],
        logger: BoundLogger,
    ) -> None:
        self._cache = cache
        self._ldap = ldap_storage
        self._firestore = firestore
        self._logger = logger

    async def clear(self) -> None:
        """Invalidate all of the caches.

        Used primarily for testing.
        """
        await self._cache.clear()

    async def get_groups_from_ldap(self, username: str) -> List[TokenGroup]:
        """Get the user's groups from LDAP.

        This duplicates some of the code that generates user information from
        LDAP, but retrieves only the user's groups.  It's used after login to
        verify that the user is a member of a group that grants access.

        Parameters
        ----------
        username : `str`
            Username of the user.

        Returns
        -------
        groups : List[`gafaelfawr.models.token.TokenGroup`]
            The user's groups according to LDAP, without GIDs.
        """
        if not self._ldap:
            raise RuntimeError("LDAP requested but not configured")
        async with self._ldap.connect() as conn:
            return await conn.get_groups(username, add_gids=False)

    async def get_user_info_from_token(
        self, token_data: TokenData
    ) -> TokenUserInfo:
        """Get the user information from a token.

        This returns the information stored in the token.  If group
        information is not present in the token and LDAP is configured, it
        will be obtained dynamically from LDAP.

        Parameters
        ----------
        token_data : `gafaelfawr.models.token.TokenData`
            Data from the authentication token.

        Returns
        -------
        user_info : `gafaelfawr.models.token.TokenUserInfo`
            User information for the holder of that token.

        Raises
        ------
        gafaelfawr.exceptions.FirestoreError
            UID/GID allocation using Firestore failed, probably because the UID
            or GID space has been exhausted.
        gafaelfawr.exceptions.LDAPError
            Gafaelfawr was configured to get user groups, username, or numeric
            UID from LDAP, but the attempt failed due to some error.
        """
        username = token_data.username
        if self._ldap:
            ldap_user_info = await self._get_user_info_from_ldap(username)
            return TokenUserInfo(
                username=username,
                name=ldap_user_info.name or token_data.name,
                uid=token_data.uid,
                email=ldap_user_info.email or token_data.email,
                groups=ldap_user_info.groups,
            )
        else:
            return TokenUserInfo(
                username=token_data.username,
                name=token_data.name,
                uid=token_data.uid,
                email=token_data.email,
                groups=token_data.groups,
            )

    async def _get_gid_from_firestore(self, group: str) -> int:
        """Get the GID for a given user from Firestore.

        Parameters
        ----------
        group : `str`
            Group of the user.

        Returns
        -------
        gid : `int`
            GID of the user.

        Raises
        ------
        gafaelfawr.exceptions.NoAvailableGidError
            No more GIDs are available in that range.
        """
        if not self._firestore:
            raise RuntimeError("Firestore requested but not configured")
        gid = self._cache.get_gid(group)
        if gid:
            return gid
        async with self._cache.gid_lock:
            gid = self._cache.get_gid(group)
            if gid:
                return gid
            gid = await self._firestore.get_gid(group)
            self._cache.store_gid(group, gid)
            return gid

    async def _get_uid_from_firestore(self, username: str) -> int:
        """Get the UID for a given user.

        Parameters
        ----------
        username : `str`
            Username of the user.

        Returns
        -------
        uid : `int`
            UID of the user.

        Raises
        ------
        gafaelfawr.exceptions.NoAvailableUidError
            No more UIDs are available in that range.
        """
        if not self._firestore:
            raise RuntimeError("Firestore requested but not configured")
        uid = self._cache.get_uid(username)
        if uid:
            return uid
        async with self._cache.uid_lock:
            uid = self._cache.get_uid(username)
            if uid:
                return uid
            bot = re.search(BOT_USERNAME_REGEX, username) is not None
            uid = await self._firestore.get_uid(username, bot=bot)
            self._cache.store_uid(username, uid)
            return uid

    async def _get_user_info_from_ldap(self, username: str) -> TokenUserInfo:
        """Get user information from LDAP.

        Currently, this only obtains the group information.  It will be
        expanded to obtain the user's full name and email address.  This is
        merged with the user information stored with the token.  GIDs are
        optionally retrieved from Firestore rather than LDAP.

        Parameters
        ----------
        username : `str`
            Username for which to get information.

        Returns
        -------
        user_info : `gafaelfawr.models.token.TokenUserInfo`
            Information about that user from LDAP.

        Raises
        ------
        gafaelfawr.exceptions.LDAPError
            An error occurred when retrieving user information from LDAP.
        """
        if not self._ldap:
            raise RuntimeError("LDAP requested but not configured")
        add_gids = self._firestore is None
        async with self._ldap.connect() as conn:
            info = TokenUserInfo(username=username)
            info.groups = await conn.get_groups(username, add_gids=add_gids)
            if self._firestore:
                for group in info.groups:
                    group.id = await self._get_gid_from_firestore(group.name)
            return info


class OIDCUserInfoService(UserInfoService):
    """Retrieve user metadata from external systems for OIDC authentication.

    This is a specialization of `UserInfoService` when the upstream
    authentication provider is OpenID Connect.  It adds additional methods to
    extract user information from the OpenID Connect ID token.

    Parameters
    ----------
    config : `gafaelfawr.config.OIDCConfig`
        Configuration for the OpenID Connect authentication provider.
    cache : `gafaelfawr.dependencies.cache.IdCache`
        The underlying UID and GID cache and locks.
    ldap_storage : `gafaelfawr.storage.ldap.LDAPStorage`, optional
        The underlying LDAP storage for user metadata, if LDAP was
        configured.
    firestore : `gafaelfawr.storage.firestore.FirestoreStorage`, optional
        The underlying Firestore storage for UID and GID assignment, if
        Firestore was configured.
    logger : `structlog.stdlib.BoundLogger`
        Logger to use.
    """

    def __init__(
        self,
        *,
        config: OIDCConfig,
        cache: IdCache,
        ldap_storage: Optional[LDAPStorage],
        firestore: Optional[FirestoreStorage],
        logger: BoundLogger,
    ) -> None:
        super().__init__(
            cache=cache,
            ldap_storage=ldap_storage,
            firestore=firestore,
            logger=logger,
        )
        self._config = config

    async def get_user_info_from_oidc_token(
        self, token: OIDCVerifiedToken
    ) -> TokenUserInfo:
        """Return the metadata for a given user.

        Determine the user's username, numeric UID, and groups.  These may
        come from LDAP, from Firestore, or some combination, depending on
        configuration.  This is the data that we'll store with the token data
        in Redis.  It therefore only includes groups if we get them statically
        from the upstream authentication provider, not if they're read
        dynamically from LDAP.

        Parameters
        ----------
        token : `gafaelfawr.models.oidc.OIDCVerifiedToken`
            The verified ID token from the OpenID Connect provider.

        Returns
        -------
        user_info : `gafaelfawr.models.token.TokenUserInfo`
            User information derived from external data sources and the
            provided token.

        Raises
        ------
        gafaelfawr.exceptions.FirestoreError
            UID/GID allocation using Firestore failed, probably because the UID
            or GID space has been exhausted.
        gafaelfawr.exceptions.LDAPError
            Gafaelfawr was configured to get user groups, username, or numeric
            UID from LDAP, but the attempt failed due to some error.
        gafaelfawr.exceptions.NoUsernameMappingError
            The opaque authentication identity could not be mapped to a
            username, probably because the user is not enrolled.
        gafaelfawr.exceptions.VerifyTokenError
            The token is missing required claims.
        """
        username = None
        uid = None
        groups = None
        if self._ldap:
            async with self._ldap.connect() as conn:
                if "sub" in token.claims:
                    username = await conn.get_username(token.claims["sub"])
                if username is None:
                    username = self._get_username_from_oidc_token(token)
                if not self._firestore:
                    uid = await conn.get_uid(username)
        else:
            username = self._get_username_from_oidc_token(token)
            groups = await self._get_groups_from_oidc_token(token, username)
        if self._firestore:
            uid = await self._get_uid_from_firestore(username)
        elif not uid:
            uid = self._get_uid_from_oidc_token(token, username)

        return TokenUserInfo(
            username=username,
            name=token.claims.get("name"),
            email=token.claims.get("email"),
            uid=uid,
            groups=groups,
        )

    async def _get_groups_from_oidc_token(
        self,
        token: OIDCVerifiedToken,
        username: str,
    ) -> List[TokenGroup]:
        """Determine the user's groups from token claims.

        Invalid groups are logged and ignored.

        Parameters
        ----------
        token : `gafaelfawr.models.oidc.OIDCVerifiedToken`
            The previously verified token.
        username : `str`
            Authenticated username (for error reporting).

        Returns
        -------
        groups : List[`gafaelfawr.models.token.TokenGroup`]
            List of groups derived from the ``isMemberOf`` token claim.

        Raises
        ------
        gafaelfawr.exceptions.FirestoreError
            An error occured obtaining the GID from Firestore.
        gafaelfawr.exceptions.InvalidTokenClaimsError
            The ``isMemberOf`` claim has an invalid syntax.
        """
        groups = []
        invalid_groups = {}
        try:
            for oidc_group in token.claims.get("isMemberOf", []):
                if "name" not in oidc_group:
                    continue
                name = oidc_group["name"]
                try:
                    if self._firestore:
                        gid = await self._get_gid_from_firestore(name)
                    else:
                        if "id" not in oidc_group:
                            invalid_groups[name] = "missing id"
                            continue
                        gid = int(oidc_group["id"])
                    groups.append(TokenGroup(name=name, id=gid))
                except (TypeError, ValueError, ValidationError) as e:
                    invalid_groups[name] = str(e)
        except TypeError as e:
            msg = f"isMemberOf claim has invalid format: {str(e)}"
            self._logger.error(
                "Unable to get groups from token",
                error=msg,
                claim=token.claims.get("isMemberOf", []),
                user=username,
            )
            raise InvalidTokenClaimsError(msg)

        if invalid_groups:
            self._logger.warning(
                "Ignoring invalid groups in OIDC token",
                error="isMemberOf claim value could not be parsed",
                invalid_groups=invalid_groups,
                user=username,
            )

        return groups

    def _get_uid_from_oidc_token(
        self, token: OIDCVerifiedToken, username: str
    ) -> int:
        """Verify and return the numeric UID from the token.

        Parameters
        ----------
        token : `gafaelfawr.models.oidc.OIDCVerifiedToken`
            The previously verified token.
        username : `str`
            Authenticated username (for error reporting).

        Returns
        -------
        uid : `int`
            The numeric UID of the user as obtained from the token.

        Raises
        ------
        gafaelfawr.exceptions.MissingClaimsError
            The token is missing the required numeric UID claim.
        gafaelfawr.exceptions.InvalidTokenClaimsError
            The numeric UID claim contains something that is not a number.
        """
        if self._config.uid_claim not in token.claims:
            msg = f"No {self._config.uid_claim} claim in token"
            self._logger.warning(msg, claims=token.claims, user=username)
            raise MissingClaimsError(msg)
        try:
            uid = int(token.claims[self._config.uid_claim])
        except Exception:
            msg = f"Invalid {self._config.uid_claim} claim in token"
            self._logger.warning(msg, claims=token.claims, user=username)
            raise InvalidTokenClaimsError(msg)
        return uid

    def _get_username_from_oidc_token(self, token: OIDCVerifiedToken) -> str:
        """Verify and return the username from the token.

        Parameters
        ----------
        token : `gafaelfawr.models.oidc.OIDCVerifiedToken`
            The previously verified token.

        Returns
        -------
        username : `str`
            The username of the user as obtained from the token.

        Raises
        ------
        gafaelfawr.exceptions.MissingClaimsError
            The token is missing the required username claim.
        """
        if self._config.username_claim not in token.claims:
            msg = f"No {self._config.username_claim} claim in token"
            self._logger.warning(msg, claims=token.claims)
            raise MissingClaimsError(msg)
        return token.claims[self._config.username_claim]