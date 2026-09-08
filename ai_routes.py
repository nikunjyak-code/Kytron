"""
================================================================================
 KYTRON AI CONSULTANT — ROUTES (ai_routes.py)
================================================================================
Two blueprints: ai_consultant_public_bp (/api/consultant/*) and
ai_consultant_admin_bp (/api/admin/consultant/*). Thin by design — parse,
validate, enforce ownership/authorization, hand off to ai_consultant.py.
Every security control established and verified across this Consultant's
prior hardening passes is carried over unchanged in substance, only
renamed to match the new file layout:

  - ended conversations are never reused (a fresh one starts instead)
  - option submissions are validated against a freshly-built server-side
    tree — client-supplied question/option ids are never trusted
  - project verification reuses Project A's own "order_login" rate-limit
    scope (IP-keyed), not a new or weaker mechanism
  - a blueprint-level error handler rolls back the session and never
    leaks an internal exception to the customer
  - request bodies are capped well below the app's global upload-sized
    MAX_CONTENT_LENGTH, specific to these small JSON endpoints
================================================================================
"""
import secrets
import logging
from datetime import datetime, timedelta

from flask import Blueprint, request, session, jsonify

from app import db, json_error, admin_required, current_customer, is_rate_limited, record_attempt
import ai_consultant
import ai_options
import ai_security
import ai_usage
from ai_models import AIConversation, AIMessage, get_ai_setting, set_ai_setting

logger = logging.getLogger("kytron.ai_consultant")

ai_consultant_public_bp = Blueprint("ai_consultant_public", __name__, url_prefix="/api/consultant")
ai_consultant_admin_bp = Blueprint("ai_consultant_admin", __name__, url_prefix="/api/admin/consultant")


def _handle_unexpected_error(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        raise exc
    try:
        db.session.rollback()
    except Exception:
        pass
    logger.exception("Unhandled error in AI Consultant route: %s", exc)
    return json_error("Something went wrong on our end. Please try again in a moment.", 500)


ai_consultant_public_bp.register_error_handler(Exception, _handle_unexpected_error)
ai_consultant_admin_bp.register_error_handler(Exception, _handle_unexpected_error)

MAX_REQUEST_BYTES = 16 * 1024


@ai_consultant_public_bp.before_request
def _reject_oversized_requests():
    if request.content_length and request.content_length > MAX_REQUEST_BYTES:
        return json_error("Request too large.", 413)


@ai_consultant_admin_bp.before_request
def _reject_oversized_admin_requests():
    if request.content_length and request.content_length > MAX_REQUEST_BYTES:
        return json_error("Request too large.", 413)


MESSAGE_MAX_CHARS = 4000
CHAT_RATE_LIMIT_WINDOW_SECONDS = 60
CHAT_RATE_LIMIT_MAX_MESSAGES = 20


def _visitor_token():
    token = session.get("ai_consultant_visitor_token")
    if not token:
        token = secrets.token_hex(16)
        session["ai_consultant_visitor_token"] = token
    return token


def _owns_conversation(conversation):
    if conversation is None:
        return False
    token = session.get("ai_consultant_visitor_token")
    if token and conversation.visitor_token == token:
        return True
    customer = current_customer()
    if customer and conversation.customer_id == customer.customer_id:
        return True
    return False


def _chat_rate_limited(visitor_token, customer_id):
    window_start = datetime.utcnow() - timedelta(seconds=CHAT_RATE_LIMIT_WINDOW_SECONDS)
    query = AIMessage.query.join(AIConversation).filter(
        AIMessage.role == "user", AIMessage.created_at >= window_start,
    )
    if customer_id:
        query = query.filter(AIConversation.customer_id == customer_id)
    else:
        query = query.filter(AIConversation.visitor_token == visitor_token)
    return query.count() >= CHAT_RATE_LIMIT_MAX_MESSAGES


def _resolve_conversation(token, customer, conversation_uid):
    conversation = None
    if conversation_uid:
        candidate = AIConversation.query.filter_by(conversation_uid=conversation_uid).first()
        if _owns_conversation(candidate) and candidate.status != "ended":
            conversation = candidate
    if conversation is None:
        conversation = ai_consultant.start_conversation(
            visitor_token=token, customer_id=customer.customer_id if customer else None,
            channel="client" if customer else "visitor",
        )
    return conversation


# ==============================================================================
# PUBLIC
# ==============================================================================
@ai_consultant_public_bp.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()
    if not text:
        return json_error("Message cannot be empty.", 400)
    if len(text) > MESSAGE_MAX_CHARS:
        return json_error("Message is too long.", 400)

    token = _visitor_token()
    customer = current_customer()
    if _chat_rate_limited(token, customer.customer_id if customer else None):
        return json_error("Too many messages. Please wait a moment and try again.", 429)

    conversation = _resolve_conversation(token, customer, data.get("conversation_uid"))
    result = ai_consultant.handle_free_text(conversation, text, customer=customer)
    return jsonify(result)


@ai_consultant_public_bp.route("/options-tree", methods=["GET"])
def options_tree():
    return jsonify(ai_options.build_tree())


@ai_consultant_public_bp.route("/option", methods=["POST"])
def option():
    data = request.get_json(silent=True) or {}
    question_id = data.get("question_id")
    selected_option_ids = data.get("selected_option_ids") or []
    free_text = (data.get("free_text") or "").strip()

    question = ai_options.get_question(question_id)
    if not question:
        return json_error("Unknown question.", 400)
    if not isinstance(selected_option_ids, list):
        return json_error("selected_option_ids must be a list.", 400)

    valid_ids = {o["id"] for o in question["options"]}
    if any(oid not in valid_ids for oid in selected_option_ids):
        return json_error("Invalid option selected.", 400)
    if question["type"] == "single_select" and len(selected_option_ids) > 1:
        return json_error("Only one option can be selected for this question.", 400)

    min_select = question.get("min_select", 1)
    if len(selected_option_ids) < min_select and not free_text:
        return json_error("Please select at least one option.", 400)
    if len(free_text) > MESSAGE_MAX_CHARS:
        return json_error("That's too long.", 400)

    token = _visitor_token()
    customer = current_customer()
    if _chat_rate_limited(token, customer.customer_id if customer else None):
        return json_error("Too many messages. Please wait a moment and try again.", 429)

    conversation = _resolve_conversation(token, customer, data.get("conversation_uid"))
    result = ai_consultant.handle_option_submission(conversation, question, selected_option_ids, free_text)

    next_question_id = question.get("next")
    if question["type"] == "single_select" and selected_option_ids:
        chosen = next((o for o in question["options"] if o["id"] == selected_option_ids[0]), None)
        if chosen:
            next_question_id = chosen.get("next")
    result["next_question_id"] = next_question_id
    return jsonify(result)


@ai_consultant_public_bp.route("/history", methods=["GET"])
def history():
    conversation = AIConversation.query.filter_by(conversation_uid=request.args.get("conversation_uid")).first()
    if not _owns_conversation(conversation):
        return json_error("Conversation not found.", 404)
    return jsonify({"conversation_uid": conversation.conversation_uid, "messages": ai_consultant.get_history(conversation)})


@ai_consultant_public_bp.route("/verify-project", methods=["POST"])
def verify_project():
    data = request.get_json(silent=True) or {}
    conversation = AIConversation.query.filter_by(conversation_uid=data.get("conversation_uid")).first()
    if not _owns_conversation(conversation):
        return json_error("Conversation not found.", 404)

    order_id = (data.get("order_id") or "").strip()
    email = (data.get("email") or "").strip()
    if not order_id or not email:
        return json_error("Order ID and email are both required.", 400)

    rate_key = request.remote_addr or "unknown"
    if is_rate_limited(rate_key, "order_login"):
        return json_error("Too many attempts. Please try again later or use the Contact page.", 429)

    summary, error = ai_security.verify_project(order_id, email, conversation)
    record_attempt(rate_key, "order_login", success=bool(conversation.verified_order_id))
    if error:
        return jsonify({"verified": False, "error": error})
    return jsonify({"verified": True, "project": summary, "verified_order_id": conversation.verified_order_id})


@ai_consultant_public_bp.route("/end", methods=["POST"])
def end():
    data = request.get_json(silent=True) or {}
    conversation = AIConversation.query.filter_by(conversation_uid=data.get("conversation_uid")).first()
    if not _owns_conversation(conversation):
        return json_error("Conversation not found.", 404)
    ai_consultant.end_conversation(conversation)
    return jsonify({"message": "Conversation ended."})


# ==============================================================================
# ADMIN
# ==============================================================================
@ai_consultant_admin_bp.route("/status", methods=["GET"])
@admin_required
def admin_status():
    return jsonify({
        "consultant_enabled": get_ai_setting("consultant_enabled", "true") == "true",
        "provider": ai_usage.availability_state(),
        "conversations_total": AIConversation.query.count(),
        "conversations_active": AIConversation.query.filter_by(status="active").count(),
        "conversations_escalated": AIConversation.query.filter_by(escalated=True).count(),
    })


@ai_consultant_admin_bp.route("/conversations", methods=["GET"])
@admin_required
def admin_list_conversations():
    search = (request.args.get("search") or "").strip()
    query = AIConversation.query
    if search:
        like = f"%{search}%"
        query = query.filter(db.or_(
            AIConversation.conversation_uid.ilike(like),
            AIConversation.customer_id.ilike(like),
            AIConversation.verified_order_id.ilike(like),
        ))
    conversations = query.order_by(AIConversation.created_at.desc()).limit(200).all()
    return jsonify([{
        "conversation_uid": c.conversation_uid, "title": c.title, "channel": c.channel, "status": c.status,
        "customer_id": c.customer_id, "verified_order_id": c.verified_order_id, "escalated": c.escalated,
        "message_count": c.messages.count(), "started_at": c.created_at.isoformat(),
    } for c in conversations])


@ai_consultant_admin_bp.route("/conversations/<conversation_uid>", methods=["GET"])
@admin_required
def admin_get_conversation(conversation_uid):
    conversation = AIConversation.query.filter_by(conversation_uid=conversation_uid).first()
    if not conversation:
        return json_error("Conversation not found.", 404)
    return jsonify({
        "conversation_uid": conversation.conversation_uid, "title": conversation.title,
        "verified_order_id": conversation.verified_order_id, "escalated": conversation.escalated,
        "escalation_reason": conversation.escalation_reason, "facts": conversation.get_facts(),
        "messages": ai_consultant.get_history(conversation),
    })


@ai_consultant_admin_bp.route("/settings", methods=["GET"])
@admin_required
def admin_get_settings():
    return jsonify({"consultant_enabled": get_ai_setting("consultant_enabled", "true") == "true"})


@ai_consultant_admin_bp.route("/settings", methods=["PATCH"])
@admin_required
def admin_update_settings():
    data = request.get_json(silent=True) or {}
    if "consultant_enabled" in data:
        set_ai_setting("consultant_enabled", "true" if data["consultant_enabled"] else "false")
    return jsonify({"message": "Consultant settings saved."})
