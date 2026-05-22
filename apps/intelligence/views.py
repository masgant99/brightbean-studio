"""Studio views for the Intelligence integration.

URL surface:

- ``/orgs/<org_id>/intelligence/``                     : playground
- ``/orgs/<org_id>/intelligence/subscribe/``           : plan picker
- ``/orgs/<org_id>/intelligence/checkout/?plan=<slug>``: TX1+TX2 checkout
- ``/orgs/<org_id>/intelligence/recover/``             : closed-tab recovery
- ``/orgs/<org_id>/intelligence/portal/``              : Stripe portal redirect
- ``/orgs/<org_id>/intelligence/billing-settings/``    : edit billing contact
- ``/orgs/<org_id>/intelligence/billing-contact/``     : POST update
- ``/orgs/<org_id>/intelligence/status/``              : HTMX polling fragment
- ``/orgs/<org_id>/intelligence/score-packaging/``     : tool POST (HTMX)
- ``/orgs/<org_id>/intelligence/score-video-hook/``    : tool POST (HTMX)
- ``/orgs/<org_id>/intelligence/benchmark-channel/``   : tool POST (HTMX)
- ``/orgs/<org_id>/intelligence/benchmark-video/``     : tool POST (HTMX)
- ``/orgs/<org_id>/intelligence/research-content-gaps/``: tool POST (HTMX)
- ``/orgs/<org_id>/intelligence/list-niches/``         : tool POST (HTMX)
- ``/intelligence/activate/?session_id=cs_…``          : Stripe success URL
- ``/intelligence/finalizing/``                        : fallback polling page
- ``/intelligence/finalizing/status/``                 : finalizing polling fragment
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.members.decorators import require_org_permission
from apps.members.models import OrgMembership, has_org_permission
from apps.organizations.models import Organization

from .decorators import intelligence_subscription_required
from .models import (
    IntelligenceSubscription,
    IntelligenceUsageEvent,
    PendingActivation,
    StudioCheckoutAttempt,
)
from .services.cache import per_request_cache
from .services.client import IntelligenceAPIClient, InternalClient
from .services.exceptions import (
    ActivationRejected,
    Conflict,
    DeploymentNotAuthorized,
    InsufficientCredits,
    IntelligenceClientError,
    NotFound,
    RateLimited,
    ServiceUnavailable,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _client() -> InternalClient:
    return InternalClient()


def _api_client_for(sub: IntelligenceSubscription) -> IntelligenceAPIClient | None:
    if not sub or not sub.intelligence_api_key:
        return None
    return IntelligenceAPIClient(api_key=sub.intelligence_api_key)


def _me_for(request, org_id, sub: IntelligenceSubscription | None) -> dict | None:
    if not sub or sub.status != "active":
        return None
    api = _api_client_for(sub)
    if api is None:
        return None
    try:
        return per_request_cache(request, (str(org_id), "me"), api.me)
    except IntelligenceClientError:
        logger.exception("Intelligence /v1/me failed; suppressing")
        return None


def _record_usage(*, organization, user, endpoint, status_code,
                  credits_charged=0, latency_ms=None):
    IntelligenceUsageEvent.objects.create(
        organization=organization,
        user=user if user.is_authenticated else None,
        endpoint=endpoint,
        status_code=status_code,
        credits_charged=credits_charged,
        latency_ms=latency_ms,
    )


def _render_tool_error(request, exc: IntelligenceClientError, *, organization):
    """Map a typed client error to the right HTMX result partial."""
    context = {"organization": organization, "code": exc.code, "message": str(exc)}
    status = exc.status_code or 500
    if isinstance(exc, InsufficientCredits):
        template = "intelligence/_tool_error_no_credits.html"
    elif isinstance(exc, RateLimited):
        context["retry_after"] = exc.retry_after
        template = "intelligence/_tool_error_rate_limited.html"
    elif isinstance(exc, ServiceUnavailable):
        template = "intelligence/_tool_error_unavailable.html"
    else:
        template = "intelligence/_tool_error.html"
    return render(request, template, context, status=status)


# ---------------------------------------------------------------------------
# Playground (overview)
# ---------------------------------------------------------------------------


@require_org_permission("use_intelligence")
def playground(request, org_id):
    """Single Intelligence UI surface. Always renders the playground
    layout; overlays / disabled state reflect ``IntelligenceSubscription``."""
    sub = getattr(request.org, "intelligence_subscription", None)
    me = _me_for(request, org_id, sub)

    can_manage_billing = has_org_permission(
        request.org_membership, "manage_intelligence_billing",
    )

    # Closed-tab recovery: only check Intelligence if we have no local sub
    # AND the user can manage billing (only they'd see the banner).
    pending = None
    if sub is None and can_manage_billing:
        try:
            pending = _client().pending_activation(external_org_id=str(org_id))
        except (ServiceUnavailable, DeploymentNotAuthorized,
                IntelligenceClientError):
            logger.exception("/pending-activation lookup failed; suppressing")
            pending = None

    context = {
        "organization": request.org,
        "subscription": sub,
        "pending_activation": pending,
        "me": me,
        "can_manage_billing": can_manage_billing,
    }
    return render(request, "intelligence/playground.html", context)


# ---------------------------------------------------------------------------
# Subscribe / checkout
# ---------------------------------------------------------------------------


@require_org_permission("manage_intelligence_billing")
def subscribe(request, org_id):
    """Plan picker + billing-contact form. Performs a local eligibility
    survey first so a user with an existing pending/active sub is routed
    to the appropriate resume/manage UI instead of being allowed to pay."""
    sub = getattr(request.org, "intelligence_subscription", None)
    if sub is not None and sub.status == IntelligenceSubscription.Status.ACTIVE:
        return redirect("intelligence:playground", org_id=org_id)
    if sub is not None and sub.status == IntelligenceSubscription.Status.FINALIZING:
        return redirect("intelligence:playground", org_id=org_id)

    # Look for an in-progress local attempt; if found, render "Resume" UI.
    resumable = StudioCheckoutAttempt.objects.filter(
        organization=request.org,
        status=StudioCheckoutAttempt.Status.OPEN,
    ).order_by("-created_at").first()

    in_flight = StudioCheckoutAttempt.objects.filter(
        organization=request.org,
        status=StudioCheckoutAttempt.Status.CREATING,
    ).order_by("-created_at").first()

    try:
        plans_resp = _client().list_plans()
    except DeploymentNotAuthorized:
        return render(
            request, "intelligence/deployment_not_authorized.html",
            {"organization": request.org}, status=403,
        )
    except IntelligenceClientError:
        logger.exception("/plans fetch failed")
        plans_resp = {"plans": []}

    context = {
        "organization": request.org,
        "plans": plans_resp.get("plans", []),
        "resumable_attempt": resumable,
        "in_flight_attempt": in_flight,
        "billing_email": request.org.billing_email or request.user.email,
    }
    return render(request, "intelligence/subscribe.html", context)


@require_POST
@require_org_permission("manage_intelligence_billing")
def checkout(request, org_id):
    """Two-transaction checkout-session creation.

    TX1: reserve local StudioCheckoutAttempt (creating) under the
    partial-unique constraint. Concurrent admins racing both get this
    far — the second hits IntegrityError and gets routed to "Resume".

    Outside TX: call Intelligence /studio-checkout-session.

    TX2: update local attempt with stripe_session_id + checkout_url +
    status=open. 302 to Stripe.
    """
    plan_slug = (request.POST.get("plan") or "").strip()
    if not plan_slug:
        return HttpResponseBadRequest("plan required")

    billing_email = (request.POST.get("billing_email") or "").strip()
    if billing_email and request.org.billing_email != billing_email:
        request.org.billing_email = billing_email
        request.org.save(update_fields=["billing_email", "updated_at"])
    billing_email = request.org.billing_email or request.user.email

    org_name = request.org.name
    user = request.user
    idempotency_key = f"checkout-{request.org.id}-{plan_slug}"

    # ---- TX1: reserve local attempt ------------------------------------
    try:
        with transaction.atomic():
            attempt = StudioCheckoutAttempt.objects.create(
                organization=request.org,
                user=user,
                plan_slug=plan_slug,
                billing_email=billing_email,
                idempotency_key=idempotency_key,
                status=StudioCheckoutAttempt.Status.CREATING,
            )
    except IntegrityError:
        # Another admin (or a previous attempt that hasn't terminated)
        # already holds the slot. Show the appropriate resume / polling UI.
        existing = StudioCheckoutAttempt.objects.filter(
            organization=request.org,
            status__in=[
                StudioCheckoutAttempt.Status.OPEN,
                StudioCheckoutAttempt.Status.CREATING,
                StudioCheckoutAttempt.Status.PENDING,
            ],
        ).order_by("-created_at").first()
        return redirect("intelligence:subscribe", org_id=org_id)

    # ---- Outside any transaction: Intelligence call --------------------
    try:
        resp = _client().studio_checkout_session(
            external_org_id=str(request.org.id),
            org_name=org_name,
            billing_email=billing_email,
            plan_slug=plan_slug,
            contact_email=user.email,
            contact_full_name=user.get_full_name() or "",
            return_base_url=_studio_base_url(),
            idempotency_key=idempotency_key,
        )
    except Conflict as exc:
        attempt.status = StudioCheckoutAttempt.Status.EXPIRED
        attempt.last_error_code = exc.code or "conflict"
        attempt.save(update_fields=["status", "updated_at"])
        # If it's "open_checkout" we have an existing URL; show resume.
        messages.warning(
            request, "Another checkout is already in progress for this org.",
        )
        return redirect("intelligence:subscribe", org_id=org_id)
    except (ServiceUnavailable, DeploymentNotAuthorized, IntelligenceClientError) as exc:
        attempt.delete()
        logger.exception("studio_checkout_session failed: %s", exc)
        messages.error(
            request, "We couldn't reach the billing service. Try again in a moment.",
        )
        return redirect("intelligence:subscribe", org_id=org_id)

    # ---- TX2: promote attempt to open + redirect -----------------------
    with transaction.atomic():
        attempt.stripe_session_id = resp["stripe_session_id"]
        attempt.checkout_url = resp["checkout_url"]
        attempt.status = StudioCheckoutAttempt.Status.OPEN
        if resp.get("expires_at"):
            try:
                attempt.expires_at = timezone.datetime.fromisoformat(
                    resp["expires_at"].replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass
        attempt.save(update_fields=[
            "stripe_session_id", "checkout_url", "status",
            "expires_at", "updated_at",
        ])

    return redirect(resp["checkout_url"])


# ---------------------------------------------------------------------------
# Activate — Stripe success URL handler
# ---------------------------------------------------------------------------


@login_required
@require_GET
def activate(request):
    """Two-phase activation handler.

    1. Look up the local StudioCheckoutAttempt by session_id; if absent,
       this user did not initiate this session — reject.
    2. Re-check current OrgMembership of OWNER/ADMIN on the attempt's
       organization. Initiator identity is NOT trusted — only current
       role. (Defeats T18 demoted-admin bypass.)
    3. Call Intelligence /activate-preflight → returns validation_token.
    4. Re-check OrgMembership against ``resolved_external_org_id`` from
       the Intelligence response.
    5. Call Intelligence /activate-commit → returns api_key on first
       successful call, ``api_key_minted=False`` on replay.
    6. Store api_key (plaintext into EncryptedTextField). If
       ``api_key_minted=False`` and we have no local key, call rotate-key
       to recover.

    On transient failure between Phase 1 and Phase 2 (or between Phase 2
    and the local commit), persist PendingActivation + enqueue worker
    fallback + redirect to the finalizing page.
    """
    session_id = (request.GET.get("session_id") or "").strip()
    if not session_id:
        return HttpResponseBadRequest("session_id required")

    attempt = StudioCheckoutAttempt.objects.filter(
        stripe_session_id=session_id,
    ).first()
    if attempt is None:
        return render(request, "intelligence/activation_failed.html", {
            "code": "unknown_session",
            "message": "We don't recognize this Stripe session.",
        }, status=400)

    org = attempt.organization
    membership = OrgMembership.objects.filter(
        user=request.user, organization=org,
        org_role__in=[OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN],
    ).first()
    if membership is None:
        return render(request, "intelligence/activation_org_mismatch.html", {
            "organization": org,
        }, status=403)

    try:
        return _activate_two_phase(
            request, session_id=session_id, attempt=attempt,
            expected_org=org, user=request.user,
        )
    except _DeferredToWorker:
        # Fallback path — PendingActivation already persisted in
        # ``_activate_two_phase``. Send the user to the finalizing page.
        return redirect("intelligence:finalizing")


class _DeferredToWorker(Exception):
    """Internal signal that activation has been queued for the worker."""


def _activate_two_phase(request, *, session_id, attempt, expected_org, user):
    """Two-phase + lost-key rotation. May raise ``_DeferredToWorker`` to
    signal a worker fallback."""
    preflight_key = f"preflight-{session_id}"
    try:
        preflight = _client().activate_preflight(
            session_id=session_id,
            expected_external_org_id=str(expected_org.id),
            plan_slug=attempt.plan_slug,
            billing_email=expected_org.billing_email or "",
            org_name=expected_org.name,
            contact_email=user.email,
            contact_full_name=user.get_full_name() or "",
            idempotency_key=preflight_key,
        )
    except ActivationRejected as exc:
        logger.warning("Activation preflight rejected: code=%s", exc.code)
        return render(request, "intelligence/activation_failed.html", {
            "code": exc.code, "message": exc.user_message,
        }, status=exc.status_code or 400)
    except (ServiceUnavailable, IntelligenceClientError):
        logger.exception("Preflight transient failure; deferring to worker")
        _queue_pending_activation(user, session_id)
        raise _DeferredToWorker

    resolved_org_id = preflight.get("resolved_external_org_id")
    if str(expected_org.id) != str(resolved_org_id):
        # Intelligence resolved a different org than ours. Refuse to commit.
        return render(request, "intelligence/activation_org_mismatch.html", {
            "organization": expected_org,
        }, status=403)

    # Re-check membership against the resolved org (belt-and-braces; should
    # be identical to expected_org but the plan calls for the explicit
    # second check).
    membership = OrgMembership.objects.filter(
        user=user, organization_id=resolved_org_id,
        org_role__in=[OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN],
    ).first()
    if membership is None:
        return render(request, "intelligence/activation_org_mismatch.html", {
            "organization": expected_org,
        }, status=403)

    commit_key = f"commit-{session_id}"
    try:
        commit_resp = _client().activate_commit(
            validation_token=preflight["validation_token"],
            contact_email=user.email,
            contact_full_name=user.get_full_name() or "",
            billing_email=expected_org.billing_email or "",
            org_name=expected_org.name,
            idempotency_key=commit_key,
        )
    except ActivationRejected as exc:
        return render(request, "intelligence/activation_failed.html", {
            "code": exc.code, "message": exc.user_message,
        }, status=exc.status_code or 400)
    except (ServiceUnavailable, IntelligenceClientError):
        logger.exception("Commit transient failure; deferring to worker")
        _queue_pending_activation(user, session_id)
        raise _DeferredToWorker

    # Final local commit.
    return _finalize_local_subscription(
        request, attempt=attempt, expected_org=expected_org,
        commit_resp=commit_resp,
    )


def _queue_pending_activation(user, session_id: str):
    """Persist PendingActivation + enqueue worker."""
    PendingActivation.objects.update_or_create(
        user=user, session_id=session_id,
        defaults={"status": PendingActivation.Status.PENDING},
    )
    # Worker call lives in milestone 17 (tasks.py). Stub: the row is
    # persisted; the recurring worker (once registered) will pick it up.


def _finalize_local_subscription(request, *, attempt, expected_org,
                                  commit_resp):
    """Write the IntelligenceSubscription row + redirect.

    Handles the api_key_minted=False replay case by calling /rotate-key
    if the local row has no key yet (would happen if Studio crashed
    between a previous commit's HTTP response and the local save).
    """
    api_key = commit_resp.get("api_key")
    api_key_minted = commit_resp.get("api_key_minted", False)

    with transaction.atomic():
        sub, _ = IntelligenceSubscription.objects.select_for_update().get_or_create(
            organization=expected_org,
            defaults={"status": IntelligenceSubscription.Status.PROVISIONING},
        )
        if api_key_minted and api_key:
            sub.intelligence_api_key = api_key
            sub.intelligence_api_key_prefix = api_key[:8]
        elif not sub.intelligence_api_key:
            # Lost-key recovery: server cached the response but we don't
            # have the plaintext. Rotate.
            try:
                rot = _client().rotate_key(
                    user_id=commit_resp["user_id"],
                    external_org_id=str(expected_org.id),
                    idempotency_key=f"rotate-{expected_org.id}-{int(time.time())}",
                )
                sub.intelligence_api_key = rot["api_key"]
                sub.intelligence_api_key_prefix = rot["api_key"][:8]
            except IntelligenceClientError:
                logger.exception("rotate-key fallback failed")

        sub.intelligence_account_id = str(commit_resp.get("user_id") or "")
        sub.plan_slug = commit_resp.get("plan_slug", "")
        if commit_resp.get("period_end"):
            try:
                sub.current_period_end = timezone.datetime.fromisoformat(
                    commit_resp["period_end"].replace("Z", "+00:00"),
                )
            except (ValueError, AttributeError):
                pass
        sub.status = IntelligenceSubscription.Status.ACTIVE
        sub.last_synced_at = timezone.now()
        sub.save()

        attempt.status = StudioCheckoutAttempt.Status.ACTIVATED
        attempt.consumed_at = timezone.now()
        attempt.save(update_fields=["status", "consumed_at", "updated_at"])

    return redirect("intelligence:playground", org_id=expected_org.id)


# ---------------------------------------------------------------------------
# Recover (closed-tab)
# ---------------------------------------------------------------------------


@require_POST
@require_org_permission("manage_intelligence_billing")
def recover(request, org_id):
    """Closed-tab recovery flow.

    Studio's playground discovers a webhook-persisted Pending row via
    ``/internal/v1/pending-activation``; this view runs the two-phase
    activation against that row's ``session_id``."""
    pending = _client().pending_activation(external_org_id=str(org_id))
    if pending is None:
        messages.warning(request, "No pending activation found.")
        return redirect("intelligence:playground", org_id=org_id)

    session_id = pending["stripe_session_id"]
    # Fabricate a local attempt row so the two-phase logic finds it.
    attempt, _ = StudioCheckoutAttempt.objects.update_or_create(
        organization=request.org,
        defaults={
            "user": request.user,
            "plan_slug": "",
            "billing_email": request.org.billing_email or request.user.email,
            "status": StudioCheckoutAttempt.Status.OPEN,
            "stripe_session_id": session_id,
            "checkout_url": "",
            "idempotency_key": f"recover-{request.org.id}",
        },
    )
    try:
        return _activate_two_phase(
            request, session_id=session_id, attempt=attempt,
            expected_org=request.org, user=request.user,
        )
    except _DeferredToWorker:
        return redirect("intelligence:finalizing")


# ---------------------------------------------------------------------------
# Polling endpoints (status / finalizing)
# ---------------------------------------------------------------------------


@require_GET
@require_org_permission("use_intelligence")
def status_fragment(request, org_id):
    """HTMX polling fragment for the playground overlay. Returns 204 when
    nothing has changed (HTMX leaves the existing partial in place);
    returns an OOB swap fragment when the state transitions to active."""
    sub = getattr(request.org, "intelligence_subscription", None)
    if sub is None:
        return HttpResponse(status=204)
    if sub.status == IntelligenceSubscription.Status.ACTIVE:
        return render(
            request, "intelligence/_status_active_oob.html",
            {"organization": request.org, "subscription": sub},
        )
    return render(
        request, "intelligence/_status_polling.html",
        {"organization": request.org, "subscription": sub},
    )


@login_required
@require_GET
def finalizing(request):
    """User-scoped finalizing page shown when sync activation transient-failed
    before we could resolve an org. Polls the user-scoped fragment below."""
    pending = (
        PendingActivation.objects.filter(user=request.user)
        .order_by("-created_at")
        .first()
    )
    return render(
        request, "intelligence/finalizing.html", {"pending": pending},
    )


@login_required
@require_GET
def finalizing_status(request):
    """HTMX polling fragment for the user-scoped finalizing page."""
    pending = (
        PendingActivation.objects.filter(user=request.user)
        .order_by("-created_at")
        .first()
    )
    if pending is None:
        return HttpResponse(status=204)
    if pending.status == PendingActivation.Status.COMPLETED:
        if pending.resolved_organization_id:
            return render(
                request,
                "intelligence/_finalizing_completed.html",
                {"org_id": pending.resolved_organization_id},
            )
    if pending.status == PendingActivation.Status.REJECTED_UNAUTHORIZED:
        return render(
            request, "intelligence/_finalizing_unauthorized.html", {},
        )
    if pending.status == PendingActivation.Status.PROVISIONING_FAILED:
        return render(
            request, "intelligence/_finalizing_failed.html",
            {"last_error": pending.last_error},
        )
    return render(
        request, "intelligence/_finalizing_polling.html",
        {"pending": pending},
    )


# ---------------------------------------------------------------------------
# Billing management
# ---------------------------------------------------------------------------


@require_POST
@require_org_permission("manage_intelligence_billing")
def portal(request, org_id):
    """Mint a Stripe Customer Portal URL via Intelligence + 302."""
    try:
        resp = _client().portal_session(external_org_id=str(org_id))
    except (ServiceUnavailable, IntelligenceClientError) as exc:
        logger.exception("portal_session failed: %s", exc)
        messages.error(
            request, "We couldn't open the billing portal. Try again in a moment.",
        )
        return redirect("intelligence:playground", org_id=org_id)
    return redirect(resp["url"])


@require_GET
@require_org_permission("manage_intelligence_billing")
def billing_settings(request, org_id):
    sub = getattr(request.org, "intelligence_subscription", None)
    return render(request, "intelligence/billing_settings.html", {
        "organization": request.org,
        "subscription": sub,
        "billing_email": request.org.billing_email or request.user.email,
    })


@require_POST
@require_org_permission("manage_intelligence_billing")
def update_billing_contact(request, org_id):
    billing_email = (request.POST.get("billing_email") or "").strip()
    if not billing_email:
        return HttpResponseBadRequest("billing_email required")

    with transaction.atomic():
        request.org.billing_email = billing_email
        request.org.save(update_fields=["billing_email", "updated_at"])

    sub = getattr(request.org, "intelligence_subscription", None)
    if sub is not None and sub.status == IntelligenceSubscription.Status.ACTIVE:
        try:
            _client().update_billing_contact(
                external_org_id=str(org_id),
                billing_email=billing_email,
                org_name=request.org.name,
            )
        except IntelligenceClientError:
            logger.exception("update_billing_contact sync failed")
            messages.warning(
                request,
                "Saved locally, but the billing service didn't acknowledge. "
                "It'll retry automatically.",
            )

    if request.headers.get("HX-Request"):
        return render(
            request, "intelligence/_billing_contact_saved.html",
            {"billing_email": billing_email},
        )
    messages.success(request, "Billing contact updated.")
    return redirect("intelligence:billing-settings", org_id=org_id)


# ---------------------------------------------------------------------------
# Tool endpoints — six HTMX POSTs
# ---------------------------------------------------------------------------


def _call_tool(request, method_name: str, *, body: dict,
               template: str, endpoint_path: str):
    """Shared tool-call shim: dispatch to the per-org IntelligenceAPIClient
    method, render the result partial, and log a usage event."""
    sub = request.org.intelligence_subscription
    api = _api_client_for(sub)
    start = time.monotonic()
    try:
        result = getattr(api, method_name)(**body)
    except IntelligenceClientError as exc:
        latency = int((time.monotonic() - start) * 1000)
        _record_usage(
            organization=request.org, user=request.user,
            endpoint=endpoint_path, status_code=exc.status_code or 500,
            latency_ms=latency,
        )
        return _render_tool_error(request, exc, organization=request.org)

    latency = int((time.monotonic() - start) * 1000)
    _record_usage(
        organization=request.org, user=request.user,
        endpoint=endpoint_path, status_code=200, latency_ms=latency,
        credits_charged=_credits_for(endpoint_path),
    )
    return render(request, template, {
        "result": result, "organization": request.org,
    })


def _credits_for(endpoint_path: str) -> int:
    """Mirror of Intelligence's per-endpoint credit cost (display-only —
    Intelligence is authoritative)."""
    return {
        "/v1/score/packaging": 1,
        "/v1/score/video-hook": 10,
        "/v1/benchmark/channel": 5,
        "/v1/benchmark/video": 3,
        "/v1/research/content-gaps": 5,
        "/v1/research/niches": 1,
    }.get(endpoint_path, 0)


@require_POST
@require_org_permission("use_intelligence")
@intelligence_subscription_required
def score_packaging(request, org_id):
    body = {
        "title": (request.POST.get("title") or "").strip() or None,
        "thumbnail_url": (request.POST.get("thumbnail_url") or "").strip() or None,
        "thumbnail_base64": (request.POST.get("thumbnail_base64") or "").strip() or None,
        "channel_url": (request.POST.get("channel_url") or "").strip() or None,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return _call_tool(
        request, "score_packaging", body=body,
        template="intelligence/_score_packaging_result.html",
        endpoint_path="/v1/score/packaging",
    )


@require_POST
@require_org_permission("use_intelligence")
@intelligence_subscription_required
def score_video_hook(request, org_id):
    body = {"youtube_url": request.POST.get("youtube_url", "").strip()}
    return _call_tool(
        request, "score_video_hook", body=body,
        template="intelligence/_score_video_hook_result.html",
        endpoint_path="/v1/score/video-hook",
    )


@require_POST
@require_org_permission("use_intelligence")
@intelligence_subscription_required
def benchmark_channel(request, org_id):
    body = {"url": request.POST.get("url", "").strip()}
    return _call_tool(
        request, "benchmark_channel", body=body,
        template="intelligence/_benchmark_channel_result.html",
        endpoint_path="/v1/benchmark/channel",
    )


@require_POST
@require_org_permission("use_intelligence")
@intelligence_subscription_required
def benchmark_video(request, org_id):
    body = {"url": request.POST.get("url", "").strip()}
    return _call_tool(
        request, "benchmark_video", body=body,
        template="intelligence/_benchmark_video_result.html",
        endpoint_path="/v1/benchmark/video",
    )


@require_POST
@require_org_permission("use_intelligence")
@intelligence_subscription_required
def research_content_gaps(request, org_id):
    niche = (request.POST.get("niche") or "").strip()
    if not niche:
        return HttpResponseBadRequest("niche required")
    body = {
        "niche": niche,
        "limit": int(request.POST.get("limit") or 20),
        "min_score": int(request.POST.get("min_score") or 0),
    }
    gap_type = request.POST.getlist("gap_type") if hasattr(request.POST, "getlist") else None
    if gap_type:
        body["gap_type"] = gap_type
    return _call_tool(
        request, "research_content_gaps", body=body,
        template="intelligence/_research_content_gaps_result.html",
        endpoint_path="/v1/research/content-gaps",
    )


@require_POST
@require_org_permission("use_intelligence")
@intelligence_subscription_required
def list_niches(request, org_id):
    return _call_tool(
        request, "list_niches", body={},
        template="intelligence/_list_niches_result.html",
        endpoint_path="/v1/research/niches",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _studio_base_url() -> str:
    """The publicly-reachable base URL for THIS Studio deployment.

    Intelligence validates ``return_base_url`` against the registered
    ``StudioDeployment.base_url`` and refuses if they differ — open-
    redirect defense — so we send whatever is configured in env.
    """
    from django.conf import settings
    return getattr(settings, "STUDIO_BASE_URL", "").rstrip("/")
