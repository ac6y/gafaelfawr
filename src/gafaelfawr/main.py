"""Application definition for Gafaelfawr."""

from __future__ import annotations

from fastapi import FastAPI

from gafaelfawr.dependencies.config import config_dependency
from gafaelfawr.dependencies.redis import redis_dependency
from gafaelfawr.handlers import (
    analyze,
    auth,
    index,
    influxdb,
    login,
    logout,
    oidc,
    tokens,
    userinfo,
    well_known,
)
from gafaelfawr.middleware.state import StateMiddleware
from gafaelfawr.middleware.x_forwarded import XForwardedMiddleware
from gafaelfawr.models.state import State

app = FastAPI()
app.include_router(analyze.router)
app.include_router(auth.router)
app.include_router(index.router)
app.include_router(influxdb.router)
app.include_router(login.router)
app.include_router(logout.router)
app.include_router(oidc.router)
app.include_router(tokens.router)
app.include_router(userinfo.router)
app.include_router(well_known.router)


@app.on_event("startup")
async def startup_event() -> None:
    config = config_dependency()
    app.add_middleware(XForwardedMiddleware, proxies=config.proxies)
    app.add_middleware(
        StateMiddleware, cookie_name="gafaelfawr", state_class=State
    )


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await redis_dependency.close()