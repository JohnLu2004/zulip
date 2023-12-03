import logging
from typing import Any, Dict, Literal, Optional

from django.conf import settings
from django.core import signing
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.utils.timezone import now as timezone_now
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from pydantic import Json

from corporate.lib.decorator import (
    authenticated_remote_realm_management_endpoint,
    authenticated_remote_server_management_endpoint,
    self_hosting_management_endpoint,
)
from corporate.lib.remote_billing_util import (
    REMOTE_BILLING_SESSION_VALIDITY_SECONDS,
    LegacyServerIdentityDict,
    RemoteBillingIdentityDict,
    RemoteBillingUserDict,
    get_identity_dict_from_session,
)
from corporate.lib.stripe import RemoteRealmBillingSession, RemoteServerBillingSession
from zerver.lib.exceptions import JsonableError, MissingRemoteRealmError
from zerver.lib.remote_server import RealmDataForAnalytics, UserDataForRemoteBilling
from zerver.lib.response import json_success
from zerver.lib.timestamp import datetime_to_timestamp
from zerver.lib.typed_endpoint import typed_endpoint
from zilencer.models import RemoteRealm, RemoteZulipServer, get_remote_server_by_uuid

billing_logger = logging.getLogger("corporate.stripe")


VALID_NEXT_PAGES = [None, "sponsorship", "upgrade", "billing", "plans"]
VALID_NEXT_PAGES_TYPE = Literal[None, "sponsorship", "upgrade", "billing", "plans"]


@csrf_exempt
@typed_endpoint
def remote_server_billing_entry(
    request: HttpRequest,
    remote_server: RemoteZulipServer,
    *,
    user: Json[UserDataForRemoteBilling],
    realm: Json[RealmDataForAnalytics],
    uri_scheme: Literal["http://", "https://"] = "https://",
    next_page: VALID_NEXT_PAGES_TYPE = None,
) -> HttpResponse:
    if not settings.DEVELOPMENT:
        return render(request, "404.html", status=404)

    try:
        remote_realm = RemoteRealm.objects.get(uuid=realm.uuid, server=remote_server)
    except RemoteRealm.DoesNotExist:
        # This error will prod the remote server to submit its realm info, which
        # should lead to the creation of this missing RemoteRealm registration.
        raise MissingRemoteRealmError

    identity_dict = RemoteBillingIdentityDict(
        user=RemoteBillingUserDict(
            user_email=user.email, user_uuid=str(user.uuid), user_full_name=user.full_name
        ),
        remote_server_uuid=str(remote_server.uuid),
        remote_realm_uuid=str(remote_realm.uuid),
        authenticated_at=datetime_to_timestamp(timezone_now()),
        uri_scheme=uri_scheme,
        next_page=next_page,
    )

    signed_identity_dict = signing.dumps(identity_dict)

    billing_access_url = (
        f"{settings.EXTERNAL_URI_SCHEME}{settings.SELF_HOSTING_MANAGEMENT_SUBDOMAIN}.{settings.EXTERNAL_HOST}"
        + reverse(remote_server_billing_finalize_login, args=[signed_identity_dict])
    )
    return json_success(request, data={"billing_access_url": billing_access_url})


@self_hosting_management_endpoint
def remote_server_billing_finalize_login(
    request: HttpRequest,
    signed_billing_access_token: str,
) -> HttpResponse:
    # Sanity assert, because otherwise these make no sense.
    assert (
        settings.SIGNED_ACCESS_TOKEN_VALIDITY_IN_SECONDS < REMOTE_BILLING_SESSION_VALIDITY_SECONDS
    )

    try:
        identity_dict: RemoteBillingIdentityDict = signing.loads(
            signed_billing_access_token, max_age=settings.SIGNED_ACCESS_TOKEN_VALIDITY_IN_SECONDS
        )
    except signing.SignatureExpired:
        raise JsonableError(_("Billing access token expired."))
    except signing.BadSignature:
        raise JsonableError(_("Invalid billing access token."))

    remote_realm_uuid = identity_dict["remote_realm_uuid"]

    request.session["remote_billing_identities"] = {}
    request.session["remote_billing_identities"][
        f"remote_realm:{remote_realm_uuid}"
    ] = identity_dict

    # TODO: Figure out redirects based on whether the realm/server already has a plan
    # and should be taken to /billing or doesn't have and should be taken to /plans.
    # For now we're only implemented the case where we have the RemoteRealm, and we take
    # to /plans.

    assert identity_dict["next_page"] in VALID_NEXT_PAGES
    if identity_dict["next_page"] is None:
        return HttpResponseRedirect(reverse("remote_realm_plans_page", args=(remote_realm_uuid,)))
    else:
        return HttpResponseRedirect(
            reverse(f"remote_realm_{identity_dict['next_page']}_page", args=(remote_realm_uuid,))
        )


def render_tmp_remote_billing_page(
    request: HttpRequest, realm_uuid: Optional[str], server_uuid: Optional[str]
) -> HttpResponse:
    identity_dict = get_identity_dict_from_session(
        request, realm_uuid=realm_uuid, server_uuid=server_uuid
    )

    if identity_dict is None:
        raise JsonableError(_("User not authenticated"))

    # This key should be set in both RemoteRealm and legacy server
    # login flows.
    remote_server_uuid = identity_dict["remote_server_uuid"]

    remote_realm_uuid = identity_dict.get("remote_realm_uuid")

    user_dict = identity_dict.get("user", {})
    # Mypy recognizes user_dict as "object" otherwise.
    # If not empty, this is actually a RemoteBillingUserDict.
    assert isinstance(user_dict, dict)

    user_email = user_dict.get("user_email")
    user_full_name = user_dict.get("user_full_name")

    try:
        remote_server = RemoteZulipServer.objects.get(uuid=remote_server_uuid)
    except RemoteZulipServer.DoesNotExist:
        raise JsonableError(_("Invalid remote server."))

    remote_realm = None
    if remote_realm_uuid:
        try:
            # Checking for the (uuid, server) is sufficient to be secure here, since the server
            # is authenticated. the uuid_owner_secret is not needed here, it'll be used for
            # for validating transfers of a realm to a different RemoteZulipServer (in the
            # export-import process).
            assert isinstance(remote_realm_uuid, str)
            remote_realm = RemoteRealm.objects.get(uuid=remote_realm_uuid, server=remote_server)
        except RemoteRealm.DoesNotExist:
            raise AssertionError(
                "The remote realm is missing despite being in the RemoteBillingIdentityDict"
            )

    remote_server_and_realm_info = {
        "remote_server_uuid": remote_server_uuid,
        "remote_server_hostname": remote_server.hostname,
        "remote_server_contact_email": remote_server.contact_email,
        "remote_server_plan_type": remote_server.plan_type,
    }

    if remote_realm is not None:
        remote_server_and_realm_info.update(
            {
                "remote_realm_uuid": remote_realm_uuid,
                "remote_realm_host": remote_realm.host,
            }
        )

    return render(
        request,
        "corporate/remote_billing.html",
        context={
            "user_email": user_email,
            "user_full_name": user_full_name,
            "remote_server_and_realm_info": remote_server_and_realm_info,
        },
    )


def remote_billing_plans_common(
    request: HttpRequest, realm_uuid: Optional[str], server_uuid: Optional[str]
) -> HttpResponse:
    """
    Once implemented, this function, shared between remote_realm_plans_page
    and remote_server_plans_page, will return a Plans page, adjusted depending
    on whether the /realm/... or /server/... endpoint is being used
    """

    return render_tmp_remote_billing_page(request, realm_uuid=realm_uuid, server_uuid=server_uuid)


@authenticated_remote_realm_management_endpoint
def remote_realm_plans_page(
    request: HttpRequest, billing_session: RemoteRealmBillingSession
) -> HttpResponse:
    realm_uuid = str(billing_session.remote_realm.uuid)
    return remote_billing_plans_common(request, realm_uuid=realm_uuid, server_uuid=None)


@authenticated_remote_server_management_endpoint
def remote_server_plans_page(
    request: HttpRequest, billing_session: RemoteServerBillingSession
) -> HttpResponse:
    server_uuid = str(billing_session.remote_server.uuid)
    return remote_billing_plans_common(request, server_uuid=server_uuid, realm_uuid=None)


def remote_billing_page_common(
    request: HttpRequest, realm_uuid: Optional[str], server_uuid: Optional[str]
) -> HttpResponse:
    """
    Once implemented, this function, shared between remote_realm_billing_page
    and remote_server_billing_page, will return a Billing page, adjusted depending
    on whether the /realm/... or /server/... endpoint is being used
    """

    return render_tmp_remote_billing_page(request, realm_uuid=realm_uuid, server_uuid=server_uuid)


@self_hosting_management_endpoint
@typed_endpoint
def remote_billing_legacy_server_login(
    request: HttpRequest,
    *,
    server_org_id: Optional[str] = None,
    server_org_secret: Optional[str] = None,
    next_page: VALID_NEXT_PAGES_TYPE = None,
) -> HttpResponse:
    context: Dict[str, Any] = {"next_page": next_page}
    if server_org_id is None or server_org_secret is None:
        context.update({"error_message": False})
        return render(request, "corporate/legacy_server_login.html", context)

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        remote_server = get_remote_server_by_uuid(server_org_id)
    except RemoteZulipServer.DoesNotExist:
        context.update(
            {"error_message": _("Did not find a server registration for this server_org_id.")}
        )
        return render(request, "corporate/legacy_server_login.html", context)

    if not constant_time_compare(server_org_secret, remote_server.api_key):
        context.update({"error_message": _("Invalid server_org_secret.")})
        return render(request, "corporate/legacy_server_login.html", context)

    if remote_server.deactivated:
        context.update({"error_message": _("Your server registration has been deactivated.")})
        return render(request, "corporate/legacy_server_login.html", context)
    remote_server_uuid = str(remote_server.uuid)

    request.session["remote_billing_identities"] = {}
    request.session["remote_billing_identities"][
        f"remote_server:{remote_server_uuid}"
    ] = LegacyServerIdentityDict(
        remote_server_uuid=remote_server_uuid,
        authenticated_at=datetime_to_timestamp(timezone_now()),
    )

    assert next_page in VALID_NEXT_PAGES
    if next_page is None:
        return HttpResponseRedirect(
            reverse("remote_server_billing_page", args=(remote_server_uuid,))
        )
    else:
        return HttpResponseRedirect(
            reverse(f"remote_server_{next_page}_page", args=(remote_server_uuid,))
        )
