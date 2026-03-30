"""Views for the Content Calendar (F-2.3) and Publish page."""

import calendar as cal_mod
import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.composer.models import ContentCategory, Post
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .holidays import get_holidays_for_range
from .models import CustomCalendarEvent, PostingSlot, Queue

# Common timezones for the publish page timezone dropdown
COMMON_TIMEZONES = [
    "US/Eastern",
    "US/Central",
    "US/Mountain",
    "US/Pacific",
    "UTC",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Amsterdam",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Australia/Sydney",
    "Pacific/Auckland",
    "America/Sao_Paulo",
    "America/Toronto",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/New_York",
]


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


def _parse_date(date_str, default=None):
    """Parse a YYYY-MM-DD date string."""
    if date_str:
        try:
            return date.fromisoformat(date_str)
        except (ValueError, TypeError):
            pass
    return default or date.today()


def _get_filtered_posts(workspace, request):
    """Apply calendar filters from query params."""
    qs = (
        Post.objects.for_workspace(workspace.id)
        .select_related("author")
        .prefetch_related("platform_posts__social_account", "media_attachments__media_asset")
    )

    # Status filter
    statuses = request.GET.getlist("status")
    if statuses:
        qs = qs.filter(status__in=statuses)

    # Platform filter
    platforms = request.GET.getlist("platform")
    if platforms:
        qs = qs.filter(platform_posts__social_account__platform__in=platforms).distinct()

    # Author filter
    authors = request.GET.getlist("author")
    if authors:
        qs = qs.filter(author_id__in=authors)

    # Category filter
    categories = request.GET.getlist("category")
    if categories:
        qs = qs.filter(category_id__in=categories)

    # Tag filter
    tags = request.GET.getlist("tag")
    if tags:
        for tag in tags:
            qs = qs.filter(tags__contains=[tag])

    # Date range
    start_date = request.GET.get("start_date")
    end_date = request.GET.get("end_date")
    if start_date:
        qs = qs.filter(scheduled_at__date__gte=_parse_date(start_date))
    if end_date:
        qs = qs.filter(scheduled_at__date__lte=_parse_date(end_date))

    return qs


def _get_publish_context(workspace, request):
    """Build shared context for the publish page (channels, tags, timezone)."""
    # Channels that have posts in this workspace
    channels_with_posts = (
        SocialAccount.objects.filter(
            platform_posts__post__workspace=workspace,
        )
        .distinct()
        .order_by("platform", "account_name")
    )

    # All workspace tags from the Tag model
    from apps.composer.models import Tag

    all_tags = set(Tag.objects.for_workspace(workspace.id).values_list("name", flat=True))

    # Display timezone
    ws_tz = workspace.effective_timezone or "UTC"
    display_timezone = request.GET.get("tz", ws_tz)

    # Build ordered timezone list (workspace default first, then common ones)
    tz_list = [ws_tz]
    for tz in COMMON_TIMEZONES:
        if tz not in tz_list:
            tz_list.append(tz)

    return {
        "channels_with_posts": channels_with_posts,
        "all_tags": sorted(all_tags),
        "display_timezone": display_timezone,
        "timezone_choices": tz_list,
        "workspace_timezone": ws_tz,
        "queue_count": Post.objects.for_workspace(workspace.id).filter(status="scheduled").count(),
        "drafts_count": Post.objects.for_workspace(workspace.id).filter(status="draft").count(),
        "approvals_count": Post.objects.for_workspace(workspace.id).filter(status__in=["pending_review", "pending_client"]).count(),
        "sent_count": Post.objects.for_workspace(workspace.id).filter(status__in=["published", "partially_published"]).count(),
    }


def _apply_publish_filters(qs, request):
    """Apply channel and tag filters from publish page dropdowns."""
    channel = request.GET.get("channel")
    if channel:
        qs = qs.filter(platform_posts__social_account_id=channel).distinct()

    tag = request.GET.get("tag")
    if tag:
        qs = qs.filter(tags__contains=[tag])

    return qs


@login_required
def calendar_view(request, workspace_id):
    """Main publish page — renders calendar or list mode."""
    workspace = _get_workspace(request, workspace_id)
    mode = request.GET.get("mode", "calendar")
    active_tab = request.GET.get("tab", "queue")
    view_type = request.GET.get("view", "month")
    target_date = _parse_date(request.GET.get("date"))

    # Connected accounts for calendar filter UI
    social_accounts = (
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        .order_by("platform")
    )

    # Authors for filter
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    authors = (
        user_model.objects.filter(
            authored_posts__workspace=workspace,
        )
        .distinct()
        .values("id", "name", "email")
    )

    # Categories for filter
    categories = ContentCategory.objects.for_workspace(workspace.id)

    # Active filters
    active_filters = {
        "statuses": request.GET.getlist("status"),
        "platforms": request.GET.getlist("platform"),
        "authors": request.GET.getlist("author"),
        "categories": request.GET.getlist("category"),
        "tags": request.GET.getlist("tag"),
    }

    show_holidays = request.GET.get("holidays") == "1"

    # Publish page context (channels, tags, timezone dropdowns)
    publish_ctx = _get_publish_context(workspace, request)

    context = {
        "workspace": workspace,
        "mode": mode,
        "active_tab": active_tab,
        "view_type": view_type,
        "target_date": target_date,
        "social_accounts": social_accounts,
        "authors": authors,
        "categories": categories,
        "active_filters": active_filters,
        "status_choices": Post.Status.choices,
        "show_holidays": show_holidays,
        **publish_ctx,
    }

    # HTMX partial: switching between list and calendar mode
    # Only intercept when the toggle buttons explicitly request a mode switch
    is_htmx = getattr(request, "htmx", False)
    if is_htmx and request.GET.get("_switch_mode"):
        if mode == "list":
            return render(request, "calendar/partials/publish_list_shell.html", context)
        else:
            # Render the full calendar shell (toolbar + grid) for mode switch.
            # We still need the calendar data populated in context first.
            _populate_calendar_context(request, workspace, view_type, target_date, context)
            return render(request, "calendar/partials/publish_calendar_shell.html", context)

    # Full page or calendar HTMX partial (sub-view switching within calendar)
    if mode == "calendar":
        return _render_calendar_partial(request, workspace, view_type, target_date, context)

    # Full page in list mode
    return render(request, "calendar/calendar.html", context)


def _populate_calendar_context(request, workspace, view_type, target_date, context):
    """Populate context with calendar data without rendering.

    Used when we need the calendar data (period_label, prev/next dates, etc.)
    but want to render a different template (e.g., the calendar shell on mode switch).
    """
    if view_type == "month":
        _month_view_data(request, workspace, target_date, context)
    elif view_type == "week":
        _week_view_data(request, workspace, target_date, context)
    elif view_type == "day":
        _day_view_data(request, workspace, target_date, context)
    else:
        _month_view_data(request, workspace, target_date, context)


def _render_calendar_partial(request, workspace, view_type, target_date, context):
    """Render the appropriate calendar partial based on view type."""
    if view_type == "month":
        return _month_view(request, workspace, target_date, context)
    elif view_type == "week":
        return _week_view(request, workspace, target_date, context)
    elif view_type == "day":
        return _day_view(request, workspace, target_date, context)
    elif view_type == "list":
        return _list_view(request, workspace, target_date, context)
    return _month_view(request, workspace, target_date, context)


def _month_view_data(request, workspace, target_date, context):
    """Populate context with month view data (no rendering)."""
    year, month = target_date.year, target_date.month
    cal = cal_mod.Calendar(firstweekday=0)  # Monday first
    weeks = cal.monthdatescalendar(year, month)

    # Get all posts for this month range
    first_day = weeks[0][0]
    last_day = weeks[-1][6]
    posts = (
        _get_filtered_posts(workspace, request)
        .filter(
            scheduled_at__date__gte=first_day,
            scheduled_at__date__lte=last_day,
        )
        .order_by("scheduled_at")
    )

    # Also include drafts without scheduled_at for the current month
    drafts = (
        _get_filtered_posts(workspace, request)
        .filter(
            status="draft",
            scheduled_at__isnull=True,
        )
        .order_by("-updated_at")[:10]
    )

    # Group posts by date
    posts_by_date = defaultdict(list)
    for post in posts:
        if post.scheduled_at:
            posts_by_date[post.scheduled_at.date()].append(post)

    # Holiday overlay
    holidays_by_date = {}
    if context.get("show_holidays"):
        holidays_by_date = get_holidays_for_range(first_day, last_day)

    # Custom calendar events
    custom_events = (
        CustomCalendarEvent.objects.for_workspace(workspace.id)
        .filter(start_date__lte=last_day, end_date__gte=first_day)
        .order_by("start_date")
    )

    # Build weeks data
    today = date.today()
    calendar_weeks = []
    for week in weeks:
        week_data = []
        for day in week:
            day_posts = posts_by_date.get(day, [])
            day_holidays = holidays_by_date.get(day.isoformat(), [])
            day_events = [e for e in custom_events if e.start_date <= day <= e.end_date]
            week_data.append(
                {
                    "date": day,
                    "is_current_month": day.month == month,
                    "is_today": day == today,
                    "is_past": day < today,
                    "posts": day_posts[:3],
                    "total_posts": len(day_posts),
                    "overflow": max(0, len(day_posts) - 3),
                    "holidays": day_holidays,
                    "events": day_events,
                }
            )
        calendar_weeks.append(week_data)

    # Navigation
    prev_month = (date(year, month, 1) - timedelta(days=1)).replace(day=1)
    next_month = (date(year, month, 28) + timedelta(days=4)).replace(day=1)

    context.update(
        {
            "calendar_weeks": calendar_weeks,
            "period_label": date(year, month, 1).strftime("%B %Y"),
            "prev_date": prev_month.isoformat(),
            "next_date": next_month.isoformat(),
            "unscheduled_drafts": drafts,
            "day_names": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        }
    )


def _month_view(request, workspace, target_date, context):
    """Render month view calendar grid."""
    _month_view_data(request, workspace, target_date, context)
    template = "calendar/partials/month_grid.html" if request.htmx else "calendar/calendar.html"
    return render(request, template, context)


def _week_view_data(request, workspace, target_date, context):
    """Populate context with week view data (no rendering)."""
    # Find Monday of the target week
    monday = target_date - timedelta(days=target_date.weekday())
    week_days = [monday + timedelta(days=i) for i in range(7)]

    posts = (
        _get_filtered_posts(workspace, request)
        .filter(
            scheduled_at__date__gte=week_days[0],
            scheduled_at__date__lte=week_days[6],
        )
        .order_by("scheduled_at")
    )

    # Group posts by (date, hour)
    posts_by_slot = defaultdict(list)
    for post in posts:
        if post.scheduled_at:
            local_dt = post.scheduled_at
            key = (local_dt.date(), local_dt.hour)
            posts_by_slot[key].append(post)

    hours = list(range(6, 23))  # 6 AM to 10 PM

    context.update(
        {
            "week_days": week_days,
            "hours": hours,
            "posts_by_slot": dict(posts_by_slot),
            "prev_date": (monday - timedelta(weeks=1)).isoformat(),
            "next_date": (monday + timedelta(weeks=1)).isoformat(),
            "period_label": f"{week_days[0].strftime('%b %d')} – {week_days[6].strftime('%b %d, %Y')}",
        }
    )


def _week_view(request, workspace, target_date, context):
    """Render week view with hourly rows."""
    _week_view_data(request, workspace, target_date, context)
    template = "calendar/partials/week_grid.html" if request.htmx else "calendar/calendar.html"
    return render(request, template, context)


def _day_view_data(request, workspace, target_date, context):
    """Populate context with day view data (no rendering)."""
    posts = (
        _get_filtered_posts(workspace, request)
        .filter(
            scheduled_at__date=target_date,
        )
        .order_by("scheduled_at")
    )

    posts_by_hour = defaultdict(list)
    for post in posts:
        if post.scheduled_at:
            posts_by_hour[post.scheduled_at.hour].append(post)

    hours = list(range(0, 24))

    context.update(
        {
            "posts_by_hour": dict(posts_by_hour),
            "hours": hours,
            "prev_date": (target_date - timedelta(days=1)).isoformat(),
            "next_date": (target_date + timedelta(days=1)).isoformat(),
            "period_label": target_date.strftime("%A, %B %d, %Y"),
        }
    )


def _day_view(request, workspace, target_date, context):
    """Render day view with detailed hour timeline."""
    _day_view_data(request, workspace, target_date, context)
    template = "calendar/partials/day_grid.html" if request.htmx else "calendar/calendar.html"
    return render(request, template, context)


def _list_view(request, workspace, target_date, context):
    """Render list/table view of posts."""
    posts = _get_filtered_posts(workspace, request).order_by("-scheduled_at", "-created_at")[:200]

    context.update(
        {
            "posts": posts,
            "period_label": "All Posts",
            "prev_date": target_date.isoformat(),
            "next_date": target_date.isoformat(),
        }
    )

    template = "calendar/partials/list_view.html" if request.htmx else "calendar/calendar.html"
    return render(request, template, context)


# ---------------------------------------------------------------------------
# Publish page tab views (HTMX partials)
# ---------------------------------------------------------------------------


@login_required
def publish_tab_queue(request, workspace_id):
    """HTMX partial: Queue tab content — shows all scheduled posts."""
    workspace = _get_workspace(request, workspace_id)
    display_tz = request.GET.get("tz", workspace.effective_timezone or "UTC")

    posts = (
        Post.objects.for_workspace(workspace.id)
        .filter(status="scheduled")
        .select_related("author")
        .prefetch_related("platform_posts__social_account", "media_attachments__media_asset")
        .order_by("scheduled_at", "-created_at")
    )
    posts = _apply_publish_filters(posts, request)

    return render(
        request,
        "calendar/partials/publish_queue.html",
        {
            "workspace": workspace,
            "posts": posts[:200],
            "display_timezone": display_tz,
        },
    )


@login_required
def publish_tab_drafts(request, workspace_id):
    """HTMX partial: Drafts tab content for the publish page."""
    workspace = _get_workspace(request, workspace_id)
    display_tz = request.GET.get("tz", workspace.effective_timezone or "UTC")

    posts = (
        Post.objects.for_workspace(workspace.id)
        .filter(status="draft")
        .select_related("author")
        .prefetch_related("platform_posts__social_account", "media_attachments__media_asset")
        .order_by("-updated_at")
    )
    posts = _apply_publish_filters(posts, request)

    return render(
        request,
        "calendar/partials/publish_drafts.html",
        {
            "workspace": workspace,
            "posts": posts[:200],
            "display_timezone": display_tz,
        },
    )


@login_required
def publish_tab_approvals(request, workspace_id):
    """HTMX partial: Approvals tab content for the publish page."""
    workspace = _get_workspace(request, workspace_id)
    display_tz = request.GET.get("tz", workspace.effective_timezone or "UTC")

    status_filter = request.GET.get("approval_status", "all")
    posts = (
        Post.objects.for_workspace(workspace.id)
        .filter(status__in=["pending_review", "pending_client"])
        .select_related("author")
        .prefetch_related("platform_posts__social_account", "media_attachments__media_asset")
        .order_by("scheduled_at", "-created_at")
    )
    posts = _apply_publish_filters(posts, request)

    if status_filter == "pending_review":
        posts = posts.filter(status="pending_review")
    elif status_filter == "pending_client":
        posts = posts.filter(status="pending_client")

    # Permission check for action buttons
    membership = getattr(request, "workspace_membership", None)
    perms = membership.effective_permissions if membership else {}
    can_approve = perms.get("approve_posts", False)

    return render(
        request,
        "calendar/partials/publish_approvals.html",
        {
            "workspace": workspace,
            "posts": posts,
            "status_filter": status_filter,
            "can_approve": can_approve,
            "pending_review_count": Post.objects.for_workspace(workspace.id)
            .filter(status="pending_review")
            .count(),
            "pending_client_count": Post.objects.for_workspace(workspace.id)
            .filter(status="pending_client")
            .count(),
            "display_timezone": display_tz,
        },
    )


@login_required
def publish_tab_sent(request, workspace_id):
    """HTMX partial: Sent tab content for the publish page."""
    workspace = _get_workspace(request, workspace_id)
    display_tz = request.GET.get("tz", workspace.effective_timezone or "UTC")

    posts = (
        Post.objects.for_workspace(workspace.id)
        .filter(status__in=["published", "partially_published"])
        .select_related("author")
        .prefetch_related("platform_posts__social_account", "media_attachments__media_asset")
        .order_by("-scheduled_at", "-created_at")
    )
    posts = _apply_publish_filters(posts, request)

    return render(
        request,
        "calendar/partials/publish_sent.html",
        {
            "workspace": workspace,
            "posts": posts[:200],
            "display_timezone": display_tz,
        },
    )


@login_required
@require_POST
def reschedule_post(request, workspace_id):
    """HTMX endpoint for drag-and-drop rescheduling."""
    workspace = _get_workspace(request, workspace_id)
    post_id = request.POST.get("post_id")
    new_datetime_str = request.POST.get("new_datetime")

    if not post_id or not new_datetime_str:
        return JsonResponse({"error": "post_id and new_datetime required"}, status=400)

    post = get_object_or_404(Post, id=post_id, workspace=workspace)

    # Check permissions — only editable statuses can be rescheduled
    if post.status not in ("draft", "approved", "scheduled"):
        return JsonResponse({"error": "Post cannot be rescheduled in its current status."}, status=400)

    # Check RBAC
    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    is_own_post = post.author_id == request.user.id
    can_edit = (is_own_post and perms.get("edit_own_posts")) or perms.get("edit_others_posts")
    if not can_edit:
        return JsonResponse({"error": "Permission denied."}, status=403)

    try:
        import zoneinfo

        ws_tz = workspace.effective_timezone or "UTC"
        tz = zoneinfo.ZoneInfo(ws_tz)
        new_dt = datetime.fromisoformat(new_datetime_str)
        if new_dt.tzinfo is None:
            new_dt = new_dt.replace(tzinfo=tz)
        post.scheduled_at = new_dt
        if post.status == "draft":
            post.status = "scheduled"
        post.save()
    except (ValueError, TypeError) as e:
        return JsonResponse({"error": f"Invalid datetime: {e}"}, status=400)

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": json.dumps({"postRescheduled": {"postId": str(post.id)}})},
    )


@login_required
def posting_slots(request, workspace_id):
    """Manage posting slots for a workspace's social accounts."""
    workspace = _get_workspace(request, workspace_id)
    accounts = SocialAccount.objects.for_workspace(workspace.id).filter(
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )

    slots = (
        PostingSlot.objects.filter(
            social_account__in=accounts,
        )
        .select_related("social_account")
        .order_by("social_account", "day_of_week", "time")
    )

    # Group by account
    slots_by_account = defaultdict(list)
    for slot in slots:
        slots_by_account[slot.social_account_id].append(slot)

    context = {
        "workspace": workspace,
        "accounts": accounts,
        "slots_by_account": dict(slots_by_account),
        "day_choices": PostingSlot.DayOfWeek.choices,
    }
    return render(request, "calendar/posting_slots.html", context)


@login_required
@require_POST
def save_posting_slot(request, workspace_id):
    """Create or update a posting slot."""
    workspace = _get_workspace(request, workspace_id)
    account_id = request.POST.get("social_account_id")
    day = request.POST.get("day_of_week")
    time_str = request.POST.get("time")

    if not all([account_id, day, time_str]):
        return JsonResponse({"error": "All fields required."}, status=400)

    account = get_object_or_404(
        SocialAccount,
        id=account_id,
        workspace=workspace,
    )

    from datetime import time

    try:
        slot_time = time.fromisoformat(time_str)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid time format."}, status=400)

    slot, created = PostingSlot.objects.get_or_create(
        social_account=account,
        day_of_week=int(day),
        time=slot_time,
        defaults={"is_active": True},
    )

    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Trigger": "slotsUpdated"})
    return JsonResponse({"id": str(slot.id), "created": created})


@login_required
@require_POST
def delete_posting_slot(request, workspace_id, slot_id):
    """Delete a posting slot."""
    workspace = _get_workspace(request, workspace_id)
    slot = get_object_or_404(PostingSlot, id=slot_id)
    # Verify the slot belongs to this workspace
    if slot.social_account.workspace_id != workspace.id:
        return JsonResponse({"error": "Not found."}, status=404)
    slot.delete()

    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Trigger": "slotsUpdated"})
    return JsonResponse({"deleted": True})


# ---------------------------------------------------------------------------
# Queue CRUD
# ---------------------------------------------------------------------------


@login_required
def queue_list(request, workspace_id):
    """List all queues for this workspace."""
    workspace = _get_workspace(request, workspace_id)
    queues = Queue.objects.for_workspace(workspace.id).select_related("social_account", "category")
    accounts = SocialAccount.objects.for_workspace(workspace.id).filter(
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    categories = ContentCategory.objects.for_workspace(workspace.id)

    return render(
        request,
        "calendar/queues.html",
        {
            "workspace": workspace,
            "queues": queues,
            "accounts": accounts,
            "categories": categories,
        },
    )


@login_required
@require_POST
def queue_create(request, workspace_id):
    """Create a new queue."""
    workspace = _get_workspace(request, workspace_id)
    name = request.POST.get("name", "").strip()
    account_id = request.POST.get("social_account_id")
    category_id = request.POST.get("category_id") or None

    if not name or not account_id:
        return JsonResponse({"error": "Name and account required."}, status=400)

    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace)

    Queue.objects.create(
        workspace=workspace,
        name=name,
        social_account=account,
        category_id=category_id,
    )

    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Trigger": "queueChanged"})
    return redirect("calendar:queue_list", workspace_id=workspace.id)


@login_required
def queue_detail(request, workspace_id, queue_id):
    """Show queue entries in order with drag-to-reorder."""
    workspace = _get_workspace(request, workspace_id)
    queue = get_object_or_404(Queue, id=queue_id, workspace=workspace)
    entries = (
        queue.entries.select_related("post__author")
        .prefetch_related("post__platform_posts__social_account")
        .order_by("position")
    )

    return render(
        request,
        "calendar/queue_detail.html",
        {
            "workspace": workspace,
            "queue": queue,
            "entries": entries,
        },
    )


@login_required
@require_POST
def queue_delete(request, workspace_id, queue_id):
    """Delete a queue."""
    workspace = _get_workspace(request, workspace_id)
    queue = get_object_or_404(Queue, id=queue_id, workspace=workspace)
    queue.delete()

    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Trigger": "queueChanged"})
    return redirect("calendar:queue_list", workspace_id=workspace.id)


@login_required
@require_POST
def queue_reorder(request, workspace_id, queue_id):
    """Reorder queue entries via HTMX drag-and-drop."""
    workspace = _get_workspace(request, workspace_id)
    queue = get_object_or_404(Queue, id=queue_id, workspace=workspace)

    entry_ids_str = request.POST.get("entry_ids", "")
    entry_ids = [s.strip() for s in entry_ids_str.split(",") if s.strip()]

    from .services import reorder_queue

    reorder_queue(queue, entry_ids)

    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Trigger": "queueReordered"})
    return JsonResponse({"reordered": True})


# ---------------------------------------------------------------------------
# Custom Calendar Events CRUD
# ---------------------------------------------------------------------------


@login_required
@require_POST
def event_create(request, workspace_id):
    """Create a custom calendar event via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    title = request.POST.get("title", "").strip()
    start_date_str = request.POST.get("start_date", "")
    end_date_str = request.POST.get("end_date", "")
    color = request.POST.get("color", "#3B82F6")
    description = request.POST.get("description", "").strip()

    if not title or not start_date_str or not end_date_str:
        return JsonResponse({"error": "Title, start date, and end date required."}, status=400)

    try:
        start = date.fromisoformat(start_date_str)
        end = date.fromisoformat(end_date_str)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid date format."}, status=400)

    if end < start:
        end = start

    CustomCalendarEvent.objects.create(
        workspace=workspace,
        title=title,
        description=description,
        start_date=start,
        end_date=end,
        color=color,
        created_by=request.user,
    )

    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Trigger": "calendarRefresh"})
    return JsonResponse({"created": True})


@login_required
@require_POST
def event_edit(request, workspace_id, event_id):
    """Edit a custom calendar event."""
    workspace = _get_workspace(request, workspace_id)
    event = get_object_or_404(CustomCalendarEvent, id=event_id, workspace=workspace)

    event.title = request.POST.get("title", event.title).strip()
    event.description = request.POST.get("description", event.description).strip()
    event.color = request.POST.get("color", event.color)

    import contextlib

    start_str = request.POST.get("start_date")
    end_str = request.POST.get("end_date")
    if start_str:
        with contextlib.suppress(ValueError, TypeError):
            event.start_date = date.fromisoformat(start_str)
    if end_str:
        with contextlib.suppress(ValueError, TypeError):
            event.end_date = date.fromisoformat(end_str)

    event.save()

    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Trigger": "calendarRefresh"})
    return JsonResponse({"updated": True})


@login_required
@require_POST
def event_delete(request, workspace_id, event_id):
    """Delete a custom calendar event."""
    workspace = _get_workspace(request, workspace_id)
    event = get_object_or_404(CustomCalendarEvent, id=event_id, workspace=workspace)
    event.delete()

    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Trigger": "calendarRefresh"})
    return JsonResponse({"deleted": True})
