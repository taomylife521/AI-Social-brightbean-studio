"""Onboarding checklist evaluation logic.

Computes the 5 checklist items and their completion status for a workspace.
Used by the workspace dashboard to render the dynamic "Get Started" card.
"""

from django.urls import reverse

from apps.workspaces.models import Workspace


def get_checklist_items(workspace):
    """Return a list of checklist item dicts with dynamic completion status.

    Each item has: key, title, description, completed (bool), url, icon_color, icon_svg
    """
    from apps.calendar.models import PostingSlot
    from apps.composer.models import Post
    from apps.members.models import WorkspaceMembership
    from apps.social_accounts.models import SocialAccount

    workspace_id = workspace.id

    items = [
        {
            "key": "connect_accounts",
            "title": "Connect social accounts",
            "description": "Link your Instagram, LinkedIn, or other platforms",
            "completed": SocialAccount.objects.for_workspace(workspace_id).exists(),
            "url": reverse(
                "social_accounts:connect",
                kwargs={"workspace_id": workspace_id},
            ),
            "icon_color": "sky",
            "icon_svg": '<path d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101"/><path d="M10.172 13.828a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/>',
        },
        {
            "key": "invite_client",
            "title": "Invite your client",
            "description": "Add a client to review and approve content",
            "completed": WorkspaceMembership.objects.filter(
                workspace_id=workspace_id,
                workspace_role=WorkspaceMembership.WorkspaceRole.CLIENT,
            ).exists(),
            "url": reverse("members:list"),
            "icon_color": "emerald",
            "icon_svg": '<path d="M16 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="8.5" cy="7" r="4"/><path d="M20 8v6m3-3h-6"/>',
        },
        {
            "key": "set_approval",
            "title": "Set approval workflow",
            "description": "Choose how posts get reviewed before publishing",
            "completed": workspace.approval_workflow_mode != Workspace.ApprovalWorkflowMode.NONE,
            "url": reverse("settings_manager:index"),
            "icon_color": "indigo",
            "icon_svg": '<path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>',
        },
        {
            "key": "create_post",
            "title": "Create your first post",
            "description": "Draft and schedule content for your audience",
            "completed": Post.objects.for_workspace(workspace_id).exists(),
            "url": reverse(
                "composer:compose",
                kwargs={"workspace_id": workspace_id},
            ),
            "icon_color": "brand",
            "icon_svg": '<path d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/>',
        },
        {
            "key": "posting_schedule",
            "title": "Set up posting schedule",
            "description": "Configure default posting times for your channels",
            "completed": PostingSlot.objects.filter(
                social_account__workspace_id=workspace_id,
                is_active=True,
            ).exists(),
            "url": reverse(
                "calendar:posting_slots",
                kwargs={"workspace_id": workspace_id},
            ),
            "icon_color": "amber",
            "icon_svg": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
        },
    ]
    return items
