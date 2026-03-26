"""Views for the Post Composer (F-2.1)."""

import contextlib
import json
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import models
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .forms import ContentCategoryForm, PostForm
from .models import (
    ContentCategory,
    Idea,
    PlatformPost,
    Post,
    PostMedia,
    PostTemplate,
    PostVersion,
)


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    has_membership = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=workspace,
    ).exists()
    if not has_membership:
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


def _sync_platform_posts(request, post, workspace):
    """Sync platform post selections from form data."""
    selected_ids_str = request.POST.get("selected_accounts", "")
    selected_ids = [s.strip() for s in selected_ids_str.split(",") if s.strip()]
    post.platform_posts.exclude(social_account_id__in=selected_ids).delete()
    for acc_id in selected_ids:
        try:
            account = SocialAccount.objects.get(id=acc_id, workspace=workspace)
        except SocialAccount.DoesNotExist:
            continue
        pp, _created = PlatformPost.objects.get_or_create(
            post=post,
            social_account=account,
        )
        override_caption = request.POST.get(f"override_caption_{acc_id}", "").strip()
        override_comment = request.POST.get(f"override_comment_{acc_id}", "").strip()
        pp.platform_specific_caption = override_caption if override_caption else None
        pp.platform_specific_first_comment = override_comment if override_comment else None
        pp.save()


def _save_version(post, user):
    """Create a PostVersion snapshot."""
    version_number = (post.versions.count()) + 1
    snapshot = {
        "caption": post.caption,
        "first_comment": post.first_comment,
        "internal_notes": post.internal_notes,
        "tags": post.tags,
        "status": post.status,
        "scheduled_at": post.scheduled_at.isoformat() if post.scheduled_at else None,
        "platform_posts": [
            {
                "social_account_id": str(pp.social_account_id),
                "platform": pp.social_account.platform,
                "caption_override": pp.platform_specific_caption,
                "first_comment_override": pp.platform_specific_first_comment,
            }
            for pp in post.platform_posts.select_related("social_account")
        ],
        "media": [
            {
                "media_asset_id": str(pm.media_asset_id),
                "position": pm.position,
                "alt_text": pm.alt_text,
            }
            for pm in post.media_attachments.all()
        ],
    }
    PostVersion.objects.create(
        post=post,
        version_number=version_number,
        snapshot=snapshot,
        created_by=user,
    )


@login_required
@require_permission("create_posts")
def compose(request, workspace_id, post_id=None):
    """Render the full-page composer for creating or editing a post."""
    workspace = _get_workspace(request, workspace_id)

    # Load existing post or prepare a blank one
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        # Enforce edit permissions: authors can edit their own posts,
        # but editing another user's post requires edit_others_posts.
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if post.author != request.user and not perms.get("edit_others_posts", False):
            raise PermissionDenied("You do not have permission to edit this post.")
        form = PostForm(instance=post)
        selected_account_ids = list(post.platform_posts.values_list("social_account_id", flat=True))
        media_attachments = post.media_attachments.select_related("media_asset").all()
    else:
        post = None
        form = PostForm()
        selected_account_ids = []
        media_attachments = []

    # Connected social accounts for this workspace
    social_accounts = (
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        .order_by("platform", "account_name")
    )

    # Platform character limits for JS
    char_limits = {
        str(acc.id): {"platform": acc.platform, "limit": acc.char_limit, "name": acc.account_name}
        for acc in social_accounts
    }

    # Workspace defaults
    default_first_comment = workspace.default_first_comment
    default_hashtags = workspace.default_hashtags

    # Categories for dropdown
    categories = ContentCategory.objects.for_workspace(workspace.id)

    # Queues for "Add to Queue" action
    from apps.calendar.models import Queue

    queues = (
        Queue.objects.for_workspace(workspace.id).filter(is_active=True).select_related("social_account", "category")
    )

    # Permissions for action buttons
    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    can_publish = perms.get("publish_directly", False)
    can_approve = perms.get("approve_posts", False)
    ws_role = membership.workspace_role if membership else None
    can_view_internal_notes = ws_role not in ("client", "viewer") if ws_role else True

    # Template data pre-fill (if using a template)
    template_id = request.GET.get("template")
    template_data = None
    if template_id and not post:
        try:
            tpl = PostTemplate.objects.get(id=template_id, workspace=workspace)
            template_data = tpl.template_data
        except PostTemplate.DoesNotExist:
            pass

    # Approval workflow context
    workflow_mode = workspace.approval_workflow_mode
    show_submit_button = workflow_mode != "none" and not can_publish
    show_resubmit_button = post is not None and post.status in ("changes_requested", "rejected")

    # Approval history and comments for existing posts
    approval_history = []
    post_comments = []
    if post:
        from apps.approvals.models import ApprovalAction
        approval_history = ApprovalAction.objects.filter(post=post).select_related("user").order_by("-created_at")[:10]
        from apps.approvals.comments import get_comments_for_post
        post_comments = get_comments_for_post(post, request.user)

    context = {
        "workspace": workspace,
        "post": post,
        "form": form,
        "social_accounts": social_accounts,
        "selected_account_ids": [str(aid) for aid in selected_account_ids],
        "media_attachments": media_attachments,
        "char_limits_json": json.dumps(char_limits),
        "default_first_comment": default_first_comment,
        "default_hashtags": json.dumps(default_hashtags),
        "can_publish": can_publish,
        "can_approve": can_approve,
        "can_view_internal_notes": can_view_internal_notes,
        "is_edit": post is not None,
        "categories": categories,
        "queues": queues,
        "template_data_json": json.dumps(template_data) if template_data else "null",
        "workflow_mode": workflow_mode,
        "show_submit_button": show_submit_button,
        "show_resubmit_button": show_resubmit_button,
        "approval_history": approval_history,
        "post_comments": post_comments,
    }
    return render(request, "composer/compose.html", context)


@login_required
@require_permission("create_posts")
@require_POST
def save_post(request, workspace_id, post_id=None):
    """Save or update a post (draft, schedule, or publish action)."""
    workspace = _get_workspace(request, workspace_id)
    action = request.POST.get("action", "save_draft")

    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        # Enforce edit permissions: authors can edit their own, others need edit_others_posts
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if post.author != request.user and not perms.get("edit_others_posts", False):
            raise PermissionDenied("You do not have permission to edit this post.")
        form = PostForm(request.POST, instance=post)
    else:
        form = PostForm(request.POST)

    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)

    post = form.save(commit=False)
    post.workspace = workspace
    if not post_id:
        post.author = request.user

    # Handle action
    if action == "schedule":
        sched_date = form.cleaned_data.get("scheduled_date")
        sched_time = form.cleaned_data.get("scheduled_time")
        if sched_date and sched_time:
            ws_tz = workspace.effective_timezone or "UTC"
            import zoneinfo

            tz = zoneinfo.ZoneInfo(ws_tz)
            naive_dt = datetime.combine(sched_date, sched_time)
            aware_dt = naive_dt.replace(tzinfo=tz)
            post.scheduled_at = aware_dt
            # Only transition if not already scheduled (scheduled → scheduled is invalid)
            if post.status != "scheduled":
                post.transition_to("scheduled")
        else:
            return JsonResponse({"errors": {"schedule": "Date and time required."}}, status=400)
    elif action == "publish_now":
        # Server-side permission check — only roles with publish_directly can bypass approval
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if not perms.get("publish_directly", False):
            raise PermissionDenied("You do not have permission to publish directly.")
        post.scheduled_at = timezone.now()
        post.transition_to("scheduled")  # Worker picks up scheduled posts where scheduled_at <= now()
    elif action == "add_to_queue":
        queue_id = request.POST.get("queue_id")
        if not queue_id:
            return JsonResponse({"errors": {"queue": "Queue selection required."}}, status=400)
        from apps.calendar.models import Queue
        from apps.calendar.services import add_to_queue

        queue = get_object_or_404(Queue, id=queue_id, workspace=workspace, is_active=True)
        if not post.status or post.status in ("", "draft"):
            post.status = "draft"
        post.save()
        add_to_queue(post, queue)
        # Save version and return early — post.save() already called
        _save_version(post, request.user)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "submit_for_approval":
        # Save post first so it has a PK, then delegate to approval service
        if not post.status or post.status in ("", "draft"):
            post.status = "draft"
        post.save()
        # Sync platform posts before submitting
        _sync_platform_posts(request, post, workspace)
        _save_version(post, request.user)
        from apps.approvals.services import submit_for_review
        submit_for_review(post, request.user, workspace)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps(
                        {"postSaved": {"postId": str(post.id), "status": post.status}}
                    ),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "resubmit_for_approval":
        # Resubmit after changes requested or rejection
        post.save()
        _sync_platform_posts(request, post, workspace)
        _save_version(post, request.user)
        from apps.approvals.services import resubmit_post
        resubmit_post(post, request.user, workspace)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps(
                        {"postSaved": {"postId": str(post.id), "status": post.status}}
                    ),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    else:
        # save_draft — keep as draft
        if not post.status or post.status in ("", "draft"):
            post.status = "draft"

    post.save()

    # Handle recurring post creation
    make_recurring = request.POST.get("make_recurring")
    if make_recurring and action == "schedule" and post.scheduled_at:
        from apps.calendar.models import RecurrenceRule

        frequency = request.POST.get("recurrence_frequency", "weekly")
        interval_str = request.POST.get("recurrence_interval", "1")
        end_date_str = request.POST.get("recurrence_end_date", "")
        try:
            interval_val = int(interval_str)
        except (ValueError, TypeError):
            interval_val = 1
        end_date_val = None
        if end_date_str:
            try:
                from datetime import date as date_cls

                end_date_val = date_cls.fromisoformat(end_date_str)
            except (ValueError, TypeError):
                pass
        RecurrenceRule.objects.update_or_create(
            post=post,
            defaults={
                "frequency": frequency,
                "interval": interval_val,
                "end_date": end_date_val,
                "is_active": True,
            },
        )

    # Sync platform posts
    selected_ids_str = request.POST.get("selected_accounts", "")
    selected_ids = [s.strip() for s in selected_ids_str.split(",") if s.strip()]

    # Remove deselected platform posts
    post.platform_posts.exclude(social_account_id__in=selected_ids).delete()

    # Create/update platform posts
    for acc_id in selected_ids:
        try:
            account = SocialAccount.objects.get(id=acc_id, workspace=workspace)
        except SocialAccount.DoesNotExist:
            continue
        pp, _created = PlatformPost.objects.get_or_create(
            post=post,
            social_account=account,
        )
        # Check for platform-specific overrides
        override_caption = request.POST.get(f"override_caption_{acc_id}", "").strip()
        override_comment = request.POST.get(f"override_comment_{acc_id}", "").strip()
        pp.platform_specific_caption = override_caption if override_caption else None
        pp.platform_specific_first_comment = override_comment if override_comment else None
        pp.save()

    # Save version
    _save_version(post, request.user)

    # Return appropriate response
    if request.htmx:
        return HttpResponse(
            status=204,
            headers={
                "HX-Trigger": json.dumps(
                    {
                        "postSaved": {
                            "postId": str(post.id),
                            "status": post.status,
                        }
                    }
                ),
            },
        )

    return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)


@login_required
@require_permission("create_posts")
@require_POST
def autosave(request, workspace_id, post_id=None):
    """Auto-save endpoint called every 30 seconds via HTMX.

    On first save for a new post (no post_id), creates the draft and returns
    an HX-Trigger with the new post ID so the client can switch the autosave
    URL to the edit endpoint, preventing duplicate drafts on subsequent ticks.
    """
    workspace = _get_workspace(request, workspace_id)

    is_new = False
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        # Enforce edit permissions on existing posts
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if post.author != request.user and not perms.get("edit_others_posts", False):
            raise PermissionDenied("You do not have permission to edit this post.")
    else:
        # Check if a previous autosave already created a draft for this session
        # by looking for the post_id passed from the client
        client_post_id = request.POST.get("_autosave_post_id", "").strip()
        if client_post_id:
            try:
                post = Post.objects.get(id=client_post_id, workspace=workspace)
            except Post.DoesNotExist:
                post = Post(workspace=workspace, author=request.user, status="draft")
                is_new = True
        else:
            post = Post(workspace=workspace, author=request.user, status="draft")
            is_new = True

    post.caption = request.POST.get("caption", "")
    post.first_comment = request.POST.get("first_comment", "")
    post.internal_notes = request.POST.get("internal_notes", "")

    tags_raw = request.POST.get("tags", "")
    if tags_raw:
        post.tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    post.save()

    # Sync platform selections
    selected_ids_str = request.POST.get("selected_accounts", "")
    selected_ids = [s.strip() for s in selected_ids_str.split(",") if s.strip()]
    post.platform_posts.exclude(social_account_id__in=selected_ids).delete()
    for acc_id in selected_ids:
        PlatformPost.objects.get_or_create(
            post=post,
            social_account_id=acc_id,
        )

    return HttpResponse(
        f'<span class="text-xs text-gray-400">Saved {timezone.now().strftime("%H:%M")}</span>',
        headers={"HX-Trigger": json.dumps({"autosaved": {"postId": str(post.id), "isNew": is_new}})},
    )


@login_required
@require_GET
def preview(request, workspace_id):
    """Live preview endpoint — renders platform-specific preview from form state.

    Called via HTMX with debounced POST from the composer.
    Stateless — no DB queries except social account lookup.
    """
    workspace = _get_workspace(request, workspace_id)
    caption = request.GET.get("caption", "")
    first_comment = request.GET.get("first_comment", "")
    selected_ids_str = request.GET.get("selected_accounts", "")
    selected_ids = [s.strip() for s in selected_ids_str.split(",") if s.strip()]

    # Build preview data per platform
    previews = []
    if selected_ids:
        accounts = SocialAccount.objects.filter(
            id__in=selected_ids,
            workspace=workspace,
        ).order_by("platform")
        for account in accounts:
            override_key = f"override_caption_{account.id}"
            effective_caption = request.GET.get(override_key, "") or caption
            char_limit = account.char_limit
            previews.append(
                {
                    "account": account,
                    "caption": effective_caption,
                    "first_comment": first_comment,
                    "char_count": len(effective_caption),
                    "char_limit": char_limit,
                    "is_over_limit": len(effective_caption) > char_limit,
                    "truncated_caption": effective_caption[:char_limit]
                    if len(effective_caption) > char_limit
                    else effective_caption,
                }
            )

    return render(
        request,
        "composer/partials/preview_panel.html",
        {
            "previews": previews,
            "workspace": workspace,
        },
    )


@login_required
@require_GET
def media_picker(request, workspace_id):
    """Modal picker for selecting media from the library."""
    workspace = _get_workspace(request, workspace_id)
    from apps.media_library.models import MediaAsset

    assets = MediaAsset.objects.for_workspace(workspace.id).order_by("-created_at")[:50]
    return render(
        request,
        "composer/partials/media_picker.html",
        {
            "assets": assets,
            "workspace": workspace,
        },
    )


@login_required
@require_POST
def attach_media(request, workspace_id, post_id):
    """Attach a media asset to a post."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    media_asset_id = request.POST.get("media_asset_id")

    if not media_asset_id:
        return JsonResponse({"error": "media_asset_id required"}, status=400)

    from apps.media_library.models import MediaAsset

    asset = get_object_or_404(MediaAsset, id=media_asset_id, workspace=workspace)

    max_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"]
    position = (max_pos or 0) + 1

    PostMedia.objects.get_or_create(
        post=post,
        media_asset=asset,
        defaults={"position": position},
    )

    return render(
        request,
        "composer/partials/media_list.html",
        {
            "media_attachments": post.media_attachments.select_related("media_asset").all(),
            "post": post,
            "workspace": workspace,
        },
    )


@login_required
@require_POST
def upload_media(request, workspace_id, post_id=None):
    """Upload a file directly from the composer and optionally attach to a post."""
    workspace = _get_workspace(request, workspace_id)
    uploaded_file = request.FILES.get("file")

    if not uploaded_file:
        return JsonResponse({"error": "No file provided"}, status=400)

    from apps.media_library.models import MediaAsset

    # Determine media type
    content_type = uploaded_file.content_type or ""
    if content_type.startswith("image/"):
        media_type = MediaAsset.MediaType.IMAGE
    elif content_type.startswith("video/"):
        media_type = MediaAsset.MediaType.VIDEO
    elif content_type == "image/gif":
        media_type = MediaAsset.MediaType.GIF
    else:
        media_type = MediaAsset.MediaType.DOCUMENT

    asset = MediaAsset.objects.create(
        workspace=workspace,
        uploaded_by=request.user,
        file=uploaded_file,
        filename=uploaded_file.name,
        media_type=media_type,
        mime_type=content_type,
        file_size=uploaded_file.size,
        source="upload",
    )

    # If a post ID is provided, attach immediately
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        max_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"]
        position = (max_pos or 0) + 1
        PostMedia.objects.create(post=post, media_asset=asset, position=position)

        return render(
            request,
            "composer/partials/media_list.html",
            {
                "media_attachments": post.media_attachments.select_related("media_asset").all(),
                "post": post,
                "workspace": workspace,
            },
        )

    return JsonResponse(
        {
            "id": str(asset.id),
            "filename": asset.filename,
            "url": asset.file.url if asset.file else "",
        }
    )


@login_required
@require_POST
def remove_media(request, workspace_id, post_id, media_id):
    """Remove a media attachment from a post."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    PostMedia.objects.filter(id=media_id, post=post).delete()

    return render(
        request,
        "composer/partials/media_list.html",
        {
            "media_attachments": post.media_attachments.select_related("media_asset").all(),
            "post": post,
            "workspace": workspace,
        },
    )


@login_required
@require_GET
def drafts_list(request, workspace_id):
    """List all drafts for this workspace."""
    workspace = _get_workspace(request, workspace_id)
    drafts = (
        Post.objects.for_workspace(workspace.id)
        .filter(status="draft")
        .select_related("author")
        .prefetch_related("platform_posts__social_account")
        .order_by("-updated_at")
    )

    return render(
        request,
        "composer/drafts_list.html",
        {
            "workspace": workspace,
            "drafts": drafts,
        },
    )


# ---------------------------------------------------------------------------
# Create landing page & Idea CRUD
# ---------------------------------------------------------------------------


def _idea_columns(workspace_id, tag=None):
    """Build Kanban columns dict for a workspace, optionally filtered by tag."""
    ideas = Idea.objects.for_workspace(workspace_id).select_related("author").order_by("position", "-created_at")
    if tag:
        ideas = ideas.filter(tags__contains=[tag])

    columns = []
    for value, label in Idea.Status.choices:
        columns.append(
            {
                "key": value,
                "label": label,
                "ideas": ideas.filter(status=value),
            }
        )

    # Collect unique tags across all ideas (unfiltered) for the dropdown
    all_ideas = Idea.objects.for_workspace(workspace_id)
    all_tags = set()
    for idea in all_ideas.only("tags"):
        if idea.tags:
            all_tags.update(idea.tags)

    return columns, sorted(all_tags)


@login_required
@require_permission("create_posts")
def create_landing(request, workspace_id):
    """Render the Create landing page with Ideas Kanban board."""
    workspace = _get_workspace(request, workspace_id)
    tab = request.GET.get("tab", "ideas")
    tag = request.GET.get("tag")

    columns, all_tags = _idea_columns(workspace.id, tag)

    context = {
        "workspace": workspace,
        "tab": tab,
        "columns": columns,
        "all_tags": all_tags,
        "active_tag": tag,
    }
    return render(request, "composer/create_landing.html", context)


@login_required
@require_permission("create_posts")
@require_POST
def idea_create(request, workspace_id):
    """Create a new idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    tags_raw = request.POST.get("tags", "")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    if not title:
        return HttpResponse("Title is required.", status=400)

    Idea.objects.create(
        workspace=workspace,
        author=request.user,
        title=title,
        description=description,
        tags=tags,
        status=Idea.Status.UNASSIGNED,
    )

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_edit(request, workspace_id, idea_id):
    """Edit an existing idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)

    idea.title = request.POST.get("title", idea.title).strip()
    idea.description = request.POST.get("description", idea.description).strip()
    tags_raw = request.POST.get("tags", "")
    if tags_raw:
        idea.tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    idea.save()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_delete(request, workspace_id, idea_id):
    """Delete an idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    idea.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_move(request, workspace_id, idea_id):
    """Move an idea to a new column/position via HTMX (drag-and-drop)."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    new_status = request.POST.get("status")
    new_position = request.POST.get("position")

    if new_status and new_status in dict(Idea.Status.choices):
        idea.status = new_status
    if new_position is not None:
        with contextlib.suppress(ValueError, TypeError):
            idea.position = int(new_position)
    idea.save()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_GET
def idea_board(request, workspace_id):
    """Return the Kanban board partial for HTMX refresh."""
    workspace = _get_workspace(request, workspace_id)
    tag = request.GET.get("tag")
    columns, all_tags = _idea_columns(workspace.id, tag)

    return render(
        request,
        "composer/partials/kanban_board.html",
        {
            "workspace": workspace,
            "columns": columns,
            "all_tags": all_tags,
            "active_tag": tag,
        },
    )


# ---------------------------------------------------------------------------
# Content Categories CRUD
# ---------------------------------------------------------------------------


@login_required
def category_list(request, workspace_id):
    """Settings page for managing content categories."""
    workspace = _get_workspace(request, workspace_id)
    categories = ContentCategory.objects.for_workspace(workspace.id)
    form = ContentCategoryForm()

    return render(
        request,
        "composer/categories.html",
        {
            "workspace": workspace,
            "categories": categories,
            "form": form,
        },
    )


@login_required
@require_POST
def category_create(request, workspace_id):
    """Create a new content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    form = ContentCategoryForm(request.POST)

    if not form.is_valid():
        return HttpResponse("Invalid data.", status=400)

    category = form.save(commit=False)
    category.workspace = workspace
    max_pos = ContentCategory.objects.for_workspace(workspace.id).aggregate(models.Max("position"))["position__max"]
    category.position = (max_pos or 0) + 1
    category.save()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


@login_required
@require_POST
def category_edit(request, workspace_id, category_id):
    """Edit a content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    category = get_object_or_404(ContentCategory, id=category_id, workspace=workspace)
    form = ContentCategoryForm(request.POST, instance=category)

    if not form.is_valid():
        return HttpResponse("Invalid data.", status=400)

    form.save()
    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


@login_required
@require_POST
def category_delete(request, workspace_id, category_id):
    """Delete a content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    category = get_object_or_404(ContentCategory, id=category_id, workspace=workspace)
    category.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


# ---------------------------------------------------------------------------
# Post Templates
# ---------------------------------------------------------------------------


@login_required
def template_list(request, workspace_id):
    """Settings page for managing post templates."""
    workspace = _get_workspace(request, workspace_id)
    templates = PostTemplate.objects.for_workspace(workspace.id).select_related("created_by")

    return render(
        request,
        "composer/templates_list.html",
        {
            "workspace": workspace,
            "templates": templates,
        },
    )


@login_required
@require_POST
def save_as_template(request, workspace_id, post_id):
    """Save the current post as a reusable template."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)

    name = request.POST.get("template_name", "").strip()
    if not name:
        name = f"Template from {post.caption_snippet or 'post'}"

    description = request.POST.get("template_description", "").strip()

    template_data = {
        "caption": post.caption,
        "first_comment": post.first_comment,
        "category_id": str(post.category_id) if post.category_id else None,
        "tags": post.tags,
        "platform_account_ids": [str(pp.social_account_id) for pp in post.platform_posts.all()],
        "media_asset_ids": [str(pm.media_asset_id) for pm in post.media_attachments.all()],
    }

    PostTemplate.objects.create(
        workspace=workspace,
        name=name,
        description=description,
        template_data=template_data,
        created_by=request.user,
    )

    if request.htmx:
        return HttpResponse(
            status=204,
            headers={"HX-Trigger": "templateSaved"},
        )
    return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)


@login_required
@require_POST
def template_delete(request, workspace_id, template_id):
    """Delete a post template."""
    workspace = _get_workspace(request, workspace_id)
    tpl = get_object_or_404(PostTemplate, id=template_id, workspace=workspace)
    tpl.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "templateChanged"},
    )


@login_required
@require_GET
def template_picker(request, workspace_id):
    """HTMX partial returning list of templates for the picker modal."""
    workspace = _get_workspace(request, workspace_id)
    templates = PostTemplate.objects.for_workspace(workspace.id).select_related("created_by")

    return render(
        request,
        "composer/partials/template_picker.html",
        {
            "workspace": workspace,
            "templates": templates,
        },
    )


@login_required
@require_GET
def use_template(request, workspace_id, template_id):
    """Redirect to composer with template data pre-filled."""
    workspace = _get_workspace(request, workspace_id)
    get_object_or_404(PostTemplate, id=template_id, workspace=workspace)

    from django.urls import reverse

    compose_url = reverse("composer:compose", kwargs={"workspace_id": workspace.id})
    return redirect(f"{compose_url}?template={template_id}")


# ---------------------------------------------------------------------------
# CSV Import
# ---------------------------------------------------------------------------


@login_required
@require_permission("create_posts")
def csv_upload(request, workspace_id):
    """Render CSV upload page or handle file upload and show column mapping."""
    workspace = _get_workspace(request, workspace_id)

    if request.method == "POST" and request.FILES.get("csv_file"):
        import csv
        import io

        csv_file = request.FILES["csv_file"]
        decoded = csv_file.read().decode("utf-8-sig")
        reader = csv.reader(io.StringIO(decoded))
        rows = list(reader)

        if not rows:
            return render(
                request,
                "composer/csv_import.html",
                {"workspace": workspace, "error": "CSV file is empty."},
            )

        headers = rows[0]
        preview_rows = rows[1:6]  # First 5 data rows

        # Auto-detect column mapping
        field_map = {
            "date": ["date", "publish_date", "scheduled_date"],
            "time": ["time", "publish_time", "scheduled_time"],
            "platforms": ["platform", "platforms", "channel", "channels"],
            "caption": ["caption", "text", "content", "message", "body"],
            "media_url": ["media_url", "media", "image_url", "image", "video_url"],
            "category": ["category", "content_category", "type"],
            "tags": ["tags", "labels", "tag"],
            "first_comment": ["first_comment", "comment"],
        }

        auto_mapping = {}
        for col_idx, header in enumerate(headers):
            header_lower = header.strip().lower().replace(" ", "_")
            for field, aliases in field_map.items():
                if header_lower in aliases:
                    auto_mapping[field] = col_idx
                    break

        # Store CSV in session for the next step
        request.session[f"csv_import_{workspace.id}"] = {
            "headers": headers,
            "rows": rows[1:],  # Exclude header
            "filename": csv_file.name,
        }

        import json as json_mod

        return render(
            request,
            "composer/partials/csv_mapping.html",
            {
                "workspace": workspace,
                "headers": headers,
                "preview_rows": preview_rows,
                "auto_mapping": auto_mapping,
                "auto_mapping_json": json_mod.dumps(auto_mapping),
                "field_choices": list(field_map.keys()),
            },
        )

    return render(
        request,
        "composer/csv_import.html",
        {"workspace": workspace},
    )


@login_required
@require_permission("create_posts")
@require_POST
def csv_preview(request, workspace_id):
    """Validate CSV rows with the selected column mapping and show preview."""
    workspace = _get_workspace(request, workspace_id)
    csv_data = request.session.get(f"csv_import_{workspace.id}")

    if not csv_data:
        return HttpResponse("No CSV data found. Please upload again.", status=400)

    # Parse column mapping from POST
    mapping = {}
    for field in ["date", "time", "platforms", "caption", "media_url", "category", "tags", "first_comment"]:
        col_idx = request.POST.get(f"map_{field}", "")
        if col_idx != "":
            import contextlib

            with contextlib.suppress(ValueError, TypeError):
                mapping[field] = int(col_idx)

    rows = csv_data["rows"]
    errors = []
    valid_count = 0

    from apps.social_accounts.models import SocialAccount

    valid_platforms = {p[0].lower() for p in SocialAccount.Platform.choices}
    connected_accounts = set(
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .values_list("platform", flat=True)
    )

    for row_idx, row in enumerate(rows, start=2):  # Row 2 = first data row
        row_errors = []

        # Validate date
        if "date" in mapping:
            date_val = row[mapping["date"]].strip() if mapping["date"] < len(row) else ""
            if date_val:
                try:
                    from datetime import date as date_cls

                    date_cls.fromisoformat(date_val)
                except ValueError:
                    row_errors.append(f"Invalid date format '{date_val}' (expected YYYY-MM-DD)")
            else:
                row_errors.append("Date is empty")

        # Validate platforms
        if "platforms" in mapping:
            platforms_val = row[mapping["platforms"]].strip() if mapping["platforms"] < len(row) else ""
            if platforms_val:
                for p in platforms_val.split(","):
                    p = p.strip().lower()
                    if p and p not in valid_platforms:
                        row_errors.append(f"Unknown platform '{p}'")
                    elif p and p not in connected_accounts:
                        row_errors.append(f"Platform '{p}' is not connected")

        # Validate caption
        if "caption" in mapping:
            caption_val = row[mapping["caption"]].strip() if mapping["caption"] < len(row) else ""
            if not caption_val:
                row_errors.append("Caption is empty")

        if row_errors:
            errors.append({"row": row_idx, "errors": row_errors})
        else:
            valid_count += 1

    # Store mapping in session
    request.session[f"csv_mapping_{workspace.id}"] = mapping

    return render(
        request,
        "composer/partials/csv_validation.html",
        {
            "workspace": workspace,
            "total_rows": len(rows),
            "valid_count": valid_count,
            "errors": errors[:50],  # Show max 50 errors
            "has_more_errors": len(errors) > 50,
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def csv_confirm_import(request, workspace_id):
    """Kick off the CSV import as a background job."""
    workspace = _get_workspace(request, workspace_id)
    csv_data = request.session.get(f"csv_import_{workspace.id}")
    mapping = request.session.get(f"csv_mapping_{workspace.id}")

    if not csv_data or not mapping:
        return HttpResponse("No CSV data found. Please upload again.", status=400)

    from apps.social_accounts.models import SocialAccount

    rows = csv_data["rows"]
    created_count = 0
    error_count = 0

    for row in rows:
        try:
            caption = row[mapping["caption"]].strip() if "caption" in mapping and mapping["caption"] < len(row) else ""
            if not caption:
                error_count += 1
                continue

            post = Post(
                workspace=workspace,
                author=request.user,
                caption=caption,
                status="draft",
            )

            # Date + time
            if "date" in mapping and mapping["date"] < len(row):
                date_str = row[mapping["date"]].strip()
                time_str = ""
                if "time" in mapping and mapping["time"] < len(row):
                    time_str = row[mapping["time"]].strip()

                if date_str:
                    import zoneinfo

                    ws_tz = workspace.effective_timezone or "UTC"
                    tz = zoneinfo.ZoneInfo(ws_tz)
                    from datetime import time as time_cls

                    d = datetime.strptime(date_str, "%Y-%m-%d").date()
                    t = datetime.strptime(time_str, "%H:%M").time() if time_str else time_cls(9, 0)
                    naive_dt = datetime.combine(d, t)
                    post.scheduled_at = naive_dt.replace(tzinfo=tz)
                    post.status = "scheduled"

            # First comment
            if "first_comment" in mapping and mapping["first_comment"] < len(row):
                post.first_comment = row[mapping["first_comment"]].strip()

            # Tags
            if "tags" in mapping and mapping["tags"] < len(row):
                tags_raw = row[mapping["tags"]].strip()
                if tags_raw:
                    post.tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

            # Category
            if "category" in mapping and mapping["category"] < len(row):
                cat_name = row[mapping["category"]].strip()
                if cat_name:
                    cat, _ = ContentCategory.objects.get_or_create(
                        workspace=workspace,
                        name=cat_name,
                        defaults={"color": "#3B82F6"},
                    )
                    post.category = cat

            post.save()

            # Platforms
            if "platforms" in mapping and mapping["platforms"] < len(row):
                platforms_str = row[mapping["platforms"]].strip()
                if platforms_str:
                    for p in platforms_str.split(","):
                        p = p.strip().lower()
                        accounts = SocialAccount.objects.filter(
                            workspace=workspace,
                            platform=p,
                            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
                        )
                        for acc in accounts:
                            PlatformPost.objects.get_or_create(
                                post=post,
                                social_account=acc,
                            )

            created_count += 1
        except Exception:
            error_count += 1

    # Clean up session data
    request.session.pop(f"csv_import_{workspace.id}", None)
    request.session.pop(f"csv_mapping_{workspace.id}", None)

    return render(
        request,
        "composer/partials/csv_progress.html",
        {
            "workspace": workspace,
            "created_count": created_count,
            "error_count": error_count,
            "total_rows": len(rows),
        },
    )
