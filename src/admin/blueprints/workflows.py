"""Workflow approval and review blueprint for Admin UI."""

import json
import logging
from datetime import UTC, datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import select

from src.admin.utils import require_tenant_access
from src.admin.utils.audit_decorator import log_admin_action
from src.admin.utils.embedded_capabilities import require_capability_blueprint
from src.core.database.database_session import get_db_session
from src.core.database.models import Context
from src.core.database.models import Principal as ModelPrincipal
from src.core.database.repositories import MediaBuyRepository
from src.core.database.repositories.creative import CreativeAssignmentRepository
from src.core.database.repositories.workflow import WorkflowRepository
from src.core.tenant_currency import tenant_reporting_currency
from src.services.protocol_webhook_service import build_request_scoped_config, send_create_media_buy_decision

logger = logging.getLogger(__name__)

workflows_bp = Blueprint("workflows", __name__)
workflows_bp.before_request(require_capability_blueprint("campaign_approval"))


# Step statuses that are waiting for an operator decision.
_AWAITING_DECISION_STEP_STATUSES = frozenset({"requires_approval", "pending_approval"})

# Step statuses that count as handled for the "Completed Today" card.
_HANDLED_STEP_STATUSES = frozenset({"completed", "approved", "rejected"})


def _task_summary_counts(steps, now: datetime) -> dict[str, int]:
    """Task counts for the Workflows page summary cards.

    Approval steps are created as ``requires_approval``; counting only
    ``pending_approval`` left the Pending Tasks card at 0 while tasks waited.
    "Today" is the current UTC day; a naive ``completed_at`` is read as UTC.

    Pure function (no DB/Flask) so it can be unit-tested directly.
    """
    today = now.astimezone(UTC).date()

    def _finished_today(step) -> bool:
        completed_at = step.completed_at
        if completed_at is None or step.status not in _HANDLED_STEP_STATUSES:
            return False
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        return completed_at.astimezone(UTC).date() == today

    return {
        "pending_tasks": sum(1 for s in steps if s.status in _AWAITING_DECISION_STEP_STATUSES),
        "completed_today": sum(1 for s in steps if _finished_today(s)),
    }


@workflows_bp.route("/<tenant_id>/workflows")
@require_tenant_access()
def list_workflows(tenant_id, **kwargs):
    """List all workflows and pending approvals."""
    from src.core.database.models import AuditLog, Tenant

    with get_db_session() as db:
        # Get tenant
        tenant = db.scalars(select(Tenant).filter_by(tenant_id=tenant_id)).first()
        if not tenant:
            return "Tenant not found", 404

        # Get all workflow steps via repository (tenant-scoped)
        workflow_repo = WorkflowRepository(db, tenant_id)
        all_steps = workflow_repo.get_all_steps()

        # Get media buys for context
        media_buy_repo = MediaBuyRepository(db, tenant_id)
        media_buys = media_buy_repo.list_all_ordered_by_created()

        # The ad server's reporting currency (GAM network / Improve Digital office).
        primary_currency = tenant_reporting_currency(db, tenant_id)

        # Build summary stats
        summary = {
            "active_buys": len([mb for mb in media_buys if mb.status == "active"]),
            **_task_summary_counts(all_steps, datetime.now(UTC)),
            "total_spend": sum(mb.budget or 0 for mb in media_buys if mb.status == "active"),
            "currency": primary_currency,
        }

        # Format all workflow steps for display in tasks tab
        workflows_list = []
        for step in all_steps:
            context = db.scalars(select(Context).filter_by(context_id=step.context_id)).first()
            principal = None
            if context and context.principal_id:
                principal = db.scalars(
                    select(ModelPrincipal).filter_by(principal_id=context.principal_id, tenant_id=tenant_id)
                ).first()

            workflows_list.append(
                {
                    "step_id": step.step_id,
                    "context_id": step.context_id,
                    "step_type": step.step_type,
                    "tool_name": step.tool_name,
                    "status": step.status,
                    "created_at": step.created_at,
                    "completed_at": step.completed_at,
                    "principal_name": principal.name if principal else "Unknown",
                    "assigned_to": step.assigned_to,
                    "error_message": step.error_message,
                    "request_data": step.request_data,
                }
            )

        # Get recent audit logs
        stmt = select(AuditLog).filter(AuditLog.tenant_id == tenant_id).order_by(AuditLog.timestamp.desc()).limit(100)
        audit_logs = db.scalars(stmt).all()

        logger.info(f"[workflows] Querying audit logs for tenant_id={tenant_id}")
        logger.info(f"[workflows] Found {len(audit_logs)} audit logs")
        if audit_logs:
            logger.info(
                f"[workflows] Latest audit log: operation={audit_logs[0].operation}, success={audit_logs[0].success}, timestamp={audit_logs[0].timestamp}"
            )
        else:
            all_logs_stmt = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(5)
            all_logs = db.scalars(all_logs_stmt).all()
            logger.warning(
                f"[workflows] No audit logs for tenant {tenant_id}, but found {len(all_logs)} logs total in database"
            )
            if all_logs:
                logger.warning(f"[workflows] Sample log tenant_ids: {[log.tenant_id for log in all_logs]}")

        return render_template(
            "workflows.html",
            tenant=tenant,
            tenant_id=tenant_id,
            summary=summary,
            workflows=workflows_list,
            media_buys=media_buys,
            tasks=workflows_list,
            audit_logs=audit_logs,
        )


@workflows_bp.route("/<tenant_id>/workflows/<workflow_id>/steps/<step_id>/review")
@require_tenant_access()
def review_workflow_step(tenant_id, workflow_id, step_id):
    """Show detailed review page for a workflow step requiring approval."""
    with get_db_session() as db:
        # Get the workflow step via repository (tenant-scoped)
        workflow_repo = WorkflowRepository(db, tenant_id)
        step = workflow_repo.get_step_by_id(step_id)

        if not step:
            flash("Workflow step not found", "error")
            return redirect(url_for("tenants.dashboard", tenant_id=tenant_id))

        # Get the context for tenant/principal info
        context = db.scalars(select(Context).filter_by(context_id=step.context_id)).first()

        # Get principal info
        principal = None
        if context and context.principal_id:
            principal = db.scalars(
                select(ModelPrincipal).filter_by(principal_id=context.principal_id, tenant_id=tenant_id)
            ).first()

        # Parse request data
        request_data = step.request_data if step.request_data else {}

        # Format the data for display
        formatted_request = json.dumps(request_data, indent=2)

        return render_template(
            "workflow_review.html",
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step=step,
            context=context,
            principal=principal,
            request_data=request_data,
            formatted_request=formatted_request,
        )


def _notify_media_buy_decision(step, tenant_id: str, media_buy, media_buy_repo, *, status: str) -> bool:
    """Send the terminal ``create_media_buy`` task webhook after an operator decision.

    The buyer's ``push_notification_config`` is request-scoped: it lives on
    the workflow step's ``request_data``, never in ``push_notification_configs``.
    Returns ``False`` when the request registered no webhook.
    """
    request_data = step.request_data or {}
    webhook_config = build_request_scoped_config(
        tenant_id=tenant_id,
        principal_id=media_buy.principal_id,
        push_config=request_data.get("push_notification_config"),
    )
    if webhook_config is None:
        return False
    return send_create_media_buy_decision(
        config=webhook_config,
        step_id=step.step_id,
        context_id=step.context_id,
        protocol=request_data.get("protocol", "mcp"),
        media_buy_id=media_buy.media_buy_id,
        package_ids=[x.package_id for x in media_buy_repo.get_packages(media_buy.media_buy_id)],
        status=status,
        confirmed_at=media_buy.confirmed_at,
        revision=media_buy.revision,
    )


def _replay_update_media_buy(step, tenant_id: str, db) -> tuple[bool, str | None]:
    """Re-enter `_update_media_buy_impl` with the persisted request payload.

    The originating buyer's `update_media_buy` call landed the step in
    `requires_approval`; the operator just approved it on the workflows
    page. Now we replay the deferred operation against the same identity,
    bypassing the manual-approval gate (which has been satisfied).

    Returns ``(success, error_message)``. ``error_message`` is non-None only
    when the replay raised or returned ``UpdateMediaBuyError``; the caller
    is responsible for surfacing it on the workflow step.
    """
    from src.core.config_loader import get_tenant_by_id
    from src.core.resolved_identity import ResolvedIdentity
    from src.core.schemas import UpdateMediaBuyError, UpdateMediaBuyRequest
    from src.core.tools.media_buy_update import _update_media_buy_impl

    context = db.scalars(select(Context).filter_by(context_id=step.context_id)).first()
    if context is None or not context.principal_id:
        return False, f"Workflow step {step.step_id} has no associated principal context"

    tenant = get_tenant_by_id(tenant_id)
    if tenant is None:
        return False, f"Tenant {tenant_id} not found"

    # Filter persisted payload to the request schema. Drops the
    # request_metadata `protocol` key the impl injects on creation, and
    # any forward-compat fields that don't belong on UpdateMediaBuyRequest.
    raw = step.request_data or {}
    allowed = set(UpdateMediaBuyRequest.model_fields.keys())
    payload = {k: v for k, v in raw.items() if k in allowed}

    try:
        req = UpdateMediaBuyRequest.model_validate(payload)
    except Exception as e:
        return False, f"Failed to reconstruct update_media_buy request: {e}"

    identity = ResolvedIdentity(
        principal_id=context.principal_id,
        tenant_id=tenant_id,
        tenant=tenant,
        protocol="mcp",
    )

    try:
        result = _update_media_buy_impl(
            req=req,
            identity=identity,
            context_id=step.context_id,
            bypass_manual_approval=True,
        )
    except Exception as e:
        logger.error(f"[APPROVAL] update_media_buy replay raised for {step.step_id}: {e}", exc_info=True)
        return False, str(e)

    if isinstance(result, UpdateMediaBuyError):
        msg = "; ".join(err.message for err in result.errors) or "update_media_buy replay failed"
        return False, msg

    return True, None


def _expired_media_buy_decision_error(
    workflow_repo: WorkflowRepository, media_buy_repo: MediaBuyRepository, step_id: str, action: str
) -> str | None:
    """Block approving/rejecting a pending media buy whose flight window has ended.

    Approval would push an order that can no longer deliver to the ad server.
    The buy stays pending; once the buyer moves the flight to a future window
    the decision unblocks on its own.

    Only the ``create_media_buy`` step is guarded: an ``update_media_buy``
    step on the same buy may be exactly the reschedule that unblocks it.
    """
    from src.core.helpers.media_buy_flight import FLIGHT_ENDED_DECISION_MESSAGE, flight_has_ended

    step = workflow_repo.get_by_step_id(step_id)
    if step is None or step.tool_name != "create_media_buy":
        return None

    for mapping in workflow_repo.get_mappings_for_step(step_id):
        if mapping.object_type != "media_buy":
            continue
        media_buy = media_buy_repo.get_by_id(mapping.object_id)
        if media_buy and media_buy.status == "pending_approval" and flight_has_ended(media_buy, datetime.now(UTC)):
            return FLIGHT_ENDED_DECISION_MESSAGE.format(action=action)
    return None


# Step statuses that already carry a decision or an outcome. A step in one of
# these is no longer waiting on the operator.
_DECIDED_STEP_STATUSES = frozenset({"approved", "rejected", "completed", "failed"})

# Creative statuses that count as ready to deliver (mirrors operations.approve_media_buy).
_APPROVED_CREATIVE_STATUSES = ("approved", "active")


def _decided_step_error(step, action: str) -> str | None:
    """Block a second decision on a step that is no longer waiting for one.

    Canceling a media buy closes its open approval steps as ``rejected``;
    without this guard a later approve flipped such a step to ``approved``.

    Pure function (no DB/Flask) so it can be unit-tested directly.
    """
    status = str(getattr(step, "status", "") or "").lower()
    if status in _DECIDED_STEP_STATUSES:
        return f"Cannot {action}: this workflow step is already {status}."
    return None


def _approve_blocked_error(step, media_buy, assignments) -> str | None:
    """Block an approval the media buy page would also refuse.

    This route used to mark the step approved before looking at the buy, so a
    buy with no creatives was booked on the ad server and a canceled or failed
    buy still accepted an approval. ``update_media_buy`` steps are exempt from
    the creative rule: the update may be the change that attaches creatives.

    Pure function (no DB/Flask) so it can be unit-tested directly.
    """
    if media_buy is None:
        return None

    from src.admin.blueprints.operations import _closed_media_buy_decision_error

    closed_error = _closed_media_buy_decision_error(media_buy, "approve")
    if closed_error:
        return closed_error

    if getattr(step, "tool_name", None) == "update_media_buy":
        return None
    if media_buy.status == "pending_approval" and not assignments:
        return "Cannot approve: no creatives are assigned to this media buy. Assign and approve creatives first."
    return None


def _unapproved_creative_ids(creatives) -> list[str]:
    """IDs of creatives that are not yet approved for delivery."""
    return [c.creative_id for c in creatives if c.status not in _APPROVED_CREATIVE_STATUSES]


def _step_media_buy(workflow_repo: WorkflowRepository, media_buy_repo: MediaBuyRepository, step_id: str):
    """The media buy a workflow step is about, or ``None``."""
    mapping = next((m for m in workflow_repo.get_mappings_for_step(step_id) if m.object_type == "media_buy"), None)
    return media_buy_repo.get_by_id(mapping.object_id) if mapping else None


@workflows_bp.route("/<tenant_id>/workflows/<workflow_id>/steps/<step_id>/approve", methods=["POST"])
@require_tenant_access(role=("admin", "member"), allow_embedded_writes=True)
@log_admin_action("approve_workflow_step")
def approve_workflow_step(tenant_id, workflow_id, step_id):
    """Approve a workflow step."""
    try:
        with get_db_session() as db:
            # Get and update the workflow step via repository (tenant-scoped)
            workflow_repo = WorkflowRepository(db, tenant_id)

            user_info = session.get("user", {})
            user_email = user_info.get("email", "system") if isinstance(user_info, dict) else str(user_info)

            expired_error = _expired_media_buy_decision_error(
                workflow_repo, MediaBuyRepository(db, tenant_id), step_id, "approve"
            )
            if expired_error:
                return jsonify({"success": False, "error": expired_error}), 409

            # Refuse before the step is marked approved: nothing below can undo that.
            media_buy_repo = MediaBuyRepository(db, tenant_id)
            pending_step = workflow_repo.get_by_step_id(step_id)
            assignments: list = []
            if pending_step is not None:
                pending_buy = _step_media_buy(workflow_repo, media_buy_repo, step_id)
                if pending_buy is not None:
                    assignments = CreativeAssignmentRepository(db, tenant_id).get_by_media_buy(pending_buy.media_buy_id)
                blocked_error = _decided_step_error(pending_step, "approve") or _approve_blocked_error(
                    pending_step, pending_buy, assignments
                )
                if blocked_error:
                    return jsonify({"success": False, "error": blocked_error}), 409

            step = workflow_repo.update_status(
                step_id,
                status="approved",
                completed_at=datetime.now(UTC),
            )

            if not step:
                return jsonify({"error": "Workflow step not found"}), 404

            db.commit()

            # Dispatch on the originating tool. Each deferred operation
            # has its own replay path; without this dispatch, only the
            # `create_media_buy` flow ever fires and other approvals
            # silently land in `approved` with no side effect.
            if step.tool_name == "update_media_buy":
                success, err_msg = _replay_update_media_buy(step, tenant_id, db)
                if not success:
                    workflow_repo.update_status(
                        step_id,
                        status="failed",
                        error_message=err_msg,
                    )
                    db.commit()
                    logger.error(f"[APPROVAL] update_media_buy replay failed for {step_id}: {err_msg}")
                    flash(f"Workflow approval recorded but update failed: {err_msg}", "error")
                    return jsonify({"success": False, "error": err_msg}), 500
                flash("Workflow step approved and update applied successfully", "success")
                return jsonify({"success": True}), 200

            # Check if this is a media buy creation workflow step
            mappings = workflow_repo.get_mappings_for_step(step_id)
            mapping = next((m for m in mappings if m.object_type == "media_buy"), None)

            logger.info(
                f"[APPROVAL] Checking for ObjectWorkflowMapping: step_id={step_id}, found={mapping is not None}"
            )
            if mapping:
                logger.info(
                    f"[APPROVAL] Found mapping: object_type={mapping.object_type}, object_id={mapping.object_id}"
                )

            if mapping:
                media_buy_id = mapping.object_id
                logger.info(f"[APPROVAL] Workflow step {step_id} approved for media buy {media_buy_id}")

                # Get the media buy
                media_buy = media_buy_repo.get_by_id(media_buy_id)

                logger.info(
                    f"[APPROVAL] Media buy lookup: found={media_buy is not None}, status={media_buy.status if media_buy else 'N/A'}"
                )

                if media_buy and media_buy.status == "pending_approval":
                    # Check if all required creatives are approved before executing adapter creation.
                    # Both lookups are tenant-scoped: creative IDs are chosen by buyers and
                    # repeat across tenants.
                    if assignments:
                        creatives = media_buy_repo.get_creatives_by_ids([a.creative_id for a in assignments])
                        unapproved_creatives = _unapproved_creative_ids(creatives)

                        if unapproved_creatives:
                            logger.warning(
                                f"[APPROVAL] Cannot execute adapter creation yet - "
                                f"{len(unapproved_creatives)} creatives not approved: {unapproved_creatives}"
                            )
                            flash(
                                f"Media buy approved! Waiting for {len(unapproved_creatives)} creative(s) to be approved before creating in GAM.",
                                "info",
                            )
                            media_buy.status = "pending_creatives"
                            db.commit()
                            return jsonify({"success": True}), 200

                    # Execute adapter creation
                    from src.core.tools.media_buy_create import execute_approved_media_buy

                    logger.info(f"[APPROVAL] Executing adapter creation for approved media buy {media_buy_id}")
                    success, error_msg = execute_approved_media_buy(media_buy_id, tenant_id)

                    if not success:
                        # Mirror operations.approve_media_buy: the step is already approved,
                        # so a buy left in pending_approval could never be decided again.
                        logger.error(f"[APPROVAL] Adapter creation failed for {media_buy_id}: {error_msg}")
                        media_buy_repo.update_status(media_buy_id, "failed")
                        db.commit()
                        flash(f"Workflow approved but media buy creation failed: {error_msg}", "error")
                        return jsonify({"success": False, "error": error_msg}), 500

                    # Update media buy status. execute_approved_media_buy wrote
                    # through its own session — refresh so we don't clobber
                    # 'pending_ad_server_approval' (GAM order still awaiting
                    # ad-server approval; the background poller owns the
                    # transition to scheduled/active).
                    db.refresh(media_buy)
                    if media_buy.status != "pending_ad_server_approval":
                        media_buy.status = "scheduled"
                    approved_at = datetime.now(UTC)
                    media_buy.approved_at = approved_at
                    if media_buy.confirmed_at is None:
                        media_buy.confirmed_at = approved_at
                    media_buy.approved_by = user_email
                    db.commit()

                    logger.info(f"[APPROVAL] Media buy {media_buy_id} successfully created in adapter")

                    # Buyer registered push_notification_config on create_media_buy:
                    # send the terminal task-status webhook (spec: completed).
                    _notify_media_buy_decision(step, tenant_id, media_buy, media_buy_repo, status="completed")
                    flash("Workflow step approved and media buy created successfully", "success")
                else:
                    logger.warning(
                        f"[APPROVAL] Media buy not executed: media_buy={media_buy is not None}, status={media_buy.status if media_buy else 'N/A'}"
                    )
                    flash("Workflow step approved successfully", "success")
            else:
                flash("Workflow step approved successfully", "success")

            return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"Error approving workflow step {step_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@workflows_bp.route("/<tenant_id>/workflows/<workflow_id>/steps/<step_id>/reject", methods=["POST"])
@require_tenant_access(role=("admin", "member"), allow_embedded_writes=True)
@log_admin_action("reject_workflow_step")
def reject_workflow_step(tenant_id, workflow_id, step_id):
    """Reject a workflow step with a reason."""
    try:
        data = request.get_json() or {}
        reason = data.get("reason", "No reason provided")

        with get_db_session() as db:
            # Get and update the workflow step via repository (tenant-scoped)
            workflow_repo = WorkflowRepository(db, tenant_id)

            user_info = session.get("user", {})
            user_email = user_info.get("email", "system") if isinstance(user_info, dict) else str(user_info)

            expired_error = _expired_media_buy_decision_error(
                workflow_repo, MediaBuyRepository(db, tenant_id), step_id, "reject"
            )
            if expired_error:
                return jsonify({"success": False, "error": expired_error}), 409

            decided_error = _decided_step_error(workflow_repo.get_by_step_id(step_id), "reject")
            if decided_error:
                return jsonify({"success": False, "error": decided_error}), 409

            step = workflow_repo.update_status(
                step_id,
                status="rejected",
                completed_at=datetime.now(UTC),
                error_message=reason,
            )

            if not step:
                return jsonify({"error": "Workflow step not found"}), 404

            db.commit()

            # Buyer registered push_notification_config on create_media_buy:
            # send the terminal task-status webhook (spec: rejected).
            mappings = workflow_repo.get_mappings_for_step(step_id)
            mapping = next((m for m in mappings if m.object_type == "media_buy"), None)
            if mapping:
                media_buy_repo = MediaBuyRepository(db, tenant_id)
                media_buy = media_buy_repo.get_by_id(mapping.object_id)
                if media_buy:
                    # Mirror operations.approve_media_buy(action="reject"): the buy
                    # itself must leave pending_approval, not just the step.
                    if media_buy.status == "pending_approval":
                        media_buy_repo.update_status(media_buy.media_buy_id, "rejected")
                        db.commit()
                    _notify_media_buy_decision(step, tenant_id, media_buy, media_buy_repo, status="rejected")

            flash("Workflow step rejected", "info")
            return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"Error rejecting workflow step {step_id}: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
