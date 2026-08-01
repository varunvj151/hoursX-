"""Domain error taxonomy and its HTTP mapping."""

import pytest

from hoursx.errors import (
    AuthenticationError,
    ConflictError,
    HoursXError,
    NotFoundError,
    PermissionError_,
    ProviderUnavailableError,
    QuotaExceededError,
    RunConflictError,
    ValidationError,
)


@pytest.mark.parametrize(
    ("error_type", "expected_status", "expected_code"),
    [
        (NotFoundError, 404, "not_found"),
        (ConflictError, 409, "conflict"),
        (ValidationError, 422, "validation_failed"),
        (AuthenticationError, 401, "unauthenticated"),
        (PermissionError_, 403, "forbidden"),
        (QuotaExceededError, 429, "quota_exceeded"),
        (ProviderUnavailableError, 503, "provider_unavailable"),
    ],
)
def test_error_status_and_code_mapping(error_type, expected_status, expected_code):
    error = error_type("boom")
    assert error.status == expected_status
    assert error.code == expected_code


def test_every_domain_error_descends_from_base():
    for error_type in (NotFoundError, ConflictError, QuotaExceededError, RunConflictError):
        assert issubclass(error_type, HoursXError)


def test_run_conflict_is_a_conflict_with_its_own_code():
    error = RunConflictError("wrong state")
    assert error.status == 409
    assert error.code == "run_state_conflict"
    assert isinstance(error, ConflictError)


def test_payload_carries_code_and_message():
    payload = NotFoundError("no such run").as_payload()
    assert payload == {"code": "not_found", "detail": "no such run"}


def test_payload_includes_context_when_supplied():
    payload = QuotaExceededError("too many", limit="max_concurrent_runs", current=8).as_payload()
    assert payload["context"] == {"limit": "max_concurrent_runs", "current": 8}


def test_error_is_raisable_and_carries_message():
    with pytest.raises(HoursXError) as caught:
        raise ValidationError("bad input")
    assert caught.value.message == "bad input"
    assert str(caught.value) == "bad input"


def test_base_error_defaults_to_internal_500():
    error = HoursXError("unexpected")
    assert error.status == 500
    assert error.code == "internal_error"
