import uuid

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.composer.models import Idea, IdeaGroup
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


class IdeaMediaFlowsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="owner@example.com", password="testpass123")
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
        OrgMembership.objects.create(
            user=self.user,
            organization=self.org,
            org_role=OrgMembership.OrgRole.OWNER,
        )
        WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )

        self.group_a = IdeaGroup.objects.create(workspace=self.workspace, name="Backlog", position=0)
        self.group_b = IdeaGroup.objects.create(workspace=self.workspace, name="In Progress", position=1)

        self.client.force_login(self.user)
        self.create_url = reverse("composer:idea_create", kwargs={"workspace_id": self.workspace.id})
        self.group_create_url = reverse("composer:idea_group_create", kwargs={"workspace_id": self.workspace.id})

    def _create_asset(self, filename="asset.png"):
        return MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            uploaded_by=self.user,
            file=f"media_library/tests/{filename}",
            filename=filename,
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/png",
            file_size=len(b"asset-bytes"),
            source="upload",
        )

    def _edit_url(self, idea_id):
        return reverse("composer:idea_edit", kwargs={"workspace_id": self.workspace.id, "idea_id": idea_id})

    def test_create_attaches_ordered_media_and_sets_cover(self):
        asset_one = self._create_asset("one.png")
        asset_two = self._create_asset("two.png")

        response = self.client.post(
            self.create_url,
            {
                "title": "Idea A",
                "description": "desc",
                "group": str(self.group_a.id),
                "tags": "alpha,beta",
                "media_asset_ids": f"{asset_two.id},{asset_one.id}",
            },
        )

        self.assertEqual(response.status_code, 204)
        idea = Idea.objects.get(title="Idea A")
        attachment_ids = [str(mid) for mid in idea.media_attachments.order_by("position").values_list("media_asset_id", flat=True)]

        self.assertEqual(attachment_ids, [str(asset_two.id), str(asset_one.id)])
        self.assertEqual(idea.media_asset_id, asset_two.id)

    def test_edit_appends_and_reorders_media(self):
        asset_one = self._create_asset("one.png")
        asset_two = self._create_asset("two.png")
        asset_three = self._create_asset("three.png")

        idea = Idea.objects.create(
            workspace=self.workspace,
            author=self.user,
            title="Idea B",
            description="",
            group=self.group_a,
            status=Idea.Status.UNASSIGNED,
            media_asset=asset_one,
        )
        idea.media_attachments.create(media_asset=asset_one, position=0)

        response = self.client.post(
            self._edit_url(idea.id),
            {
                "title": "Idea B",
                "description": "updated",
                "group": str(self.group_a.id),
                "tags": "alpha",
                "media_asset_ids": f"{asset_two.id},{asset_one.id},{asset_three.id}",
            },
        )

        self.assertEqual(response.status_code, 204)
        idea.refresh_from_db()
        attachment_ids = [str(mid) for mid in idea.media_attachments.order_by("position").values_list("media_asset_id", flat=True)]

        self.assertEqual(attachment_ids, [str(asset_two.id), str(asset_one.id), str(asset_three.id)])
        self.assertEqual(idea.media_asset_id, asset_two.id)

    def test_edit_removes_attachments_by_omission(self):
        asset_one = self._create_asset("one.png")
        asset_two = self._create_asset("two.png")

        idea = Idea.objects.create(
            workspace=self.workspace,
            author=self.user,
            title="Idea C",
            group=self.group_a,
            status=Idea.Status.UNASSIGNED,
            media_asset=asset_one,
        )
        idea.media_attachments.create(media_asset=asset_one, position=0)
        idea.media_attachments.create(media_asset=asset_two, position=1)

        response = self.client.post(
            self._edit_url(idea.id),
            {
                "title": "Idea C",
                "description": "",
                "group": str(self.group_a.id),
                "tags": "",
                "media_asset_ids": str(asset_two.id),
            },
        )

        self.assertEqual(response.status_code, 204)
        idea.refresh_from_db()
        attachment_ids = [str(mid) for mid in idea.media_attachments.order_by("position").values_list("media_asset_id", flat=True)]

        self.assertEqual(attachment_ids, [str(asset_two.id)])
        self.assertEqual(idea.media_asset_id, asset_two.id)

    def test_edit_with_empty_tags_clears_tags(self):
        idea = Idea.objects.create(
            workspace=self.workspace,
            author=self.user,
            title="Idea D",
            description="",
            group=self.group_a,
            status=Idea.Status.UNASSIGNED,
            tags=["keep"],
        )

        response = self.client.post(
            self._edit_url(idea.id),
            {
                "title": "Idea D",
                "description": "",
                "group": str(self.group_a.id),
                "tags": "",
                "media_asset_ids": "",
            },
        )

        self.assertEqual(response.status_code, 204)
        idea.refresh_from_db()
        self.assertEqual(idea.tags, [])

    def test_edit_updates_group_when_valid_and_ignores_invalid_group(self):
        idea = Idea.objects.create(
            workspace=self.workspace,
            author=self.user,
            title="Idea E",
            description="",
            group=self.group_a,
            status=Idea.Status.UNASSIGNED,
            tags=["alpha"],
        )

        valid_response = self.client.post(
            self._edit_url(idea.id),
            {
                "title": "Idea E",
                "description": "",
                "group": str(self.group_b.id),
                "tags": "alpha",
                "media_asset_ids": "",
            },
        )
        self.assertEqual(valid_response.status_code, 204)
        idea.refresh_from_db()
        self.assertEqual(idea.group_id, self.group_b.id)

        invalid_group_id = uuid.uuid4()
        invalid_response = self.client.post(
            self._edit_url(idea.id),
            {
                "title": "Idea E",
                "description": "",
                "group": str(invalid_group_id),
                "tags": "alpha",
                "media_asset_ids": "",
            },
        )
        self.assertEqual(invalid_response.status_code, 204)
        idea.refresh_from_db()
        self.assertEqual(idea.group_id, self.group_b.id)

    def test_create_legacy_media_asset_id_fallback(self):
        asset = self._create_asset("legacy.png")

        response = self.client.post(
            self.create_url,
            {
                "title": "Idea F",
                "description": "",
                "group": str(self.group_a.id),
                "tags": "",
                "media_asset_id": str(asset.id),
            },
        )

        self.assertEqual(response.status_code, 204)
        idea = Idea.objects.get(title="Idea F")
        attachment_ids = [str(mid) for mid in idea.media_attachments.order_by("position").values_list("media_asset_id", flat=True)]

        self.assertEqual(idea.media_asset_id, asset.id)
        self.assertEqual(attachment_ids, [str(asset.id)])

    def test_create_returns_json_payload_when_requested(self):
        response = self.client.post(
            self.create_url,
            {
                "title": "Idea JSON",
                "description": "desc",
                "group": str(self.group_a.id),
                "tags": "json,tag",
                "media_asset_ids": "",
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertIn("idea_id", payload)
        self.assertEqual(payload.get("group_id"), str(self.group_a.id))
        self.assertIn('data-idea-id="', payload.get("card_html", ""))

    def test_edit_returns_json_payload_when_requested(self):
        idea = Idea.objects.create(
            workspace=self.workspace,
            author=self.user,
            title="Idea JSON Edit",
            description="",
            group=self.group_a,
            status=Idea.Status.UNASSIGNED,
            tags=["old"],
        )

        response = self.client.post(
            self._edit_url(idea.id),
            {
                "title": "Idea JSON Edit",
                "description": "updated",
                "group": str(self.group_b.id),
                "tags": "new-tag",
                "media_asset_ids": "",
            },
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("idea_id"), str(idea.id))
        self.assertEqual(payload.get("previous_group_id"), str(self.group_a.id))
        self.assertEqual(payload.get("group_id"), str(self.group_b.id))
        self.assertIn('data-idea-id="' + str(idea.id) + '"', payload.get("card_html", ""))

    def test_group_create_and_delete_return_json_when_requested(self):
        create_response = self.client.post(
            self.group_create_url,
            {"name": "JSON Group"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(create_response.status_code, 200)
        create_payload = create_response.json()
        self.assertTrue(create_payload.get("ok"))
        self.assertEqual(create_payload.get("group_name"), "JSON Group")
        self.assertIn('data-group-id="', create_payload.get("column_html", ""))

        group_id = create_payload["group_id"]
        delete_url = reverse(
            "composer:idea_group_delete",
            kwargs={"workspace_id": self.workspace.id, "group_id": group_id},
        )
        delete_response = self.client.post(delete_url, HTTP_ACCEPT="application/json")
        self.assertEqual(delete_response.status_code, 200)
        delete_payload = delete_response.json()
        self.assertTrue(delete_payload.get("ok"))
        self.assertEqual(delete_payload.get("group_id"), group_id)
