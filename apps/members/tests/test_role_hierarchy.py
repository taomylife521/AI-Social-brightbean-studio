"""HTTP-level tests for invite / role-management hierarchy enforcement.

Covers V1 from the May-2026 security audit: an org admin must not be able to
invite users with org/workspace roles above their own, nor demote an org owner.
"""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


def _login(client, user):
    client.force_login(user)


def _make_user(email):
    user = User.objects.create_user(
        email=email,
        password="testpass123",
        tos_accepted_at=timezone.now(),
    )
    # The accounts post_save signal auto-provisions a default Organization +
    # Workspace + OrgMembership for every new User. Tests that want to attach
    # the user to a specific org must start from a clean slate, otherwise the
    # RBAC middleware (which does OrgMembership.objects.filter(...).first())
    # may pick the auto-org instead of the test org.
    from apps.members.models import OrgMembership, WorkspaceMembership
    from apps.organizations.models import Organization

    auto_org_ids = list(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True))
    WorkspaceMembership.objects.filter(user=user).delete()
    OrgMembership.objects.filter(user=user).delete()
    Organization.objects.filter(id__in=auto_org_ids).delete()
    return user


class InviteRoleHierarchyTests(TestCase):
    """POST /members/invite/ must enforce role-hierarchy."""

    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.workspace_a = Workspace.objects.create(organization=self.org, name="WS-A")
        self.workspace_b = Workspace.objects.create(organization=self.org, name="WS-B")

        self.owner = _make_user("owner@example.com")
        self.admin = _make_user("admin@example.com")
        OrgMembership.objects.create(user=self.owner, organization=self.org, org_role="owner")
        OrgMembership.objects.create(user=self.admin, organization=self.org, org_role="admin")
        # Admin is a viewer in WS-A (cannot grant owner there) and not a member of WS-B.
        WorkspaceMembership.objects.create(user=self.admin, workspace=self.workspace_a, workspace_role="viewer")

        self.url = reverse("members:invite")

    def test_admin_cannot_invite_as_owner_of_workspace_they_only_view(self):
        _login(self.client, self.admin)
        response = self.client.post(
            self.url,
            data={
                "email": "victim@example.com",
                "org_role": "member",
                f"ws_{self.workspace_a.id}": "1",
                f"ws_role_{self.workspace_a.id}": "owner",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Invitation.objects.filter(email="victim@example.com").exists())

    def test_admin_cannot_invite_into_workspace_they_dont_belong_to(self):
        _login(self.client, self.admin)
        response = self.client.post(
            self.url,
            data={
                "email": "victim@example.com",
                "org_role": "member",
                f"ws_{self.workspace_b.id}": "1",
                f"ws_role_{self.workspace_b.id}": "viewer",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Invitation.objects.filter(email="victim@example.com").exists())

    def test_admin_cannot_invite_as_admin(self):
        # Only owners can grant admin (lateral grants from admin → admin forbidden).
        _login(self.client, self.admin)
        response = self.client.post(
            self.url,
            data={
                "email": "lateral@example.com",
                "org_role": "admin",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(Invitation.objects.filter(email="lateral@example.com").exists())

    def test_owner_can_invite_admin_with_any_workspace_role(self):
        _login(self.client, self.owner)
        response = self.client.post(
            self.url,
            data={
                "email": "legit@example.com",
                "org_role": "admin",
                f"ws_{self.workspace_a.id}": "1",
                f"ws_role_{self.workspace_a.id}": "owner",
            },
        )
        # 200 (HTML redirect) or 302 — anything < 400 means accepted. The view
        # returns redirect to members:list for non-HTMX flows.
        self.assertLess(response.status_code, 400)
        self.assertTrue(Invitation.objects.filter(email="legit@example.com").exists())

    def test_admin_with_manager_role_can_invite_editor(self):
        WorkspaceMembership.objects.create(user=self.admin, workspace=self.workspace_b, workspace_role="manager")
        _login(self.client, self.admin)
        response = self.client.post(
            self.url,
            data={
                "email": "editor@example.com",
                "org_role": "member",
                f"ws_{self.workspace_b.id}": "1",
                f"ws_role_{self.workspace_b.id}": "editor",
            },
        )
        self.assertLess(response.status_code, 400)
        self.assertTrue(Invitation.objects.filter(email="editor@example.com").exists())


class UpdateMemberRoleHierarchyTests(TestCase):
    """POST /members/<id>/role/ — admin cannot demote owner."""

    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.owner = _make_user("owner@example.com")
        self.other_owner = _make_user("owner2@example.com")
        self.admin = _make_user("admin@example.com")
        OrgMembership.objects.create(user=self.owner, organization=self.org, org_role="owner")
        self.other_owner_membership = OrgMembership.objects.create(
            user=self.other_owner, organization=self.org, org_role="owner"
        )
        OrgMembership.objects.create(user=self.admin, organization=self.org, org_role="admin")

    def test_admin_cannot_demote_owner(self):
        _login(self.client, self.admin)
        url = reverse("members:update_role", kwargs={"membership_id": self.other_owner_membership.id})
        response = self.client.post(url, data={"org_role": "admin"})
        self.assertEqual(response.status_code, 422)
        self.other_owner_membership.refresh_from_db()
        self.assertEqual(self.other_owner_membership.org_role, "owner")

    def test_admin_cannot_promote_member_to_admin(self):
        # Lateral promotion to admin requires owner privileges.
        member = _make_user("member@example.com")
        membership = OrgMembership.objects.create(user=member, organization=self.org, org_role="member")
        _login(self.client, self.admin)
        url = reverse("members:update_role", kwargs={"membership_id": membership.id})
        response = self.client.post(url, data={"org_role": "admin"})
        self.assertEqual(response.status_code, 422)
        membership.refresh_from_db()
        self.assertEqual(membership.org_role, "member")
