from allauth.account.signals import user_signed_up
from django.db.models.signals import post_save
from django.dispatch import receiver


def provision_organization_and_workspace(user):
    """Create a default Organization, Workspace, and memberships for a new user.

    Skips if the user already belongs to an organization (e.g. invited users).
    Safe to call multiple times — the guard is idempotent.
    """
    from apps.members.models import OrgMembership, WorkspaceMembership
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    # Skip if user was invited to an existing org or already provisioned
    if OrgMembership.objects.filter(user=user).exists():
        return

    org = Organization.objects.create(
        name="My Organization",
        default_timezone="UTC",
    )

    OrgMembership.objects.create(
        user=user,
        organization=org,
        org_role=OrgMembership.OrgRole.OWNER,
    )

    # Create a default workspace so the user can start immediately
    workspace = Workspace.objects.create(
        organization=org,
        name="My Workspace",
        description="Your default workspace. Rename it anytime.",
    )

    WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )

    # Set as last workspace so dashboard redirects here
    user.last_workspace_id = workspace.id
    user.save(update_fields=["last_workspace_id"])


@receiver(user_signed_up)
def create_organization_on_signup(sender, request, user, **kwargs):
    """Handle allauth signup — create org + workspace.

    If the user signed up via an invitation link, accept the invitation
    instead of creating a default org. The invite token is stored in
    the session by the accept_invite view.

    By this point, post_save has already fired and provisioned a default
    "My Organization". If invite acceptance succeeds, we clean up that
    default org so the user only belongs to the invited org.
    """
    pending_token = request.session.pop("pending_invite_token", None)
    if pending_token:
        from apps.members.models import Invitation, OrgMembership
        from apps.members.services import accept_invitation
        from apps.workspaces.models import Workspace

        try:
            invitation = Invitation.objects.get(
                token=pending_token,
                accepted_at__isnull=True,
            )
            if not invitation.is_expired:
                invited_org_id = invitation.organization_id

                # Accept the invitation (creates OrgMembership + WorkspaceMemberships)
                accept_invitation(invitation, user)

                # Clean up the default org that post_save created, if it's
                # different from the invited org.
                default_memberships = OrgMembership.objects.filter(
                    user=user,
                ).exclude(organization_id=invited_org_id)
                for membership in default_memberships:
                    org = membership.organization
                    membership.delete()
                    # Only delete the org if it's the auto-provisioned one
                    # and has no other members.
                    if (
                        org.name == "My Organization"
                        and not org.memberships.exists()
                    ):
                        Workspace.objects.filter(organization=org).delete()
                        org.delete()

                return  # Done — user is now in the invited org only
        except Invitation.DoesNotExist:
            pass  # Fall through to default provisioning
        except ValueError:
            pass  # Invite acceptance failed (e.g. email mismatch) — keep default org

    # No invite or invite failed — ensure default provisioning happened.
    # post_save already handled this, so this is a no-op (idempotent guard).
    provision_organization_and_workspace(user)


@receiver(post_save, sender="accounts.User")
def create_organization_on_user_create(sender, instance, created, **kwargs):
    """Handle any user creation path (createsuperuser, admin, shell).

    The allauth signal fires *after* post_save, so for normal signups
    post_save runs first and the allauth handler is a no-op (idempotent guard).
    """
    if created:
        provision_organization_and_workspace(instance)
