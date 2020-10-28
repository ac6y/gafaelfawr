"""Configuration for Gafaelfawr.

There are two, mostly-parallel models defined here.  The ones ending in
``Settings`` are the pydantic models used to read the settings file from disk,
the root of which is `Settings`.  This is then processed and broken up into
configuration dataclasses for various components and then exposed to the rest
of Gafaelfawr as the `Config` object.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from ipaddress import _BaseNetwork
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

import yaml
from pydantic import AnyHttpUrl, BaseModel, IPvAnyNetwork, validator

from gafaelfawr.keypair import RSAKeyPair

__all__ = [
    "Config",
    "GitHubConfig",
    "GitHubSettings",
    "IssuerAudienceSettings",
    "IssuerConfig",
    "IssuerSettings",
    "OIDCConfig",
    "OIDCClient",
    "OIDCServerConfig",
    "OIDCSettings",
    "SafirConfig",
    "Settings",
    "VerifierConfig",
]


class IssuerAudienceSettings(BaseModel):
    """pydantic model of issuer audience configuration."""

    default: str
    """Default aud (audience) field in issued tokens."""

    internal: str
    """Internal aud (audience) field in issued tokens."""


class IssuerSettings(BaseModel):
    """pydantic model of issuer configuration."""

    iss: str
    """iss (issuer) field in issued tokens."""

    key_id: str
    """kid (key ID) header field in issued tokens."""

    aud: IssuerAudienceSettings
    """aud (audience) possibilities for issued tokens."""

    key_file: str
    """File containing RSA private key for signing issued tokens."""

    exp_minutes: int = 1440  # 1 day
    """Number of minutes into the future that a token should expire."""

    influxdb_secret_file: Optional[str] = None
    """File containing shared secret for issuing InfluxDB tokens."""

    influxdb_username: Optional[str] = None
    """The username to set in all InfluxDB tokens."""


class GitHubSettings(BaseModel):
    """pydantic model of GitHub configuration."""

    client_id: str
    """Client ID of the GitHub App."""

    client_secret_file: str
    """File containing secret for the GitHub App."""


class OIDCSettings(BaseModel):
    """pydantic model of OpenID Connect configuration."""

    client_id: str
    """Client ID for talking to the OpenID Connect provider."""

    client_secret_file: str
    """File containing secret for talking to the OpenID Connect provider."""

    login_url: AnyHttpUrl
    """URL to which to send the user to initiate authentication."""

    login_params: Dict[str, str] = {}
    """Additional parameters to the login URL."""

    redirect_url: AnyHttpUrl
    """Return URL to which the authentication provider should send the user.

    This should be the full URL of the /login route of Gafaelfawr.
    """

    token_url: AnyHttpUrl
    """URL at which to redeem the authentication code for a token."""

    scopes: List[str] = []
    """Scopes to request from the authentication provider.

    The ``openid`` scope will always be added and does not need to be
    specified.
    """

    issuer: str
    """Expected issuer of the ID token."""

    audience: str
    """Expected audience of the ID token."""

    key_ids: List[str] = []
    """List of acceptable kids that may be used to sign the ID token."""


class Settings(BaseModel):
    """pydantic model of Gafaelfawr settings file.

    This describes the settings file as parsed from disk.  This model will be
    converted to a `Config` dataclass for internal use so that some settings
    can be duplicated, rewritten, or parsed into internal formats for later
    convenience.

    Several fields use an empty dictionary or empty list as a default value.
    Due to a quirk in how Python handles empty dict and list constructors, the
    caller must be careful to never modify those fields and instead treat the
    value as read-only.  In practice, this isn't much of a concern since this
    object is only used to convert to a `Config` object.
    """

    realm: str
    """Realm for HTTP authentication."""

    loglevel: str = "INFO"
    """Logging level."""

    session_secret_file: str
    """File containing encryption secret for session cookie and store."""

    redis_url: str
    """URL for the Redis server that stores sessions."""

    redis_password_file: Optional[str] = None
    """File containing the password to use when connecting to Redis."""

    proxies: Optional[List[IPvAnyNetwork]]
    """Trusted proxy IP netblocks in front of Gafaelfawr.

    If this is set to a non-empty list, it will be used as the trusted list of
    proxies when parsing ``X-Forwarded-For`` for the ``/auth`` route.  IP
    addresses from that header will be discarded from the right side when they
    are within a netblock in this list until a non-matching IP is reached or
    there is only one IP left, and then that IP will be used as the remote IP
    for logging purposes.  This will allow logging of accurate client IP
    addresses.
    """

    after_logout_url: AnyHttpUrl
    """Default URL to which to send the user after logging out."""

    username_claim: str = "uid"
    """Name of claim to use as the username."""

    uid_claim: str = "uidNumber"
    """Name of claim to use as the UID."""

    issuer: IssuerSettings
    """Settings for the internal token issuer."""

    database_url: str
    """URL for the PostgreSQL database."""

    initial_admins: List[str]
    """Initial token administrators to configure when initializing database."""

    github: Optional[GitHubSettings] = None
    """Settings for the GitHub authentication provider."""

    oidc: Optional[OIDCSettings] = None
    """Settings for the OpenID Connect authentication provider."""

    oidc_server_secrets_file: Optional[str] = None
    """Path to file containing OpenID Connect client secrets in JSON."""

    known_scopes: Dict[str, str] = {}
    """Known scopes (the keys) and their descriptions (the values)."""

    group_mapping: Dict[str, List[str]] = {}
    """Mappings of scopes to lists of groups that provide them."""

    class Config:
        env_prefix = "GAFAELFAWR_"

    @validator("loglevel")
    def valid_loglevel(cls, v: str) -> str:
        level = getattr(logging, v, None)
        if not level:
            raise ValueError("invalid logging level")
        return v

    @validator("oidc", always=True)
    def exactly_one_provider(
        cls, v: Optional[OIDCSettings], values: Dict[str, object]
    ) -> Optional[OIDCSettings]:
        """Ensure either github or oidc is set, not both."""
        if v and "github" in values and values["github"]:
            raise ValueError("both github and oidc settings present")
        if not v and ("github" not in values or not values["github"]):
            raise ValueError("neither github nor oidc settings present")
        return v

    @validator("initial_admins", pre=True)
    def nonempty_list(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("initial_admins is empty")
        return v


@dataclass(frozen=True)
class SafirConfig:
    """Safir configuration for Gafaelfawr.

    These configuration settings are used by the Safir middleware.
    """

    log_level: str
    """The log level of the application's logger.

    Takes the first value of the following that is set:

    - The ``SAFIR_LOG_LEVEL`` environment variable.
    - The ``loglevel`` Gafaelfawr configuration setting.
    - ``INFO``
    """

    name: str = os.getenv("SAFIR_NAME", "gafaelfawr")
    """The application's name, which doubles as the root HTTP endpoint path.

    Set with the ``SAFIR_NAME`` environment variable.
    """

    profile: str = os.getenv("SAFIR_PROFILE", "production")
    """Application run profile: "development" or "production".

    Set with the ``SAFIR_PROFILE`` environment variable.
    """

    logger_name: str = os.getenv("SAFIR_LOGGER", "gafaelfawr")
    """The root name of the application's logger.

    Set with the ``SAFIR_LOGGER`` environment variable.
    """


@dataclass(frozen=True)
class IssuerConfig:
    """Configuration for how to issue tokens."""

    iss: str
    """iss (issuer) field in issued tokens."""

    kid: str
    """kid (key ID) header field in issued tokens."""

    aud: str
    """Default aud (audience) field in issued tokens."""

    aud_internal: str
    """Internal aud (audience) field in issued tokens."""

    keypair: RSAKeyPair
    """RSA key pair for signing and verifying issued tokens."""

    exp_minutes: int
    """Number of minutes into the future that a token should expire."""

    group_mapping: Mapping[str, FrozenSet[str]]
    """Mapping of group names to the set of scopes that group grants."""

    username_claim: str
    """Token claim from which to take the username."""

    uid_claim: str
    """Token claim from which to take the UID."""

    influxdb_secret: Optional[str]
    """Shared secret for issuing InfluxDB authentication tokens."""

    influxdb_username: Optional[str]
    """The username to set in all InfluxDB tokens."""


@dataclass(frozen=True)
class VerifierConfig:
    """Configuration for how to verify tokens."""

    iss: str
    """iss (issuer) field in issued tokens."""

    aud: str
    """Default aud (audience) field in issued tokens."""

    aud_internal: str
    """Internal aud (audience) field in issued tokens."""

    keypair: RSAKeyPair
    """RSA key pair for signing and verifying issued tokens."""

    username_claim: str
    """Token claim from which to take the username."""

    uid_claim: str
    """Token claim from which to take the UID."""

    oidc_iss: Optional[str]
    """Expected issuer of the ID token from an OpenID Connect provider."""

    oidc_aud: Optional[str]
    """Expected audience of the ID token an OpenID Connect provider."""

    oidc_kids: Tuple[str, ...]
    """List of acceptable kids that may be used to sign the ID token."""


@dataclass(frozen=True)
class GitHubConfig:
    """Metadata for GitHub authentication.

    Some configuration parameters are duplicated from the main application
    configuration so that all of the configuration for the GitHub provider is
    encapsulated here.
    """

    client_id: str
    """Client ID of the GitHub App."""

    client_secret: str
    """Secret for the GitHub App."""

    username_claim: str
    """Name of claim in which to store the username."""

    uid_claim: str
    """Name of claim in which to store the UID."""


@dataclass(frozen=True)
class OIDCConfig:
    """Configuration for OpenID Connect authentication."""

    client_id: str
    """Client ID for talking to the OpenID Connect provider."""

    client_secret: str
    """Secret for talking to the OpenID Connect provider."""

    login_url: str
    """URL to which to send the user to initiate authentication."""

    login_params: Mapping[str, str]
    """Additional parameters to the login URL."""

    redirect_url: str
    """Return URL to which the authentication provider should send the user.

    This should be the full URL of the /login route of Gafaelfawr.
    """

    token_url: str
    """URL at which to redeem the authentication code for a token."""

    scopes: Tuple[str, ...]
    """Scopes to request from the authentication provider.

    The ``openid`` scope will always be added and does not need to be
    specified.
    """

    issuer: str
    """Expected issuer of the ID token."""

    audience: str
    """Expected audience of the ID token."""

    key_ids: Tuple[str, ...]
    """List of acceptable kids that may be used to sign the ID token."""


@dataclass(frozen=True)
class OIDCClient:
    """Configuration for a single OpenID Connect client of our server."""

    client_id: str
    """Unique identifier of the client."""

    client_secret: str
    """Secret used to authenticate this client."""


@dataclass(frozen=True)
class OIDCServerConfig:
    """Configuration for the OpenID Connect server."""

    clients: Tuple[OIDCClient, ...]
    """Supported OpenID Connect clients."""


@dataclass(frozen=True)
class Config:
    """Configuration for Gafaelfawr.

    The internal representation of the configuration, created from the
    `Settings` model.

    Some configuration parameters from the settings file are copied into
    multiple configuration dataclasses.  This allows the configuration for
    each internal component to be self-contained and unaware of the
    configuration of the rest of the application.
    """

    realm: str
    """Realm for HTTP authentication."""

    session_secret: str
    """Secret used to encrypt the session cookie and session store."""

    redis_url: str
    """URL for the Redis server that stores sessions."""

    redis_password: Optional[str]
    """Password for the Redis server that stores sessions."""

    proxies: Tuple[_BaseNetwork, ...]
    """Trusted proxy IP netblocks in front of Gafaelfawr.

    If this is set to a non-empty list, it will be used as the trusted list of
    proxies when parsing ``X-Forwarded-For`` for the ``/auth`` route.  IP
    addresses from that header will be discarded from the right side when they
    match an entry in this list until a non-matching IP is reached or there is
    only one IP left, and then that IP will be used as the remote IP for
    logging purposes.  This will allow logging of accurate client IP
    addresses.
    """

    after_logout_url: str
    """Default URL to which to send the user after logging out."""

    issuer: IssuerConfig
    """Configuration for internally-issued tokens."""

    verifier: VerifierConfig
    """Configuration for the token verifier."""

    github: Optional[GitHubConfig]
    """Configuration for GitHub authentication."""

    oidc: Optional[OIDCConfig]
    """Configuration for OpenID Connect authentication."""

    oidc_server: Optional[OIDCServerConfig]
    """Configuration for the OpenID Connect server."""

    known_scopes: Mapping[str, str]
    """Known scopes (the keys) and their descriptions (the values)."""

    database_url: str
    """URL for the PostgreSQL database."""

    initial_admins: Tuple[str, ...]
    """Initial token administrators to configure when initializing database."""

    safir: SafirConfig
    """Configuration for the Safir middleware."""

    @classmethod
    def from_file(cls, path: str, **overrides: Any) -> Config:
        """Construct a Config object from a settings file.

        Parameters
        ----------
        path : `str`
            Path to the settings file in YAML.
        **overrides : `typing.Any`
            Settings that override settings read from the configuration file.

        Returns
        -------
        config : `Config`
            The corresponding Config object.
        """
        with open(path, "r") as f:
            raw_settings = yaml.safe_load(f)
        raw_settings.update(overrides)
        settings = Settings.parse_obj(raw_settings)

        # Load the secrets from disk.
        key = cls._load_secret(settings.issuer.key_file)
        keypair = RSAKeyPair.from_pem(key)
        session_secret = cls._load_secret(settings.session_secret_file)
        redis_password = None
        if settings.redis_password_file:
            path = settings.redis_password_file
            redis_password = cls._load_secret(path).decode()
        influxdb_secret = None
        if settings.issuer.influxdb_secret_file:
            path = settings.issuer.influxdb_secret_file
            influxdb_secret = cls._load_secret(path).decode()
        if settings.github:
            path = settings.github.client_secret_file
            github_secret = cls._load_secret(path).decode()
        if settings.oidc:
            path = settings.oidc.client_secret_file
            oidc_secret = cls._load_secret(path).decode()

        # If there is an OpenID Connect server configuration, load it from a
        # file in JSON format.  (It contains secrets.)
        oidc_server_config = None
        if settings.oidc_server_secrets_file:
            path = settings.oidc_server_secrets_file
            oidc_secrets_json = cls._load_secret(path).decode()
            oidc_secrets = json.loads(oidc_secrets_json)
            oidc_clients = tuple(
                (
                    OIDCClient(client_id=c["id"], client_secret=c["secret"])
                    for c in oidc_secrets
                )
            )
            oidc_server_config = OIDCServerConfig(clients=oidc_clients)

        # The group mapping in the settings maps a scope to a list of groups
        # that provide that scope.  This may be conceptually easier for the
        # person writing the configuration, but for our purposes we want a map
        # from a group name to a set of scopes that group provides.
        #
        # Reconstruct the group mapping in the form in which we want to use it
        # internally.
        group_mapping = defaultdict(set)
        for scope, groups in settings.group_mapping.items():
            for group in groups:
                group_mapping[group].add(scope)
        group_mapping_frozen = {
            k: frozenset(v) for k, v in group_mapping.items()
        }

        # Build the Config object.
        issuer_config = IssuerConfig(
            iss=settings.issuer.iss,
            kid=settings.issuer.key_id,
            aud=settings.issuer.aud.default,
            aud_internal=settings.issuer.aud.internal,
            keypair=keypair,
            exp_minutes=settings.issuer.exp_minutes,
            group_mapping=group_mapping_frozen,
            username_claim=settings.username_claim,
            uid_claim=settings.uid_claim,
            influxdb_secret=influxdb_secret,
            influxdb_username=settings.issuer.influxdb_username,
        )
        verifier_config = VerifierConfig(
            iss=settings.issuer.iss,
            aud=settings.issuer.aud.default,
            aud_internal=settings.issuer.aud.internal,
            keypair=keypair,
            username_claim=settings.username_claim,
            uid_claim=settings.uid_claim,
            oidc_iss=settings.oidc.issuer if settings.oidc else None,
            oidc_aud=settings.oidc.audience if settings.oidc else None,
            oidc_kids=tuple(settings.oidc.key_ids if settings.oidc else []),
        )
        github_config = None
        if settings.github:
            github_config = GitHubConfig(
                client_id=settings.github.client_id,
                client_secret=github_secret,
                username_claim=settings.username_claim,
                uid_claim=settings.uid_claim,
            )
        oidc_config = None
        if settings.oidc:
            oidc_config = OIDCConfig(
                client_id=settings.oidc.client_id,
                client_secret=oidc_secret,
                login_url=str(settings.oidc.login_url),
                login_params=settings.oidc.login_params,
                redirect_url=str(settings.oidc.redirect_url),
                token_url=str(settings.oidc.token_url),
                scopes=tuple(settings.oidc.scopes),
                issuer=settings.oidc.issuer,
                audience=settings.oidc.audience,
                key_ids=tuple(settings.oidc.key_ids),
            )
        log_level = os.getenv("SAFIR_LOG_LEVEL", settings.loglevel)
        return cls(
            realm=settings.realm,
            session_secret=session_secret.decode(),
            redis_url=settings.redis_url,
            redis_password=redis_password,
            proxies=tuple(settings.proxies if settings.proxies else []),
            after_logout_url=str(settings.after_logout_url),
            issuer=issuer_config,
            verifier=verifier_config,
            github=github_config,
            oidc=oidc_config,
            oidc_server=oidc_server_config,
            known_scopes=settings.known_scopes or {},
            database_url=settings.database_url,
            initial_admins=tuple(settings.initial_admins),
            safir=SafirConfig(log_level=log_level),
        )

    @staticmethod
    def _load_secret(path: str) -> bytes:
        """Load a secret from a file."""
        with open(path, "rb") as fh:
            secret = fh.read().strip()
            assert len(secret), f"Secret file {path} is empty"
            return secret
