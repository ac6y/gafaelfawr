"""Handler for authentication and authorization checking (``/auth``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from safir.datetime import current_datetime
from safir.models import ErrorModel
from safir.slack.webhook import SlackRouteErrorHandler

from ..auth import (
    clean_authorization,
    clean_cookies,
    generate_challenge,
    generate_unauthorized_challenge,
)
from ..constants import MINIMUM_LIFETIME
from ..dependencies.auth import AuthenticateRead
from ..dependencies.context import RequestContext, context_dependency
from ..exceptions import (
    ExternalUserInfoError,
    InsufficientScopeError,
    InvalidDelegateToError,
    InvalidMinimumLifetimeError,
    InvalidServiceError,
    InvalidTokenError,
)
from ..models.auth import AuthType, Satisfy
from ..models.token import TokenData
from ..util import is_mobu_bot_user

router = APIRouter(route_class=SlackRouteErrorHandler)

__all__ = ["router"]


@dataclass
class AuthConfig:
    """Configuration for an authorization request."""

    auth_type: AuthType
    """The authentication type to use in challenges."""

    delegate_scopes: set[str]
    """List of scopes the delegated token should have."""

    delegate_to: str | None
    """Internal service for which to create an internal token."""

    minimum_lifetime: timedelta | None
    """Required minimum lifetime of the token."""

    notebook: bool
    """Whether to generate a notebook token."""

    satisfy: Satisfy
    """The authorization strategy if multiple scopes are required."""

    scopes: set[str]
    """The scopes the authentication token must have."""

    service: str | None
    """Name of the service for which authorization is being checked."""

    use_authorization: bool
    """Whether to put any delegated token in the ``Authorization`` header."""

    username: str | None
    """Restrict access to the ingress to only this username."""


def auth_uri(
    *,
    x_original_uri: Annotated[
        str | None,
        Header(description="URL for which authorization is being checked"),
    ] = None,
    x_original_url: Annotated[
        str | None,
        Header(
            description=(
                "URL for which authorization is being checked."
                " `X-Original-URI` takes precedence if both are set."
            ),
        ),
    ] = None,
) -> str:
    """Determine URL for which we're validating authentication.

    ``X-Original-URI`` will only be set if the auth-method annotation is set.
    That is recommended, but allow for the case where it isn't set and fall
    back on ``X-Original-URL``, which is set unconditionally.
    """
    return x_original_uri or x_original_url or "NONE"


def auth_config(
    *,
    auth_type: Annotated[
        AuthType,
        Query(
            title="Challenge type",
            description="Type of `WWW-Authenticate` challenge to return",
            examples=["basic"],
        ),
    ] = AuthType.Bearer,
    delegate_to: Annotated[
        str | None,
        Query(
            title="Service name",
            description="Create an internal token for the named service",
            examples=["some-service"],
        ),
    ] = None,
    delegate_scope: Annotated[
        str | None,
        Query(
            title="Scope of delegated token",
            description=(
                "Comma-separated list of scopes to add to the delegated token."
                " All listed scopes are implicitly added to the scope"
                " requirements for authorization."
            ),
            examples=["read:all,write:all"],
        ),
    ] = None,
    minimum_lifetime: Annotated[
        int | None,
        Query(
            title="Required minimum lifetime",
            description=(
                "Force reauthentication if the delegated token (internal or"
                " notebook) would have a shorter lifetime, in seconds, than"
                " this parameter."
            ),
            ge=MINIMUM_LIFETIME.total_seconds(),
            examples=[86400],
        ),
    ] = None,
    notebook: Annotated[
        bool,
        Query(
            title="Request notebook token",
            description=(
                "Cannot be used with `delegate_to` or `delegate_scope`"
            ),
            examples=[True],
        ),
    ] = False,
    satisfy: Annotated[
        Satisfy,
        Query(
            title="Scope matching policy",
            description=(
                "Set to `all` to require all listed scopes, set to `any` to"
                " require any of the listed scopes"
            ),
            examples=["any"],
        ),
    ] = Satisfy.ALL,
    scope: Annotated[
        list[str],
        Query(
            title="Required scopes",
            description=(
                "If given more than once, meaning is determined by the"
                " `satisfy` parameter"
            ),
            examples=["read:all"],
        ),
    ],
    service: Annotated[
        str | None,
        Query(
            title="Service",
            description="Name of the underlying service",
            examples=["tap"],
        ),
    ] = None,
    use_authorization: Annotated[
        bool,
        Query(
            title="Put delegated token in Authorization",
            description=(
                "If true, also replace the Authorization header with any"
                " delegated token, passed as a bearer token."
            ),
            examples=[True],
        ),
    ] = False,
    username: Annotated[
        str | None,
        Query(
            title="Restrict to username",
            description=(
                "Only allow access to this ingress by the user with the given"
                " username. All other users, regardless of scopes, will"
                " receive 403 errors. The user must still meet the scope"
                " requirements for the ingress."
            ),
            examples=["rra"],
        ),
    ] = None,
    auth_uri: Annotated[str, Depends(auth_uri)],
    context: Annotated[RequestContext, Depends(context_dependency)],
) -> AuthConfig:
    """Construct the configuration for an authorization request.

    A shared dependency that reads various GET parameters and headers and
    converts them into an `AuthConfig` class.

    Raises
    ------
    InvalidDelegateToError
        Raised if ``notebook`` and ``delegate_to`` are both set.
    InvalidServiceError
        Raised if ``service`` is set to something different than
        ``delegate_to``.
    """
    if notebook and delegate_to:
        msg = "delegate_to cannot be set for notebook tokens"
        raise InvalidDelegateToError(msg)
    if service and delegate_to and service != delegate_to:
        msg = "service must be the same as delegate_to"
        raise InvalidServiceError(msg)
    scopes = set(scope)
    context.rebind_logger(
        auth_uri=auth_uri,
        required_scopes=sorted(scopes),
        satisfy=satisfy.name.lower(),
    )
    if username:
        context.rebind_logger(required_user=username)

    if delegate_scope:
        delegate_scopes = {s.strip() for s in delegate_scope.split(",")}
    else:
        delegate_scopes = set()
    lifetime = None
    if minimum_lifetime:
        lifetime = timedelta(seconds=minimum_lifetime)
    elif not minimum_lifetime and (notebook or delegate_to):
        lifetime = MINIMUM_LIFETIME
    return AuthConfig(
        auth_type=auth_type,
        delegate_scopes=delegate_scopes,
        delegate_to=delegate_to,
        minimum_lifetime=lifetime,
        notebook=notebook,
        satisfy=satisfy,
        scopes=scopes,
        service=service,
        use_authorization=use_authorization,
        username=username,
    )


async def authenticate_with_type(
    *,
    auth_type: Annotated[
        AuthType,
        Query(
            title="Challenge type",
            description=(
                "Control the type of WWW-Authenticate challenge returned"
            ),
            examples=["basic"],
        ),
    ] = AuthType.Bearer,
    context: Annotated[RequestContext, Depends(context_dependency)],
) -> TokenData:
    """Set authentication challenge based on auth_type parameter."""
    authenticate = AuthenticateRead(auth_type=auth_type, ajax_forbidden=True)
    return await authenticate(context=context)


@router.get(
    "/auth",
    description="Meant to be used as an NGINX auth_request handler",
    responses={
        400: {"description": "Bad request", "model": ErrorModel},
        401: {"description": "Unauthenticated"},
        403: {"description": "Permission denied"},
    },
    summary="Authenticate user",
    tags=["internal"],
)
async def get_auth(
    *,
    auth_config: Annotated[AuthConfig, Depends(auth_config)],
    token_data: Annotated[TokenData, Depends(authenticate_with_type)],
    context: Annotated[RequestContext, Depends(context_dependency)],
    response: Response,
) -> dict[str, str]:
    check_lifetime(context, auth_config, token_data)

    # Determine whether the request is authorized.
    if auth_config.satisfy == Satisfy.ANY:
        authorized = any(s in token_data.scopes for s in auth_config.scopes)
    else:
        authorized = all(s in token_data.scopes for s in auth_config.scopes)
    if not authorized:
        raise generate_challenge(
            context,
            auth_config.auth_type,
            InsufficientScopeError("Token missing required scope"),
            auth_config.scopes,
        )

    # Check a user constraint. InsufficientScopeError is not really correct,
    # but none of the RFC 6750 error codes are correct and it's the closest.
    if auth_config.username and token_data.username != auth_config.username:
        raise generate_challenge(
            context,
            auth_config.auth_type,
            InsufficientScopeError("Access not allowed for this user"),
            auth_config.scopes,
        )

    # Log and return the results.
    context.logger.info("Token authorized")
    headers = await build_success_headers(context, auth_config, token_data)
    for key, value in headers:
        response.headers.append(key, value)
    if context.metrics and not is_mobu_bot_user(token_data.username):
        attrs = {"username": token_data.username}
        if auth_config.service:
            attrs["service"] = auth_config.service
        context.metrics.request_auth.add(1, attrs)
    return {"status": "ok"}


@router.get(
    "/auth/anonymous",
    description=(
        "Intended for use as an auth-url handler for anonymous routes. No"
        " authentication is done and no authorization checks are performed,"
        " but the `Authorization` and `Cookie` headers are still reflected"
        " in the response with Gafaelfawr tokens and cookies stripped."
    ),
    summary="Filter headers for anonymous routes",
    tags=["internal"],
)
async def get_anonymous(
    *,
    context: Annotated[RequestContext, Depends(context_dependency)],
    response: Response,
) -> dict[str, str]:
    if "Authorization" in context.request.headers:
        raw_authorizations = context.request.headers.getlist("Authorization")
        authorizations = clean_authorization(raw_authorizations)
        for authorization in authorizations:
            response.headers.append("Authorization", authorization)
    if "Cookie" in context.request.headers:
        raw_cookies = context.request.headers.getlist("Cookie")
        cookies = clean_cookies(raw_cookies)
        for cookie in cookies:
            response.headers.append("Cookie", cookie)
    return {"status": "ok"}


def check_lifetime(
    context: RequestContext, auth_config: AuthConfig, token_data: TokenData
) -> None:
    """Check if the token lifetime is long enough.

    This check is done prior to getting the delegated token during the initial
    authentication check. The timing of the check is a bit awkward, since the
    semantic request is a minimum lifetime of any delegated internal or
    notebook token we will pass along.  However, getting the latter is more
    expensive: we would have to do all the work of creating the token, then
    retrieve it from Redis, and then check its lifetime.

    Thankfully, we can know in advance whether the token we will create will
    have a long enough lifetime. We can request tokens up to the lifetime of
    the parent token and therefore can check the required lifetime against the
    lifetime of the parent token as long as we require the child token have
    the required lifetime (which we do, in `build_success_headers`).

    The only special case we need to handle is where the required lifetime is
    too close to the maximum lifetime for new tokens, since the lifetime of
    delegated tokens will be capped at that. In this case, we can never
    satisfy this request and need to raise a 422 error instead of a 401 or 403
    error. We don't allow required lifetimes within ``MINIMUM_LIFETIME`` of
    the maximum lifetime to avoid the risk of a slow infinite redirect loop
    when the login process takes a while.

    Parameters
    ----------
    context
        The context of the incoming request.
    auth_config
        Configuration parameters for the authorization.
    token_data
        The data from the authentication token.

    Raises
    ------
    fastapi.HTTPException
        Raised if the minimum lifetime is not satisfied. This will be a 401 or
        403 HTTP error as appropriate.
    InvalidMinimumLifetime
        Raised if the specified minimum lifetime is longer than the maximum
        lifetime of a token minus the minimum remaining lifetime and therefore
        cannot be satisfied.
    """
    if not auth_config.minimum_lifetime:
        return
    max_lifetime = context.config.token_lifetime - MINIMUM_LIFETIME
    if auth_config.minimum_lifetime > max_lifetime:
        min_seconds = int(auth_config.minimum_lifetime.total_seconds())
        max_seconds = int(max_lifetime.total_seconds())
        msg = (
            f"Requested lifetime {min_seconds}s longer than maximum lifetime"
            f" {max_seconds}s"
        )
        raise InvalidMinimumLifetimeError(msg)
    if token_data.expires:
        lifetime = token_data.expires - current_datetime()
        if auth_config.minimum_lifetime > lifetime:
            raise generate_unauthorized_challenge(
                context,
                auth_config.auth_type,
                InvalidTokenError("Remaining token lifetime too short"),
                ajax_forbidden=True,
            )


async def build_success_headers(
    context: RequestContext, auth_config: AuthConfig, token_data: TokenData
) -> list[tuple[str, str]]:
    """Construct the headers for successful authorization.

    The following headers may be included:

    X-Auth-Request-Email
        The email address of the authenticated user, if known.
    X-Auth-Request-User
        The username of the authenticated user.
    X-Auth-Request-Token
        If requested by ``notebook`` or ``delegate_to``, will be set to the
        delegated token.
    Authorization
        The input ``Authorization`` headers with any headers containing
        Gafaelfawr tokens stripped.
    Cookie
        The input ``Cookie`` headers with any cookie values containing
        Gafaelfawr tokens stripped.

    Parameters
    ----------
    context
        The context of the incoming request.
    auth_config
        Configuration parameters for the authorization.
    token_data
        The data from the authentication token.

    Returns
    -------
    headers
        Headers to include in the response.

    Raises
    ------
    fastapi.HTTPException
        Raised if user information could not be retrieved from external
        systems.
    """
    info_service = context.factory.create_user_info_service()
    try:
        user_info = await info_service.get_user_info_from_token(token_data)
    except ExternalUserInfoError as e:
        # Catch these exceptions rather than raising an uncaught exception or
        # reporting the exception to Slack. This route is called on every user
        # request and may be called multiple times per second, so if we
        # reported every exception during an LDAP outage to Slack, we would
        # get rate-limited or destroy the Slack channel. Instead, log the
        # exception and return 403 and rely on failures during login (which
        # are reported to Slack) and external testing to detect these
        # problems.
        msg = "Unable to get user information"
        context.logger.exception(msg, user=token_data.username, error=str(e))
        raise HTTPException(
            headers={"Cache-Control": "no-cache, no-store"},
            status_code=500,
            detail=[{"msg": msg, "type": "user_info_failed"}],
        ) from e

    headers = [("X-Auth-Request-User", token_data.username)]
    if user_info.email:
        headers.append(("X-Auth-Request-Email", user_info.email))

    # Add the delegated token, if there should be one.
    delegated = await build_delegated_token(context, auth_config, token_data)
    if delegated:
        headers.append(("X-Auth-Request-Token", delegated))

    # If told to put the delegated token in the Authorization header, do that.
    # Otherwise, strip authentication tokens from the Authorization headers of
    # the incoming request and reflect the remainder back in the response.
    # Always do this with the Cookie header. ingress-nginx can then be
    # configured to lift those headers up into the proxy request, preventing
    # the user's cookie from being passed down to the protected application.
    if auth_config.use_authorization:
        if delegated:
            headers.append(("Authorization", f"Bearer {delegated}"))
    elif "Authorization" in context.request.headers:
        raw_authorizations = context.request.headers.getlist("Authorization")
        authorizations = clean_authorization(raw_authorizations)
        if authorizations:
            headers.extend(("Authorization", v) for v in authorizations)
    if "Cookie" in context.request.headers:
        raw_cookies = context.request.headers.getlist("Cookie")
        cookies = clean_cookies(raw_cookies)
        if cookies:
            headers.extend(("Cookie", v) for v in cookies)

    return headers


async def build_delegated_token(
    context: RequestContext, auth_config: AuthConfig, token_data: TokenData
) -> str | None:
    """Construct the delegated token for this request.

    Parameters
    ----------
    context
        Context of the incoming request.
    auth_config
        Configuration parameters for the authorization.
    token_data
        Data from the authentication token.

    Returns
    -------
    str or None
        Delegated token to include in the request, or `None` if none should be
        included.
    """
    if auth_config.notebook:
        token_service = context.factory.create_token_service()
        async with context.session.begin():
            token = await token_service.get_notebook_token(
                token_data,
                ip_address=context.ip_address,
                minimum_lifetime=auth_config.minimum_lifetime,
            )
        return str(token)
    elif auth_config.delegate_to:
        # Delegated scopes are optional; if the authenticating token doesn't
        # have the scope, it's omitted from the delegated token.  (To make it
        # mandatory, require that scope via the scope parameter as well, and
        # then the authenticating token will always have it.)  Therefore,
        # reduce the scopes of the internal token to the intersection between
        # the requested delegated scopes and the scopes of the authenticating
        # token.
        delegate_scopes = auth_config.delegate_scopes & set(token_data.scopes)
        token_service = context.factory.create_token_service()
        async with context.session.begin():
            token = await token_service.get_internal_token(
                token_data,
                service=auth_config.delegate_to,
                scopes=sorted(delegate_scopes),
                ip_address=context.ip_address,
                minimum_lifetime=auth_config.minimum_lifetime,
            )
        return str(token)
    else:
        return None
