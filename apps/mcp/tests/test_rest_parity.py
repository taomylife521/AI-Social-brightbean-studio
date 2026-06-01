"""Gap 4 + 5 regression: MCP and REST must serialize Post identically.

Both surfaces delegate to ``apps.api.schemas.PostResponse.from_post`` so
they cannot drift in either field set (Gap 4: previously the MCP body
omitted ``created_at``, ``updated_at``, and ``platform_post_id``) or
wire format (Gap 5: previously REST emitted ``2026-06-15T09:00:00Z``
while MCP emitted ``2026-06-15T09:00:00+00:00``).

If these fail, do NOT hand-fix the MCP serializer — fix
``PostResponse.from_post`` and the failure goes away in both places.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost, Post
from apps.members.models import (
    PERMISSION_KEYS,
    OrgMembership,
    WorkspaceMembership,
)


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


MCP_URL = "/api/v1/mcp/"


def _rpc(method, params=None, *, id_=1):
    return {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": id_}


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="parity@example.com",
        password="testpass123",
        name="Parity",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(db):
    from apps.organizations.models import Organization

    return Organization.objects.create(name="Parity Org")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Parity Workspace", organization=organization)


@pytest.fixture
def owner_memberships(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    return WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )


@pytest.fixture
def social_account(db, workspace):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="li-parity",
        account_name="Parity LinkedIn",
        connection_status="connected",
    )


@pytest.fixture
def issued_key(db, user, owner_memberships, workspace, social_account):
    return services.issue_api_key(
        workspace=workspace,
        social_accounts=[social_account],
        issued_by=user,
        name="parity",
        permissions=list(PERMISSION_KEYS),
    )


@pytest.fixture
def client_with_token(issued_key):
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued_key.plaintext_token}")


@pytest.fixture
def scheduled_post(db, user, workspace, social_account):
    """A scheduled Post with one PlatformPost child plus a non-empty
    ``platform_post_id`` so we exercise every field the old MCP serializer
    used to drop. ``Post.status`` is a property derived from children, so
    we set the status on the PlatformPost only.
    """
    when = timezone.now() + timedelta(hours=2)
    post = Post.objects.create(
        workspace=workspace,
        author=user,
        title="Parity title",
        caption="Parity caption",
        first_comment="Parity first comment",
        scheduled_at=when,
    )
    PlatformPost.objects.create(
        post=post,
        social_account=social_account,
        status="scheduled",
        scheduled_at=when,
        platform_post_id="upstream-abc-123",
    )
    return post


@pytest.mark.django_db
class TestRestMcpPostParity:
    def test_mcp_get_post_and_rest_get_post_return_identical_bodies(
        self, client_with_token, scheduled_post
    ):
        rest = client_with_token.get(f"/api/v1/posts/{scheduled_post.id}")
        assert rest.status_code == 200, rest.content
        rest_body = rest.json()

        mcp = client_with_token.post(
            MCP_URL,
            data=json.dumps(
                _rpc(
                    "tools/call",
                    {"name": "get_post", "arguments": {"post_id": str(scheduled_post.id)}},
                )
            ),
            content_type="application/json",
        )
        assert mcp.status_code == 200
        mcp_envelope = mcp.json()
        assert "error" not in mcp_envelope, mcp_envelope
        mcp_body = json.loads(mcp_envelope["result"]["content"][0]["text"])

        assert mcp_body == rest_body, (
            "MCP and REST disagree on the Post payload. "
            "Likely _serialize_post in apps/mcp/handlers.py drifted from "
            "PostResponse.from_post — they MUST share the schema."
        )

    def test_mcp_payload_includes_gap_4_fields(self, client_with_token, scheduled_post):
        """Gap 4: ``created_at``, ``updated_at``, ``platform_post_id`` were
        previously absent on the MCP side.
        """
        mcp = client_with_token.post(
            MCP_URL,
            data=json.dumps(
                _rpc(
                    "tools/call",
                    {"name": "get_post", "arguments": {"post_id": str(scheduled_post.id)}},
                )
            ),
            content_type="application/json",
        )
        body = json.loads(mcp.json()["result"]["content"][0]["text"])
        assert "created_at" in body
        assert "updated_at" in body
        assert body["platform_posts"][0]["platform_post_id"] == "upstream-abc-123"

    def test_mcp_scheduled_at_uses_z_suffix_for_utc(self, client_with_token, scheduled_post):
        """Gap 5: MCP used to emit ``+00:00``; both surfaces now emit ``Z``."""
        mcp = client_with_token.post(
            MCP_URL,
            data=json.dumps(
                _rpc(
                    "tools/call",
                    {"name": "get_post", "arguments": {"post_id": str(scheduled_post.id)}},
                )
            ),
            content_type="application/json",
        )
        body = json.loads(mcp.json()["result"]["content"][0]["text"])
        assert body["scheduled_at"].endswith("Z"), body["scheduled_at"]
        assert "+00:00" not in body["scheduled_at"]
        assert body["platform_posts"][0]["scheduled_at"].endswith("Z")
