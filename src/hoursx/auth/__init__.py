"""Authentication and authorization: credentials, JWTs, and RBAC."""

from hoursx.auth.credentials import hash_api_key, hash_password, new_api_key, verify_password
from hoursx.auth.rbac import Permission, Role, role_allows
from hoursx.auth.tokens import TokenError, issue_token, verify_token

__all__ = [
    "Permission",
    "Role",
    "TokenError",
    "hash_api_key",
    "hash_password",
    "issue_token",
    "new_api_key",
    "role_allows",
    "verify_password",
    "verify_token",
]
