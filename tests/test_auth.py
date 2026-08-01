"""Credential hashing, JWT round-trips, and RBAC grants."""

import pytest

from hoursx.auth import (
    Permission,
    Role,
    TokenError,
    hash_api_key,
    hash_password,
    issue_token,
    new_api_key,
    role_allows,
    verify_password,
    verify_token,
)


def test_password_roundtrip():
    stored = hash_password("correct horse battery")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong", stored)


def test_password_hashes_are_salted():
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_malformed_hash():
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "")


def test_jwt_roundtrip():
    token = issue_token(user_id="u123", secret="s", ttl_seconds=60)
    assert verify_token(token, secret="s") == "u123"


def test_jwt_rejects_wrong_secret_and_garbage():
    token = issue_token(user_id="u123", secret="s", ttl_seconds=60)
    with pytest.raises(TokenError):
        verify_token(token, secret="other")
    with pytest.raises(TokenError):
        verify_token("garbage", secret="s")


def test_jwt_rejects_expired():
    token = issue_token(user_id="u123", secret="s", ttl_seconds=-10)
    with pytest.raises(TokenError):
        verify_token(token, secret="s")


def test_api_key_hash_is_deterministic():
    key = new_api_key()
    assert key.startswith("hx_")
    assert hash_api_key(key) == hash_api_key(key)


@pytest.mark.parametrize(
    ("role", "permission", "allowed"),
    [
        (Role.VIEWER, Permission.OBSERVE, True),
        (Role.VIEWER, Permission.SESSIONS_USE, False),
        (Role.MEMBER, Permission.SESSIONS_USE, True),
        (Role.MEMBER, Permission.AGENTS_MANAGE, False),
        (Role.ADMIN, Permission.AGENTS_MANAGE, True),
        (Role.ADMIN, Permission.WORKSPACE_MANAGE, False),
        (Role.OWNER, Permission.WORKSPACE_MANAGE, True),
    ],
)
def test_rbac_matrix(role, permission, allowed):
    assert role_allows(role, permission) is allowed


def test_rbac_unknown_role_grants_nothing():
    assert not role_allows("superuser", Permission.OBSERVE)
