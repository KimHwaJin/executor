"""One API key that test apps accept, so HTTP tests read like real calls."""

import hashlib

from executor_service.settings import AuthClient

TEST_KEY = "test-key"
# Admin and relay for any tenant: tests reach every route and tenant.
TEST_CLIENT = AuthClient(
    name="test",
    key_sha256=(hashlib.sha256(TEST_KEY.encode()).hexdigest(),),
    tenant="dev",
    relay=True,
    allowed_tenants=("*",),
    admin=True,
)
TEST_AUTH_CLIENTS = (TEST_CLIENT,)
TEST_HEADERS = {"X-Api-Key": TEST_KEY}
