"""Views for the Post Composer (F-2.1)."""

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

from .forms import PostForm
from .models import PlatformPost, Post, PostMedia, PostVersion


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
            status=SocialAccount.Status.CONNECTED,
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

    # Permissions for action buttons
    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    can_publish = perms.get("publish_directly", False)
    can_approve = perms.get("approve_posts", False)

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
        "is_edit": post is not None,
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
    elif action == "submit_for_approval":
        if post.status == "draft" or post.status == "changes_requested":
            post.transition_to("pending_review")
    else:
        # save_draft — keep as draft
        if not post.status or post.status in ("", "draft"):
            post.status = "draft"

    post.save()

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
