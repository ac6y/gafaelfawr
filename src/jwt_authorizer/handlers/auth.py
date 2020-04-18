"""Handler for authentication and authorization checking (``/auth``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import jwt
from aiohttp import web

from jwt_authorizer.authnz import (
    authenticate,
    authorize,
    capabilities_from_groups,
)
from jwt_authorizer.handlers import routes
from jwt_authorizer.handlers.util import (
    build_capability_headers,
    forbidden,
    get_token_from_request,
    unauthorized,
)
from jwt_authorizer.session import Ticket

if TYPE_CHECKING:
    from jwt_authorizer.config import Config
    from jwt_authorizer.factory import ComponentFactory
    from logging import Logger
    from typing import Any, Mapping, Tuple

__all__ = ["get_auth"]


@routes.get("/auth")
async def get_auth(request: web.Request) -> web.Response:
    """Authenticate and authorize a token.

    Parameters
    ----------
    request : `aiohttp.web.Request`
        The incoming request, normally from nginx's ``auth_request``
        directive.

    Returns
    -------
    response : `aiohttp.web.Response`
        The response.

    Notes
    -----
    Expects the following query parameters to be set:

    capability
        One or more capabilities to check (required).
    satisfy
        Require that ``all`` (the default) or ``any`` of the capabilities
        requested via the ``capbility`` parameter be satisfied.

    Expects the following headers to be set in the request:

    Authorization
        The JWT token. This must always be the full JWT token. The token
        should be in this  header as type ``Bearer``, but it may be type
        ``Basic`` if ``x-oauth-basic`` is the username or password.
    X-Orig-Authorization
        The Authorization header as it was received before processing by
        ``oauth2_proxy``. This is useful when the original header was an
        ``oauth2_proxy`` ticket, as this gives access to the ticket.

    The following headers may be set in the response:

    X-Auth-Request-Email
        If enabled and email is available, this will be set based on the
        ``email`` claim.
    X-Auth-Request-User
        If enabled and the field is available, this will be set from token
        based on the ``JWT_USERNAME_KEY`` field.
    X-Auth-Request-Uid
        If enabled and the field is available, this will be set from token
        based on the ``JWT_UID_KEY`` field.
    X-Auth-Request-Groups
        When a token has groups available in the ``isMemberOf`` claim, the
        names of the groups will be returned, comma-separated, in this
        header.
    X-Auth-Request-Token
        If enabled, the encoded token will be set.
    X-Auth-Request-Token-Ticket
        When a ticket is available for the token, we will return it under this
        header.
    X-Auth-Request-Token-Capabilities
        If the token has capabilities in the ``scope`` claim, they will be
        returned in this header.
    X-Auth-Request-Token-Capabilities-Accepted
        A space-separated list of token capabilities the reliant resource
        accepts.
    X-Auth-Request-Token-Capabilities-Satisfy
        The strategy the reliant resource uses to accept a capability. Values
        include ``any`` or ``all``.
    WWW-Authenticate
        If the request is unauthenticated, this header will be set.
    """
    config: Config = request.config_dict["jwt_authorizer/config"]
    logger: Logger = request["safir/logger"]

    encoded_token = await get_token_from_request(request)
    if not encoded_token:
        logger.info("No token found, returning unauthorized")
        raise unauthorized(request, "Unable to find token")

    # Authentication
    try:
        verified_token = await authenticate(request, encoded_token)
    except jwt.PyJWTError as e:
        logger.exception("Failed to authenticate token")
        raise unauthorized(request, "Invalid token", message=str(e))

    # Authorization
    okay, message = authorize(request, verified_token)
    jti = verified_token.get("jti", "UNKNOWN")
    if okay:
        user_id = verified_token[config.uid_key]
        logger.info(
            f"Allowed token with Token ID={jti} for user={user_id} "
            f"from issuer={verified_token['iss']}"
        )
        return await success(request, encoded_token, verified_token)
    else:
        logger.error(f"Failed to authorize Token ID {jti} because {message}")
        raise forbidden(request, verified_token, message)


async def _check_reissue_token(
    request: web.Request, encoded_token: str, decoded_token: Mapping[str, Any]
) -> Tuple[str, str]:
    """Possibly reissue the token.

    Parameters
    ----------
    request : `aiohttp.web.Request`
        The incoming request.
    encoded_token : `str`
        The current token, encoded.
    decoded_token : `Mapping` [`str`, `Any`]
        The current token, decoded.

    Returns
    -------
    encoded_token : `str`
        An encoded token, which may have been reissued.
    oauth2_proxy_ticket_str : `str`
        A ticket for the oauth2_proxy session.

    Notes
    -----
    The token will be reissued under two scenarios.

    The first scenario is a newly logged in session with a cookie, indicated
    by the token being issued from another issuer.  We reissue the token with
    a default audience.

    The second scenario is a request to an internal resource, as indicated by
    the ``audience`` parameter being equal to the configured internal
    audience, where the current token's audience is from the default audience.
    We will reissue the token with an internal audience.
    """
    config: Config = request.config_dict["jwt_authorizer/config"]
    factory: ComponentFactory = request.config_dict["jwt_authorizer/factory"]

    # Only reissue token if it's requested and if it's a different
    # issuer than this application uses to reissue a token
    iss = config.issuer.iss
    default_audience = config.issuer.aud
    internal_audience = config.issuer.aud_internal
    to_internal_audience = request.query.get("audience") == internal_audience
    from_this_issuer = decoded_token["iss"] == iss
    from_default_audience = decoded_token["aud"] == default_audience
    cookie_name = config.session_store.ticket_prefix
    ticket_str = request.cookies.get(cookie_name, "")
    ticket = None
    new_audience = None
    if not from_this_issuer:
        # If we didn't issue the token, it came from a provider as part of a
        # new session. This only happens once, after initial login, so there
        # should always be a cookie set. If there isn't, or we fail to parse
        # it, something funny is going on and we can abort with an exception.
        ticket = Ticket.from_cookie(cookie_name, ticket_str)

        # Make a copy of the previous token and add capabilities
        decoded_token = dict(decoded_token)
        decoded_token["scope"] = " ".join(
            sorted(
                capabilities_from_groups(decoded_token, config.group_mapping)
            )
        )
        new_audience = config.issuer.aud
    elif from_this_issuer and from_default_audience and to_internal_audience:
        # In this case, we only reissue tokens from a default audience
        new_audience = config.issuer.aud_internal
        ticket = Ticket()

    if new_audience:
        assert ticket
        issuer = factory.create_token_issuer()
        encoded_token = await issuer.reissue_token(
            decoded_token, ticket, internal=to_internal_audience
        )

    return encoded_token, ticket.encode(cookie_name) if ticket else ""


async def success(
    request: web.Request, encoded_token: str, verified_token: Mapping[str, Any]
) -> web.Response:
    """Construct a response for successful authorization.

    Parameters
    ----------
    request : `aiohttp.web.Request`
        The incoming request.
    encoded_token : `str`
        The token encoded as a JWT.
    verified_token : `Mapping` [`str`, `Any`]
        A verified token containing group and scope information.

    Returns
    -------
    response : `aiohttp.web.Resposne`
        Response to send.
    """
    config: Config = request.config_dict["jwt_authorizer/config"]

    headers = build_capability_headers(request, verified_token)

    if config.set_user_headers:
        email = verified_token.get("email")
        user = verified_token.get(config.username_key)
        uid = verified_token.get(config.uid_key)
        groups_list = verified_token.get("isMemberOf", list())
        if email:
            headers["X-Auth-Request-Email"] = email
        if user:
            headers["X-Auth-Request-User"] = user
        if uid:
            headers["X-Auth-Request-Uid"] = uid
        if groups_list:
            groups = ",".join([g["name"] for g in groups_list])
            headers["X-Auth-Request-Groups"] = groups

    encoded_token, oauth2_proxy_ticket = await _check_reissue_token(
        request, encoded_token, verified_token
    )
    headers["X-Auth-Request-Token"] = encoded_token
    headers["X-Auth-Request-Token-Ticket"] = oauth2_proxy_ticket
    return web.Response(headers=headers, text="ok")