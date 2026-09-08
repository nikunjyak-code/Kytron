"""
================================================================================
 KYTRON AI CONSULTANT — SECURITY BOUNDARY (ai_security.py)
================================================================================
Every rule in Section 8/9 of the spec is enforced HERE, not in the model's
behavior. This is the ONLY file that touches Project-A's business models
(Project, Customer, catalogue), and every function in it is a read. There
is no code path from this file into a write — grep for
db.session.add/delete against a Project-A model here and you will find
none.

The AI never decides authorization. Every tool in ai_tools.py that
touches project data calls a function here, and every one of those
functions either (a) requires an already-verified conversation
(verified_order_id set via verify_project() below) or (b) requires an
already-authenticated Customer object passed in by ai_routes.py from
Project A's own session — never a customer_id string typed by the user
or suggested by the model.
================================================================================
"""
from app import (
    db,
    Project,
    Customer,
    ProjectTypeModel,
    CategoryModel,
    ServiceModel,
    FeatureModel,
    STATUS_LABELS,
    is_valid_email,
    serialize_project_summary,
)

MAX_FAILED_LOOKUP_ATTEMPTS = 5

STATUS_EXPLANATIONS = {
    "Registered": "Your project has been received and is queued for review.",
    "Under Process": "Our team is actively working on your project.",
    "Testing Phase": "Development is done and the project is being tested before delivery.",
    "On Revision": "Changes you requested are being applied.",
    "Ready for Delivery": "Your project is finished and ready to be handed over.",
    "Delivered": "Your project has been delivered. Final close-out steps may still be pending.",
    "Completed": "Your project is fully complete and closed out.",
    "Cancelled": "This project was cancelled.",
}


class LookupBlocked(Exception):
    """Raised when a conversation has exceeded MAX_FAILED_LOOKUP_ATTEMPTS.
    A secondary, UX-level guard — the real brute-force defense is the
    IP-scoped rate limit applied in ai_routes.py using Project A's own
    "order_login" scope (is_rate_limited/record_attempt), which can't be
    reset by simply starting a new conversation the way this per-row
    counter can."""


# ==============================================================================
# PROJECT VERIFICATION / ACCESS — the security-critical surface
# ==============================================================================
def verify_project(order_id, email, conversation):
    """Anonymous-visitor path: order_id + registered project email,
    matched against Project A's OWN authoritative relationship
    (project.customer.email) — not a second, independently-derived
    check. Scopes conversation.verified_order_id on success so later
    turns may reference only this one project."""
    if conversation.failed_lookup_attempts >= MAX_FAILED_LOOKUP_ATTEMPTS:
        raise LookupBlocked()

    order_id = (order_id or "").strip().upper()
    email = (email or "").strip().lower()
    if not order_id or not is_valid_email(email):
        return None, "Enter a valid Order ID and email."

    project = Project.query.filter_by(order_id=order_id).first()
    if not project or not project.customer or project.customer.email != email:
        conversation.failed_lookup_attempts += 1
        db.session.commit()
        if conversation.failed_lookup_attempts >= MAX_FAILED_LOOKUP_ATTEMPTS:
            raise LookupBlocked()
        return None, "We couldn't match that Order ID with the email provided."

    conversation.verified_order_id = project.order_id
    conversation.failed_lookup_attempts = 0
    db.session.commit()
    return serialize_project_summary(project), None


def my_projects(customer):
    """Logged-in path. `customer` must already be an authenticated
    Customer object supplied by ai_routes.py from Project A's own
    session — this function never authenticates anyone itself, and never
    accepts a bare customer_id string."""
    if not customer:
        return []
    projects = customer.projects.order_by(Project.created_at.desc()).all()
    return [serialize_project_summary(p) for p in projects]


def get_scoped_project(conversation, customer):
    """The single project (if any) this conversation may reference.
    Hard-scoped to the authenticated customer's own rows even if
    verified_order_id somehow referenced something else."""
    order_id = conversation.verified_order_id
    if not order_id:
        return None
    query = Project.query.filter_by(order_id=order_id)
    if customer:
        query = query.filter_by(customer_id=customer.id)
    project = query.first()
    return serialize_project_summary(project) if project else None


def project_details(conversation, customer, order_id=None):
    """Slightly richer than get_scoped_project for the get_project_details
    tool — still only ever the display-safe serializer, still hard-scoped
    to what this conversation/customer is actually allowed to see. If
    `order_id` is supplied by the model (from a tool call), it is IGNORED
    unless it matches what this conversation/customer already has access
    to — the model cannot widen its own access by naming a different
    order_id in a tool call."""
    if customer:
        projects = my_projects(customer)
        if order_id:
            match = next((p for p in projects if p["order_id"] == order_id.strip().upper()), None)
            return match
        if conversation.verified_order_id:
            return next((p for p in projects if p["order_id"] == conversation.verified_order_id), None)
        return projects[0] if len(projects) == 1 else None
    return get_scoped_project(conversation, customer=None)


def explain_status(status):
    return STATUS_EXPLANATIONS.get(status, "Status details aren't available for this project yet.")


def next_action_guidance(project_summary):
    if not project_summary:
        return None
    if project_summary.get("has_pending_action"):
        return "There's a pending action on this project — check the Client Portal for an approval or request waiting on you."
    status = project_summary.get("status")
    if status in ("Registered", "Under Process", "Testing Phase", "On Revision"):
        return "Nothing is waiting on you right now — our team is actively working on it."
    if status in ("Ready for Delivery", "Delivered"):
        return "Check the Client Portal for delivery/handover details."
    if status == "Completed":
        return "This project is complete."
    return None


# ==============================================================================
# CATALOGUE — public, no authorization needed
# ==============================================================================
def catalog_snapshot():
    return {
        "project_types": [
            {"id": t.id, "name": t.name, "description": t.description}
            for t in ProjectTypeModel.query.filter_by(is_active=True).order_by(ProjectTypeModel.sort_order).all()
        ],
        "services": [
            {"id": s.id, "name": s.name, "description": s.description}
            for s in ServiceModel.query.filter_by(is_active=True).order_by(ServiceModel.sort_order).all()
        ],
        "categories": [
            {"id": c.id, "name": c.name}
            for c in CategoryModel.query.filter_by(is_active=True).order_by(CategoryModel.sort_order).all()
        ],
    }


def features_for_project_type(project_type_text, limit=10):
    if not project_type_text:
        return []
    text = project_type_text.lower()
    matched_id = None
    for t in ProjectTypeModel.query.all():
        if t.id.lower() in text or t.name.lower() in text:
            matched_id = t.id
            break
    if not matched_id:
        return []
    names = [
        f.name for f in FeatureModel.query.filter_by(project_type_id=matched_id, is_active=True)
        .order_by(FeatureModel.sort_order).all()
    ]
    return names[:limit]


def workflow_stages():
    return list(STATUS_LABELS.values())
