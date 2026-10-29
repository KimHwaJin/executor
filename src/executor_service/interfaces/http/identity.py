"""OpenAPI declarations for the headers the authentication middleware reads.

The middleware in ``interfaces.auth`` authenticates and validates these
headers before routing, so these dependencies only document them.
"""

from typing import Annotated

from fastapi import Header, Security
from fastapi.security import APIKeyHeader

API_KEY = APIKeyHeader(
    name="X-Api-Key",
    auto_error=False,
    description="Caller API key. Required when AUTH_MODE=api_key (stg, prd).",
)


async def documented_identity(
    _api_key: Annotated[str | None, Security(API_KEY)] = None,
    _tenant_id: Annotated[
        str | None,
        Header(
            alias="X-Tenant-Id",
            description=(
                "Relay keys only, within their allowed tenants. Defaults to "
                "the key's tenant, or 'dev' in header mode."
            ),
        ),
    ] = None,
    _user_id: Annotated[
        str | None,
        Header(
            alias="X-User-Id",
            description=(
                "Owning user, ^[a-z0-9][a-z0-9._-]{0,63}$ after lowercasing. "
                "Required for writes; reads without it see the whole tenant."
            ),
        ),
    ] = None,
    _project_id: Annotated[
        str | None,
        Header(
            alias="X-Project-Id",
            description="^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$, never 'unscoped'.",
        ),
    ] = None,
    _session_id: Annotated[
        str | None,
        Header(
            alias="X-Session-Id",
            description="Same format as X-Project-Id, which it requires.",
        ),
    ] = None,
) -> None:
    pass


async def documented_admin_key(
    _api_key: Annotated[str | None, Security(API_KEY)] = None,
) -> None:
    pass
