"""Tests for the Discard Checkout Session feature.

Covers:
- ``InternalClient.cancel_studio_checkout_session`` HTTP wire shape and
  the NotFound → no-op contract that the view depends on.
- ``views.discard_checkout`` happy path, no-op path, remote-failure path,
  and the Conflict-on-already-terminal path.
- ``subscribe.html`` template branching: when ``resumable_attempt`` is set,
  the Discard form is rendered and the plan-picker form is suppressed.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
from django.contrib.messages.storage.fallback import FallbackStorage
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.intelligence import views
from apps.intelligence.models import StudioCheckoutAttempt
from apps.intelligence.services.client import InternalClient
from apps.intelligence.services.exceptions import (
    Conflict,
    ServiceUnavailable,
)
from apps.members.models import OrgMembership
from apps.organizations.models import Organization

_BASE_URL = "https://intel.example.com/internal/v1"


def _attach_session_and_messages(request):
    """Set up just enough request state for ``django.contrib.messages`` calls
    inside the view to not blow up. We don't assert on the rendered messages
    UI, only on the queued message list."""
    # Minimal session shim — the FallbackStorage works without a full
    # session backend in tests.
    request.session = {}
    storage = FallbackStorage(request)
    request._messages = storage
    return storage


def _make_owner(org):
    user = User.objects.create_user(
        email=f"owner-{org.id}@example.com",
        password="pw",
        name="Owner",
        tos_accepted_at=timezone.now(),
    )
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    return user


def _make_member(org):
    user = User.objects.create_user(
        email=f"member-{org.id}@example.com",
        password="pw",
        name="Member",
        tos_accepted_at=timezone.now(),
    )
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.MEMBER)
    return user


# ---------------------------------------------------------------------------
# Client method tests
# ---------------------------------------------------------------------------


@override_settings(
    INTELLIGENCE_INTERNAL_URL=_BASE_URL,
    STUDIO_DEPLOYMENT_ID="prod",
    STUDIO_SHARED_SECRET="test-secret",
)
class CancelStudioCheckoutSessionClientTests(SimpleTestCase):
    def test_posts_to_correct_path_with_body_and_idempotency_key(self):
        with patch("apps.intelligence.services.client.httpx.Client") as mock_cls:
            mock = mock_cls.return_value.__enter__.return_value
            mock.request.return_value = httpx.Response(200, json={})

            InternalClient().cancel_studio_checkout_session(
                external_org_id="org-uuid",
                stripe_session_id="cs_test_123",
                idempotency_key="cancel-att-uuid",
            )

            call = mock.request.call_args
            assert call.args[0] == "POST"
            assert call.args[1] == f"{_BASE_URL}/studio-checkout-session/cancel"
            assert call.kwargs["headers"]["X-Idempotency-Key"] == "cancel-att-uuid"

    def test_swallows_404_as_noop(self):
        # If Intelligence has no matching open attempt, the discard must
        # still succeed locally — a stale Studio mirror cannot block UX.
        with patch("apps.intelligence.services.client.httpx.Client") as mock_cls:
            mock = mock_cls.return_value.__enter__.return_value
            mock.request.return_value = httpx.Response(404, json={"code": "not_found"})

            result = InternalClient().cancel_studio_checkout_session(
                external_org_id="org-uuid",
                stripe_session_id=None,
                idempotency_key="cancel-att-uuid",
            )
            assert result == {}

    def test_raises_servicunavailable_on_5xx(self):
        with patch("apps.intelligence.services.client.httpx.Client") as mock_cls:
            mock = mock_cls.return_value.__enter__.return_value
            mock.request.return_value = httpx.Response(503, json={})
            with self.assertRaises(ServiceUnavailable):
                InternalClient().cancel_studio_checkout_session(
                    external_org_id="org-uuid",
                    stripe_session_id="cs_x",
                    idempotency_key="cancel-att-uuid",
                )


# ---------------------------------------------------------------------------
# View tests
# ---------------------------------------------------------------------------


class DiscardCheckoutViewTests(TestCase):
    """Direct-call tests on the view function via RequestFactory.

    The decorator ``require_org_permission`` is applied at definition time
    and resolves OrgMembership from the URL kwarg ``org_id``; we call the
    decorated view directly, which exercises both the auth+permission
    gate and the body. ``redirect()`` is patched so we don't depend on
    the production URL conf being loaded (intelligence URLs are
    feature-gated and not registered in the test settings).
    """

    def setUp(self):
        self.rf = RequestFactory()
        self.org = Organization.objects.create(name="Acme")
        self.owner = _make_owner(self.org)

    def _make_post(self, user):
        request = self.rf.post(f"/orgs/{self.org.id}/intelligence/discard-checkout/")
        request.user = user
        storage = _attach_session_and_messages(request)
        return request, storage

    def _open_attempt(self, **overrides):
        defaults = {
            "organization": self.org,
            "plan_slug": "hobby",
            "stripe_session_id": "cs_test_123",
            "checkout_url": "https://stripe.example/cs_test_123",
            "status": StudioCheckoutAttempt.Status.OPEN,
        }
        defaults.update(overrides)
        return StudioCheckoutAttempt.objects.create(**defaults)

    def test_member_without_permission_gets_403(self):
        from django.core.exceptions import PermissionDenied

        member = _make_member(self.org)
        request, _ = self._make_post(member)
        with self.assertRaises(PermissionDenied):
            views.discard_checkout(request, org_id=self.org.id)

    def test_happy_path_cancels_remote_and_local(self):
        attempt = self._open_attempt()
        request, storage = self._make_post(self.owner)

        with (
            patch("apps.intelligence.views._client") as mock_client_factory,
            patch("apps.intelligence.views.redirect") as mock_redirect,
        ):
            mock_client_factory.return_value.cancel_studio_checkout_session.return_value = {}
            mock_redirect.return_value = "REDIRECT"

            result = views.discard_checkout(request, org_id=self.org.id)

            assert result == "REDIRECT"
            mock_redirect.assert_called_once_with("intelligence:subscribe", org_id=self.org.id)
            call = mock_client_factory.return_value.cancel_studio_checkout_session.call_args
            assert call.kwargs["external_org_id"] == str(self.org.id)
            assert call.kwargs["stripe_session_id"] == "cs_test_123"
            assert call.kwargs["idempotency_key"] == f"cancel-{attempt.id}"

        attempt.refresh_from_db()
        assert attempt.status == StudioCheckoutAttempt.Status.CANCELED
        assert attempt.consumed_at is not None

        # Partial-unique slot is now free — a fresh CREATING attempt is allowed.
        StudioCheckoutAttempt.objects.create(
            organization=self.org,
            plan_slug="standard",
            status=StudioCheckoutAttempt.Status.CREATING,
        )

        messages = [m.message for m in storage]
        assert any("Checkout discarded" in m for m in messages)

    def test_no_attempt_is_a_noop_with_info_message(self):
        request, storage = self._make_post(self.owner)
        with (
            patch("apps.intelligence.views._client") as mock_client_factory,
            patch("apps.intelligence.views.redirect") as mock_redirect,
        ):
            mock_redirect.return_value = "REDIRECT"

            result = views.discard_checkout(request, org_id=self.org.id)

            assert result == "REDIRECT"
            # No remote call when there's nothing to cancel.
            mock_client_factory.return_value.cancel_studio_checkout_session.assert_not_called()

        assert StudioCheckoutAttempt.objects.filter(organization=self.org).count() == 0
        messages = [m.message for m in storage]
        assert any("No checkout to discard" in m for m in messages)

    def test_remote_unavailable_leaves_local_row_unchanged(self):
        attempt = self._open_attempt()
        request, storage = self._make_post(self.owner)

        with (
            patch("apps.intelligence.views._client") as mock_client_factory,
            patch("apps.intelligence.views.redirect") as mock_redirect,
        ):
            mock_client_factory.return_value.cancel_studio_checkout_session.side_effect = ServiceUnavailable(
                "503", status_code=503, code="", body={}
            )
            mock_redirect.return_value = "REDIRECT"

            result = views.discard_checkout(request, org_id=self.org.id)
            assert result == "REDIRECT"

        attempt.refresh_from_db()
        assert attempt.status == StudioCheckoutAttempt.Status.OPEN
        assert attempt.consumed_at is None

        messages = [m.message for m in storage]
        assert any("couldn't reach" in m.lower() for m in messages)

    def test_conflict_still_cancels_local(self):
        # Remote says already-terminal (e.g. another tab already canceled).
        # The local mirror should still flip to canceled — no point leaving
        # a stale ``open`` row blocking the partial-unique slot.
        attempt = self._open_attempt()
        request, _ = self._make_post(self.owner)

        with (
            patch("apps.intelligence.views._client") as mock_client_factory,
            patch("apps.intelligence.views.redirect") as mock_redirect,
        ):
            mock_client_factory.return_value.cancel_studio_checkout_session.side_effect = Conflict(
                "already canceled",
                status_code=409,
                code="already_canceled",
                body={},
            )
            mock_redirect.return_value = "REDIRECT"
            views.discard_checkout(request, org_id=self.org.id)

        attempt.refresh_from_db()
        assert attempt.status == StudioCheckoutAttempt.Status.CANCELED


# ---------------------------------------------------------------------------
# Template branching tests
# ---------------------------------------------------------------------------


class SubscribeTemplateBranchingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme")

    def _render(self, **context_overrides):
        context = {
            "organization": self.org,
            "plans": [
                {"slug": "hobby", "name": "Hobby", "price_cents": 1900, "monthly_credits": 6000},
                {"slug": "standard", "name": "Standard", "price_cents": 9900, "monthly_credits": 33000},
            ],
            "resumable_attempt": None,
            "in_flight_attempt": None,
            "billing_email": "owner@example.com",
        }
        context.update(context_overrides)
        return render_to_string("intelligence/subscribe.html", context)

    def test_no_attempt_renders_plan_picker_form(self):
        html = self._render()
        assert "Choose a plan" in html
        assert "Discard Checkout Session" not in html
        assert 'name="plan"' in html

    def test_resumable_renders_discard_form_and_hides_picker(self):
        attempt = StudioCheckoutAttempt(
            organization=self.org,
            plan_slug="hobby",
            stripe_session_id="cs_test_123",
            checkout_url="https://stripe.example/cs_test_123",
            status=StudioCheckoutAttempt.Status.OPEN,
        )
        html = self._render(resumable_attempt=attempt)
        assert "Resume your checkout" in html
        assert "Discard Checkout Session" in html
        # Plan picker is suppressed — no radios, no "Choose a plan".
        assert "Choose a plan" not in html
        assert 'name="plan"' not in html

    def test_in_flight_hides_picker_but_no_discard_form(self):
        attempt = StudioCheckoutAttempt(
            organization=self.org,
            plan_slug="hobby",
            status=StudioCheckoutAttempt.Status.CREATING,
        )
        html = self._render(in_flight_attempt=attempt)
        assert "Setting up your checkout" in html
        # The discard CTA is only on the resume card, not the spinner card.
        assert "Discard Checkout Session" not in html
        assert "Choose a plan" not in html
