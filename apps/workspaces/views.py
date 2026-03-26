from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.onboarding.checklist import get_checklist_items
from apps.onboarding.models import OnboardingChecklist

from .models import Workspace


@login_required
def detail(request, workspace_id):
    try:
        workspace = Workspace.objects.get(id=workspace_id)
    except Workspace.DoesNotExist:
        raise Http404 from None

    # Verify user has access
    if not WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).exists():
        raise Http404

    # Persist last used workspace
    request.user.last_workspace_id = workspace.id
    request.user.save(update_fields=["last_workspace_id"])

    # Onboarding checklist
    checklist_dismissed = OnboardingChecklist.objects.filter(
        user=request.user, workspace=workspace, is_dismissed=True
    ).exists()
    checklist_items = [] if checklist_dismissed else get_checklist_items(workspace)
    completed_count = sum(1 for item in checklist_items if item["completed"])

    return render(
        request,
        "workspaces/detail.html",
        {
            "workspace": workspace,
            "checklist_items": checklist_items,
            "checklist_dismissed": checklist_dismissed,
            "checklist_completed_count": completed_count,
            "checklist_total_count": len(checklist_items),
        },
    )


@login_required
def workspace_list(request):
    memberships = WorkspaceMembership.objects.filter(user=request.user).select_related("workspace")
    workspaces = [m.workspace for m in memberships if not m.workspace.is_archived]
    return render(request, "workspaces/list.html", {"workspaces": workspaces})


@login_required
@require_POST
def workspace_create(request):
    """Create a new workspace in the user's organization."""
    name = request.POST.get("name", "").strip()
    if not name:
        return redirect("dashboard")

    # Get the user's organization
    org_membership = OrgMembership.objects.filter(user=request.user).select_related("organization").first()
    if not org_membership:
        return redirect("dashboard")

    workspace = Workspace.objects.create(
        organization=org_membership.organization,
        name=name,
    )

    WorkspaceMembership.objects.create(
        user=request.user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )

    # Set as current workspace
    request.user.last_workspace_id = workspace.id
    request.user.save(update_fields=["last_workspace_id"])

    return redirect("workspaces:detail", workspace_id=workspace.id)
