"""Customer Gmail auth retry must not mutate business state or supplier integration."""
import pytest
from app.workers.email_runtime import should_rebuild_readonly_session
from app.integrations.email.health import classify, retry_delay
from google.auth.exceptions import RefreshError


@pytest.mark.parametrize("category", ["reauth_required", "bad_credentials"])
def test_customer_credentials_failure_rebuilds_transport(category):
    assert should_rebuild_readonly_session("customer", category)


@pytest.mark.parametrize("category", ["connected", "network_unavailable", "database_unavailable",
                                      "worker_error", "not_configured"])
def test_non_auth_failure_preserves_worker_session(category):
    assert not should_rebuild_readonly_session("customer", category)


@pytest.mark.parametrize("category", ["reauth_required", "bad_credentials", "network_unavailable",
                                      "database_unavailable"])
def test_supplier_mailbox_is_not_modified_by_customer_recovery(category):
    assert not should_rebuild_readonly_session("supplier", category)


def test_invalid_grant_reauth_remains_manual_and_throttled():
    assert classify(RefreshError("invalid_grant")) == "reauth_required"
    assert retry_delay("reauth_required", 1, 60) >= 900
