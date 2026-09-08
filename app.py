"""
================================================================================
 KYTRON CLIENT PORTAL — BACKEND (app.py)
================================================================================
Single-file, production-ready Flask backend for the two attached frontends:

    client.html            -> Client Portal (register / submit project / find)
    project_details.html   -> Order Login + Project Dashboard

Everything lives in this one file, organized into clearly labeled sections.
The database (kytron.db) is created and self-migrated automatically on every
startup — new tables / new columns are added without ever touching existing
data. No manual SQL, no external migration framework.

Run:
    pip install flask flask-sqlalchemy werkzeug
    python app.py
================================================================================
"""

# ==============================================================================
# SECTION 1 — IMPORTS
# ==============================================================================
import os
import re
import json
from dotenv import load_dotenv
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Resolved relative to THIS file's actual location, never the current
# working directory — so `python app.py` behaves identically regardless
# of which directory it's launched from.
#
# override=False (python-dotenv's own default, made explicit here rather
# than relied on implicitly) means .env only ever FILLS IN variables not
# already present in the process environment — a deployment platform that
# injects GROQ_API_KEY/OPENROUTER_API_KEY/GEMINI_API_KEY (or anything
# else) directly continues to take precedence over .env, never the
# reverse. No credentials are ever created, faked, or hard-coded here;
# this only decides which SOURCE a name is read from.
#
# The actual diagnostic for whether this succeeded is logged further
# below (see "STARTUP DIAGNOSTIC"), once logging is configured — logging
# a message THIS early would silently go nowhere, since Python's root
# logger has no handler until logging.basicConfig() runs later in this
# file, and a missing/misplaced .env is exactly the kind of failure that
# must not fail silently (it previously caused every AI provider to
# report "unconfigured" with no visible cause anywhere).
_ENV_FILE_PATH = os.path.join(BASE_DIR, ".env")
_ENV_FILE_LOADED = load_dotenv(_ENV_FILE_PATH, override=False)

import time
import string
import secrets
import sqlite3
import logging
import mimetypes

import sys
sys.modules["app"] = sys.modules[__name__]

from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps

from flask import Flask, request, jsonify, session, send_from_directory, g, Response
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import inspect as sa_inspect, text
from flask_sqlalchemy import SQLAlchemy

# ==============================================================================
# SECTION 2 — APP CONFIGURATION
# ==============================================================================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
SECRET_KEY_FILE = os.path.join(INSTANCE_DIR, "secret.key")

os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.join(UPLOAD_DIR, "projects"), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_DIR, "revisions"), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_DIR, "requirements"), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_DIR, "account_photos"), exist_ok=True)


def _load_or_create_secret_key():
    """Persist a random secret key across restarts instead of hard-coding one."""
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "r") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w") as f:
        f.write(key)
    return key


# In production, set KYTRON_HTTPS_ONLY=1 once the portal is served over HTTPS
# so session cookies get the Secure flag. Left off by default for local dev.
HTTPS_ONLY = os.environ.get("KYTRON_HTTPS_ONLY", "0") == "1"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("KYTRON_ALLOWED_ORIGINS", "").split(",") if o.strip()]

app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_or_create_secret_key(),
    SQLALCHEMY_DATABASE_URI="sqlite:///" + os.path.join(INSTANCE_DIR, "kytron.db"),
    SQLALCHEMY_BINDS={"ai_consultant": "sqlite:///" + os.path.join(INSTANCE_DIR, "ai_consultant.db")},
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=HTTPS_ONLY,
    PERMANENT_SESSION_LIFETIME=timedelta(days=5),  # "Remember Me" — 5 day sessions
    MAX_CONTENT_LENGTH=60 * 1024 * 1024,  # hard ceiling above the 50MB per-file rule
    JSON_SORT_KEYS=False,
)

db = SQLAlchemy(app)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger("kytron")

# STARTUP DIAGNOSTIC — never logs a secret value, only whether the file
# was found and its path. Deliberately emitted at INFO level (not DEBUG)
# so it's visible in a normal production log without needing verbose
# logging turned on — the whole point is that "all three AI providers
# report unconfigured" should never again be a silent, undiagnosable
# startup state.
if _ENV_FILE_LOADED:
    logger.info(".env loaded: yes (%s)", _ENV_FILE_PATH)
else:
    logger.info(
        ".env not found at expected application path (%s); relying on the process environment for any "
        "configuration normally supplied via .env (e.g. GROQ_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY)",
        _ENV_FILE_PATH,
    )

ALLOWED_UPLOAD_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf",
    "png", "jpg", "jpeg", "gif", "webp", "svg",
    "zip", "rar", "7z",
    "psd", "ai", "fig", "sketch",
    "json", "xml",
    "mp4", "mov", "mp3", "wav",
}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB per file, per spec


# ==============================================================================
# SECTION 3 — DATABASE MODELS
# ==============================================================================
def _now():
    return datetime.utcnow()


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.String(20), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False, index=True)
    country_code = db.Column(db.String(6), default="+91")
    phone = db.Column(db.String(40))
    company = db.Column(db.String(200))
    # Single project-update notification channel (email). True = customer has
    # opted in to receive project-lifecycle emails (submission confirmation,
    # approval, rejection, revision activity, etc). This is a customer-level
    # preference, independent of mandatory account/security emails, which
    # always send regardless of this flag. Existing rows migrate to True
    # (see run_self_migration) to preserve pre-refactor behavior, since every
    # project-update email used to send unconditionally.
    project_update_email = db.Column(db.Boolean, nullable=False, default=True)

    password_hash = db.Column(db.String(255), nullable=False)
    must_change_password = db.Column(db.Boolean, default=True, nullable=False)

    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    lockout_until = db.Column(db.DateTime)
    account_disabled = db.Column(db.Boolean, nullable=False, default=False)

    # --- My Account additions (profile / business / verification / prefs) ---
    display_name = db.Column(db.String(200))
    photo_path = db.Column(db.String(500))

    business_website = db.Column(db.String(300))
    business_type = db.Column(db.String(100))
    address = db.Column(db.String(300))
    city = db.Column(db.String(120))
    state = db.Column(db.String(120))
    pin_code = db.Column(db.String(12))
    gstin = db.Column(db.String(20))

    # Registration already confirms email ownership (it's the login
    # credential), so existing + new rows default True; phone verification
    # is a distinct, not-yet-exercised flow and defaults False.
    email_verified = db.Column(db.Boolean, nullable=False, default=True)
    phone_verified = db.Column(db.Boolean, nullable=False, default=False)
    phone_otp_hash = db.Column(db.String(255))
    phone_otp_expires_at = db.Column(db.DateTime)
    phone_otp_attempts = db.Column(db.Integer, default=0, nullable=False)

    pref_language = db.Column(db.String(20), default="en")
    pref_country = db.Column(db.String(20), default="IN")
    pref_currency = db.Column(db.String(10), default="INR")
    pref_appearance = db.Column(db.String(20), default="dark")
    notif_marketing_email = db.Column(db.Boolean, nullable=False, default=False)
    notif_inapp_enabled = db.Column(db.Boolean, nullable=False, default=True)

    # Bumped by "sign out everywhere"; every session carries the version it
    # was issued under, so login_required rejects stale sessions without a
    # second auth system or a server-side session store.
    session_version = db.Column(db.Integer, nullable=False, default=1)

    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)

    projects = db.relationship("Project", backref="customer", lazy="dynamic")

    def to_public_dict(self):
        return {
            "customer_id": self.customer_id,
            "name": self.full_name,
            "email": self.email,
            "country_code": self.country_code or "+91",
            "phone": self.phone,
            "company": self.company,
            "must_change_password": self.must_change_password,
        }

    def to_account_dict(self):
        return {
            "customer_id": self.customer_id,
            "name": self.full_name,
            "display_name": self.display_name or self.full_name,
            "email": self.email,
            "country_code": self.country_code or "+91",
            "phone": self.phone,
            "company": self.company,
            "photo_url": f"/api/client/account/photo/{self.customer_id}" if self.photo_path else None,
            "business": {
                "company": self.company,
                "website": self.business_website,
                "business_type": self.business_type,
                "address": self.address,
                "city": self.city,
                "state": self.state,
                "pin_code": self.pin_code,
                "gstin": self.gstin,
            },
        }


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)

    project_name = db.Column(db.String(200))
    project_type = db.Column(db.String(50))
    service = db.Column(db.String(50))
    categories = db.Column(db.Text, default="[]")          # JSON list of category ids
    requested_features = db.Column(db.Text, default="[]")   # JSON list — client requested
    approved_features = db.Column(db.Text, default="[]")    # JSON list — admin approved

    budget_range = db.Column(db.String(50))          # client's estimated / requested budget
    preferred_deadline = db.Column(db.String(50))     # client's requested deadline
    final_deadline = db.Column(db.String(50))          # admin approved deadline

    description = db.Column(db.Text)
    admin_notes = db.Column(db.Text)

    status = db.Column(db.String(30), default="Registered", nullable=False)
    is_approved = db.Column(db.Boolean, default=False, nullable=False)

    support_duration_label = db.Column(db.String(50))
    support_expiry = db.Column(db.String(50))
    support_status = db.Column(db.String(20), default="not_started")

    revision_total = db.Column(db.Integer, default=2, nullable=False)
    revision_window_label = db.Column(db.String(50))

    # --- Admin Panel additions: structured deadline tracking + priority ---
    deadline_date = db.Column(db.Date)          # parsed date backing the deadline badge/bucket system
    priority = db.Column(db.String(20), default="normal", nullable=False)  # low | normal | high | urgent

    # --- Phase 7: delivery + handover. Status itself stays the existing
    # free-string column (extended additively with 'Ready for Delivery' and
    # 'Delivered' — see STATUS_BADGE in the frontends); these fields record
    # the delivery EVENT itself, kept distinct from 'Completed' per the
    # delivered-vs-completed boundary (7S). handover_notes is plain
    # operational info (URL, domain/hosting pointers) — deliberately no
    # credential/secret field exists here or anywhere else in this model;
    # this app has no secrets-vault infrastructure, so one hasn't been
    # invented rather than storing passwords in plaintext.
    delivered_at = db.Column(db.DateTime)
    delivered_by = db.Column(db.String(200))
    delivery_notes = db.Column(db.Text)
    handover_url = db.Column(db.String(500))
    handover_notes = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)

    payment = db.relationship("Payment", backref="project", uselist=False, lazy="joined")
    documents = db.relationship("Document", backref="project", lazy="dynamic")
    revisions = db.relationship("Revision", backref="project", lazy="dynamic")
    timeline_events = db.relationship("TimelineEvent", backref="project", lazy="dynamic")
    milestones = db.relationship("Milestone", backref="project", lazy="dynamic")
    tasks = db.relationship("Task", backref="project", lazy="dynamic")
    requirements = db.relationship("Requirement", backref="project", lazy="dynamic")
    approvals = db.relationship("Approval", backref="project", lazy="dynamic")
    payment_transactions = db.relationship("PaymentTransaction", backref="project", lazy="dynamic")
    conversation = db.relationship("Conversation", backref="project", uselist=False, lazy="joined")

    def categories_list(self):
        try:
            return json.loads(self.categories or "[]")
        except (TypeError, ValueError):
            return []

    def requested_features_list(self):
        try:
            return json.loads(self.requested_features or "[]")
        except (TypeError, ValueError):
            return []

    def approved_features_list(self):
        try:
            return json.loads(self.approved_features or "[]")
        except (TypeError, ValueError):
            return []


STATUS_LABELS = {
    "Registered": "Registered",
    "Under Process": "Under Process",
    "Testing Phase": "Testing Phase",
    "On Revision": "On Revision",
    # Added in Phase 7 to close the gap between "being worked on" and
    # "Completed" — previously those were the same status, which meant
    # "delivered" and "fully closed out" (final payment, handover done)
    # were indistinguishable (7S). Existing statuses above are unchanged;
    # this is a pure additive extension of the same free-string column,
    # not a lifecycle replacement.
    "Ready for Delivery": "Ready for Delivery",
    "Delivered": "Delivered",
    "Completed": "Completed",
    "Cancelled": "Cancelled",
}


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), unique=True, nullable=False)

    estimated_budget = db.Column(db.Float, default=0.0)
    final_cost = db.Column(db.Float, default=0.0)
    advance_percentage = db.Column(db.Float, default=0.0)
    advance_amount = db.Column(db.Float, default=0.0)
    advance_paid = db.Column(db.Float, default=0.0)
    remaining_amount = db.Column(db.Float, default=0.0)
    extra_charges = db.Column(db.Float, default=0.0)
    addon_charges = db.Column(db.Float, default=0.0)
    revision_charges = db.Column(db.Float, default=0.0)

    status = db.Column(db.String(20), default="pending", nullable=False)  # pending/partial/paid/overdue
    # Kytron's market is India-only today: new rows default to real INR
    # values. currency_code is the source of truth for logic; currency_symbol
    # is a display convenience derived from it. Existing rows created before
    # this column existed still say "$" — see backfill_payment_currency()
    # for the one-time, additive correction (never touches amounts).
    currency_code = db.Column(db.String(3), default="INR", nullable=False)
    currency_symbol = db.Column(db.String(5), default="₹")

    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)


PAYMENT_STATUS_LABELS = {"pending": "Pending", "partial": "Partial", "paid": "Paid", "overdue": "Overdue"}


# ==============================================================================
# SECTION 3AA — PAYMENT TRANSACTIONS (the actual money-movement ledger — kept
# deliberately separate from Payment, which holds the agreed FINANCIAL TERMS
# for a project, not a record of individual payments received. A Payment row
# is configuration ("the project costs ₹50,000, 40% advance"); a
# PaymentTransaction row is a fact ("₹20,000 arrived via UPI on this date,
# reference XYZ, verified by this admin"). Money on this ledger is stored in
# integer minor units (paise) specifically because this is the one place
# amounts get repeatedly summed — the safest representation for that. The
# Payment model's own Float columns are left as-is: they're single
# admin-set configuration values, not compounding sums, so migrating them
# to minor units would add risk (touching every existing consumer in
# Admin.html/Client.html/Project_details.html) without a matching safety
# benefit. See recompute_payment_balance() / sync_payment_advance_from_
# transactions() for how the two models are kept consistent.
# ==============================================================================
class PaymentTransaction(db.Model):
    __tablename__ = "payment_transactions"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)

    amount_minor = db.Column(db.Integer, nullable=False)  # paise
    currency_code = db.Column(db.String(3), default="INR", nullable=False)

    method = db.Column(db.String(20), nullable=False)  # upi/bank_transfer/cash/card/other
    reference = db.Column(db.String(120))  # UTR / bank reference, customer-supplied, never required
    payment_date = db.Column(db.Date)  # date the customer says the payment was made
    note = db.Column(db.Text)

    proof_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)  # which invoice this transaction pays off, if any (Phase 8)

    status = db.Column(db.String(20), default="submitted", nullable=False)  # submitted/verified/rejected
    submitted_by = db.Column(db.String(20), default="customer", nullable=False)  # 'customer' | 'admin'
    submitted_at = db.Column(db.DateTime, default=_now, nullable=False)

    reviewed_by = db.Column(db.String(200))  # admin full_name
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.Text)  # required on rejection, optional on verification

    receipt_number = db.Column(db.String(30), unique=True)  # assigned only once verified

    proof_document = db.relationship("Document")
    invoice = db.relationship("Invoice", backref=db.backref("transactions", lazy="dynamic"))


PAYMENT_TXN_STATUS_LABELS = {"submitted": "Submitted", "verified": "Verified", "rejected": "Rejected"}
PAYMENT_TXN_METHOD_LABELS = {"upi": "UPI", "bank_transfer": "Bank Transfer", "cash": "Cash", "card": "Card", "other": "Other"}


def to_minor_units(rupees):
    """Rupees (any numeric/str input) -> integer paise. The single
    conversion point into the ledger, so transaction amounts are never
    summed as float anywhere."""
    d = Decimal(str(rupees)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(d * 100)


def from_minor_units(paise):
    return round((paise or 0) / 100.0, 2)


def format_inr(amount):
    """Server-side Indian digit-grouping formatter: 125000 -> '₹1,25,000'.
    Used anywhere money is rendered outside the browser (emails, timeline
    text, receipts) so formatting can't drift from the frontend's copy."""
    amount = round(float(amount or 0), 2)
    neg = amount < 0
    amount = abs(amount)
    whole = int(amount)
    frac = round(amount - whole, 2)
    s = str(whole)
    if len(s) > 3:
        last3, rest = s[-3:], s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        s = ",".join(parts) + "," + last3
    if frac:
        s += f".{int(round(frac * 100)):02d}"
    return ("-" if neg else "") + "₹" + s


def gen_receipt_number():
    while True:
        stamp = datetime.utcnow().strftime("%Y%m")
        candidate = f"KYT-RCP-{stamp}-{_random_digits(5)}"
        if not PaymentTransaction.query.filter_by(receipt_number=candidate).first():
            return candidate


def recompute_payment_balance(payment):
    """Single source of truth for Payment.status/remaining_amount. Total due
    always includes final_cost plus all three charge categories.
    Previously this formula was duplicated and INCONSISTENT between the
    accept-request flow (which omitted extra/addon/revision charges) and
    the admin payment PATCH route (which included them) — found during the
    Phase 3 audit and fixed by centralizing here; both call sites now use
    this function instead of repeating the arithmetic."""
    total_due = round((payment.final_cost or 0) + (payment.extra_charges or 0)
                       + (payment.addon_charges or 0) + (payment.revision_charges or 0), 2)
    payment.remaining_amount = round(total_due - (payment.advance_paid or 0), 2)
    if total_due > 0 and (payment.advance_paid or 0) >= total_due:
        payment.status = "paid"
    elif (payment.advance_paid or 0) > 0:
        payment.status = "partial"
    else:
        payment.status = "pending"


def sync_payment_advance_from_transactions(project):
    """Once a project has at least one PaymentTransaction, advance_paid
    becomes DERIVED — the sum of verified transactions — rather than a
    number admin edits by hand. Projects with zero transactions (all
    pre-Phase-3 projects) keep working exactly as before: advance_paid
    stays whatever admin last set via the legacy PATCH route, untouched.
    This is the backward-compatibility boundary: nothing about existing
    financial data changes unless/until a real transaction is recorded
    against that project."""
    payment = project.payment
    if not payment:
        return
    has_transactions = project.payment_transactions.count() > 0
    if has_transactions:
        verified_minor = db.session.query(
            db.func.coalesce(db.func.sum(PaymentTransaction.amount_minor), 0)
        ).filter_by(project_id=project.id, status="verified").scalar()
        payment.advance_paid = from_minor_units(verified_minor)
    recompute_payment_balance(payment)


# ==============================================================================
# Phase 7: execution/delivery helpers. All factual, computed from real rows
# — no scores, no invented business rules. Delivery readiness never hard-
# blocks the "mark delivered" action on unpaid balance (7P: "do not
# automatically block delivery for every unpaid amount unless existing
# commercial terms require it" — no such rule exists in this codebase), so
# the remaining balance is surfaced as information, not counted as a
# blocker.
# ==============================================================================
def compute_delivery_readiness(project):
    blockers = []

    open_tasks = [t for t in project.tasks if t.status != "done"]
    if open_tasks:
        blockers.append(f"{len(open_tasks)} task{'s' if len(open_tasks) != 1 else ''} not marked done")

    open_milestones = [m for m in project.milestones if m.status != "completed"]
    if open_milestones:
        blockers.append(f"{len(open_milestones)} milestone{'s' if len(open_milestones) != 1 else ''} not completed")

    pending_approvals = [a for a in project.approvals if a.status == "pending"]
    if pending_approvals:
        blockers.append(f"{len(pending_approvals)} approval{'s' if len(pending_approvals) != 1 else ''} awaiting the customer")

    open_requirements = [r for r in project.requirements if r.status not in ("accepted", "not_applicable")]
    if open_requirements:
        blockers.append(f"{len(open_requirements)} requirement{'s' if len(open_requirements) != 1 else ''} not yet accepted")

    open_change_requests = ChangeRequest.query.filter(
        ChangeRequest.project_id == project.id,
        ChangeRequest.status.in_(["submitted", "under_review", "proposal_required"]),
    ).all()
    if open_change_requests:
        blockers.append(f"{len(open_change_requests)} change request{'s' if len(open_change_requests) != 1 else ''} unresolved")

    remaining = project.payment.remaining_amount if project.payment else 0
    return {
        "ready": len(blockers) == 0,
        "blockers": blockers,
        "payment_remaining": remaining or 0,
        "payment_remaining_formatted": format_inr(remaining or 0) if remaining else None,
    }


def compute_customer_next_action(project):
    """First real pending item, in the order a customer would actually
    care about — proposal decision first (nothing else can proceed
    commercially until that's resolved), then approvals, then what they
    need to provide, then payment. Returns None when nothing is pending."""
    latest_proposals = {}
    for p in Proposal.query.filter_by(project_id=project.id).order_by(Proposal.version.desc()).all():
        latest_proposals.setdefault(p.proposal_number, p)
    for p in latest_proposals.values():
        if p.status in ("sent", "viewed"):
            return f"Review the proposal: {p.title}"

    for a in project.approvals:
        if a.status == "pending":
            return f"Approve: {a.item_title}"

    for r in project.requirements:
        if r.status in ("pending", "revision_required"):
            return f"Provide: {r.title}"

    if project.payment and (project.payment.remaining_amount or 0) > 0 and project.payment.status != "paid":
        return f"Complete payment of {format_inr(project.payment.remaining_amount)}"

    # Support checked last — financial/commercial actions above always
    # take priority over a support reply (9AB explicit rule).
    waiting_support = SupportTicket.query.filter_by(project_id=project.id, status="waiting_for_customer").first()
    if waiting_support:
        return f"Reply to support request: {waiting_support.title or gen_support_ticket_ref(waiting_support)}"

    return None


def compute_commercial_summary(project):
    """The Phase-8N view: Quoted / Agreed / Invoiced / Verified received /
    Remaining — five numbers, five different authoritative sources, never
    fabricated or independently editable:
      quoted    = the most relevant Proposal's total (accepted, else latest sent/viewed)
      agreed    = Payment.final_cost, set only via the existing accept-request flow
      invoiced  = sum of ISSUED (non-draft, non-cancelled) Invoice amounts
      received  = Payment.advance_paid (already derived from verified transactions)
      remaining = Payment.remaining_amount (server-recomputed, never trusted from a client)
    No 'profit' or fabricated metric is derived here."""
    quoted = None
    accepted = Proposal.query.filter_by(project_id=project.id, status="accepted").order_by(Proposal.updated_at.desc()).first()
    if accepted:
        quoted = from_minor_units(accepted.total_minor)
    else:
        latest = Proposal.query.filter(
            Proposal.project_id == project.id, Proposal.status.in_(["sent", "viewed"])
        ).order_by(Proposal.updated_at.desc()).first()
        if latest:
            quoted = from_minor_units(latest.total_minor)

    invoiced_minor = db.session.query(
        db.func.coalesce(db.func.sum(Invoice.amount_minor), 0)
    ).filter(Invoice.project_id == project.id, Invoice.status == "issued").scalar()

    payment = project.payment
    return {
        "quoted": quoted, "quoted_formatted": format_inr(quoted) if quoted is not None else None,
        "agreed": payment.final_cost if payment else None,
        "agreed_formatted": format_inr(payment.final_cost) if payment and payment.final_cost else None,
        "invoiced": from_minor_units(invoiced_minor),
        "invoiced_formatted": format_inr(from_minor_units(invoiced_minor)) if invoiced_minor else None,
        "received": payment.advance_paid if payment else 0,
        "received_formatted": format_inr(payment.advance_paid) if payment and payment.advance_paid else None,
        "remaining": payment.remaining_amount if payment else None,
        "remaining_formatted": format_inr(payment.remaining_amount) if payment and payment.remaining_amount else None,
    }


# ==============================================================================
# SECTION 3AB — LEADS (pre-customer enquiries). A Lead represents a possible
# business relationship BEFORE any Project exists — deliberately not a
# Customer or Project record. Converting a lead only ever links/creates a
# Customer; it never auto-creates a Project, preserving the existing
# Project Request → Admin Review → Accept boundary untouched.
# ==============================================================================
class Lead(db.Model):
    __tablename__ = "leads"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.String(20), unique=True, nullable=False)

    name = db.Column(db.String(120), nullable=False)
    business_name = db.Column(db.String(150))
    email = db.Column(db.String(150), nullable=False, index=True)
    phone = db.Column(db.String(30), index=True)
    business_category = db.Column(db.String(80))

    source = db.Column(db.String(30), default="website", nullable=False)
    enquiry_type = db.Column(db.String(80))
    message = db.Column(db.Text, nullable=False)
    budget_range = db.Column(db.String(50))
    preferred_timeline = db.Column(db.String(50))
    preferred_contact_method = db.Column(db.String(20))

    status = db.Column(db.String(20), default="new", nullable=False)
    priority = db.Column(db.String(10), default="normal")

    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True)

    last_contacted_at = db.Column(db.DateTime)
    next_follow_up_at = db.Column(db.Date)
    converted_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)

    customer = db.relationship("Customer")
    project = db.relationship("Project")


class LeadNote(db.Model):
    """Business follow-up notes — deliberately separate from AuditLog.
    AuditLog stays a system/security trail of what changed and when;
    LeadNote is the sales-relevant 'what did we say, what's next' record a
    human actually wants to read. Never customer-visible."""
    __tablename__ = "lead_notes"

    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)
    note = db.Column(db.Text, nullable=False)
    contact_method = db.Column(db.String(20))  # call/whatsapp/email/meeting/other — nullable for a plain note
    created_by = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    lead = db.relationship("Lead", backref=db.backref("notes", lazy="dynamic", order_by="LeadNote.created_at.desc()"))


LEAD_STATUS_LABELS = {
    "new": "New", "contacted": "Contacted", "qualified": "Qualified",
    "proposal_requested": "Proposal Requested", "converted": "Converted", "lost": "Lost",
}
LEAD_SOURCE_LABELS = {
    "website": "Website", "contact_form": "Contact Form", "referral": "Referral",
    "instagram": "Instagram", "whatsapp": "WhatsApp", "direct": "Direct",
    "advertisement": "Advertisement", "other": "Other", "unknown": "Unknown",
}
LEAD_CONTACT_METHOD_LABELS = {"call": "Call", "whatsapp": "WhatsApp", "email": "Email", "meeting": "Meeting", "other": "Other"}


def gen_lead_id():
    while True:
        candidate = "KYT-LED-" + _random_digits(6)
        if not Lead.query.filter_by(lead_id=candidate).first():
            return candidate


def find_open_lead_by_contact(email, phone=None):
    """Conservative duplicate-detection: exact email match only (no fuzzy
    matching — two different businesses must never get merged), restricted
    to leads that are still open (not converted/lost). A re-enquiry from
    someone whose earlier lead was already closed deserves a fresh look,
    not a silent reopen of stale context, so those are excluded on
    purpose."""
    return Lead.query.filter(
        Lead.email == email, ~Lead.status.in_(["converted", "lost"])
    ).order_by(Lead.created_at.desc()).first()


def serialize_lead(lead, include_notes=False):
    data = {
        "id": lead.id,
        "lead_id": lead.lead_id,
        "name": lead.name,
        "business_name": lead.business_name,
        "email": lead.email,
        "phone": lead.phone,
        "business_category": lead.business_category,
        "source": lead.source,
        "source_label": LEAD_SOURCE_LABELS.get(lead.source, lead.source),
        "enquiry_type": lead.enquiry_type,
        "message": lead.message,
        "budget_range": lead.budget_range,
        "preferred_timeline": lead.preferred_timeline,
        "preferred_contact_method": lead.preferred_contact_method,
        "status": lead.status,
        "status_label": LEAD_STATUS_LABELS.get(lead.status, lead.status),
        "priority": lead.priority,
        "customer_id": lead.customer.customer_id if lead.customer else None,
        "project_order_id": lead.project.order_id if lead.project else None,
        "last_contacted_at": fmt_dt(lead.last_contacted_at) if lead.last_contacted_at else None,
        "next_follow_up_at": lead.next_follow_up_at.isoformat() if lead.next_follow_up_at else None,
        "converted_at": fmt_dt(lead.converted_at) if lead.converted_at else None,
        "created_at": fmt_dt(lead.created_at),
        "updated_at": fmt_dt(lead.updated_at),
    }
    if include_notes:
        data["notes"] = [serialize_lead_note(n) for n in lead.notes]
    return data


def serialize_lead_note(n):
    return {
        "id": n.id,
        "note": n.note,
        "contact_method": n.contact_method,
        "contact_method_label": LEAD_CONTACT_METHOD_LABELS.get(n.contact_method) if n.contact_method else None,
        "created_by": n.created_by,
        "created_at": fmt_dt(n.created_at),
    }


# ==============================================================================
# SECTION 3AC — PROPOSALS / QUOTATIONS (Phase 5). Sits between Project
# Request and Approved Project: a Proposal always references an existing
# Project (Phase 1 already unified "project request" and Project into one
# row via is_approved=False — there's no separate ProjectRequest entity to
# duplicate). Accepting a proposal does NOT auto-approve the project or
# touch Payment — it only locks the commercial terms and makes them
# available for admin's existing Accept-Request action to read from
# (see admin_accept_project_request's optional proposal_id param). That
# keeps the single existing approval boundary from Phase 1/2 completely
# intact rather than creating a second, conflicting one.
# ==============================================================================
class Proposal(db.Model):
    __tablename__ = "proposals"

    id = db.Column(db.Integer, primary_key=True)
    proposal_number = db.Column(db.String(20), nullable=False, index=True)  # stable across versions
    version = db.Column(db.Integer, default=1, nullable=False)

    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), default="draft", nullable=False)
    # draft / sent / viewed / changes_requested / accepted / rejected / expired / superseded

    currency_code = db.Column(db.String(3), default="INR", nullable=False)
    subtotal_minor = db.Column(db.Integer, default=0, nullable=False)  # server-recomputed from lines, never trusted from the client
    total_minor = db.Column(db.Integer, default=0, nullable=False)     # == subtotal_minor today; its own column so a future
                                                                        # discount/tax layer doesn't need a schema change

    scope_summary = db.Column(db.Text)          # "what we will build" — plain language
    deliverables = db.Column(db.Text)            # newline-separated, same convention as approved_features elsewhere
    exclusions = db.Column(db.Text)
    timeline_label = db.Column(db.String(100))
    revision_count = db.Column(db.Integer)
    revision_window_label = db.Column(db.String(100))
    additional_revision_note = db.Column(db.String(200))   # e.g. "₹1,500 per extra round" — free text, not a charge engine
    payment_terms_summary = db.Column(db.Text)              # AGREED TERMS ("50% advance, ..."), never a PaymentTransaction
    support_duration_label = db.Column(db.String(100))
    validity_date = db.Column(db.Date)

    created_by = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)
    sent_at = db.Column(db.DateTime)
    viewed_at = db.Column(db.DateTime)
    responded_at = db.Column(db.DateTime)
    accepted_at = db.Column(db.DateTime)
    rejected_at = db.Column(db.DateTime)
    customer_comment = db.Column(db.Text)  # request-changes / rejection comment

    supersedes_id = db.Column(db.Integer, db.ForeignKey("proposals.id"), nullable=True)

    project = db.relationship("Project")
    lines = db.relationship("ProposalLine", backref="proposal", lazy="dynamic", order_by="ProposalLine.sort_order")


class ProposalLine(db.Model):
    __tablename__ = "proposal_lines"

    id = db.Column(db.Integer, primary_key=True)
    proposal_id = db.Column(db.Integer, db.ForeignKey("proposals.id"), nullable=False, index=True)

    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    quantity = db.Column(db.Float, default=1.0)          # a count, not money — Float is fine here
    unit_price_minor = db.Column(db.Integer, nullable=False)  # paise, same convention as PaymentTransaction
    sort_order = db.Column(db.Integer, default=0)


# ==============================================================================
# SECTION 3AF — INVOICES (Phase 8). Distinct commercial authority from
# Proposal (what Kytron quotes) and PaymentTransaction (what money actually
# moved): an Invoice is a formal, dated REQUEST for a specific payable
# amount. "Invoice" already existed as a Document category with nothing
# behind it — this is what that gap was for.
#
# status (draft/issued/cancelled) is the only admin-controlled lifecycle.
# Whether an issued invoice is paid is NEVER a stored field an admin
# toggles — see compute_invoice_payment_status(): it's derived from the
# sum of verified PaymentTransactions linked to this invoice via
# PaymentTransaction.invoice_id, so there is exactly one authority for
# "is this paid," not two that can drift out of sync (8V).
# ==============================================================================
class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(20), unique=True, nullable=False)

    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    proposal_id = db.Column(db.Integer, db.ForeignKey("proposals.id"), nullable=True)  # what commercial basis this invoice is for, if any

    title = db.Column(db.String(200), nullable=False)
    notes = db.Column(db.Text)
    amount_minor = db.Column(db.Integer, nullable=False)  # paise — admin-entered, e.g. a specific milestone amount
    currency_code = db.Column(db.String(3), default="INR", nullable=False)

    status = db.Column(db.String(20), default="draft", nullable=False)  # draft | issued | cancelled

    issue_date = db.Column(db.Date)
    due_date = db.Column(db.Date)

    created_by = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)

    project = db.relationship("Project")
    proposal = db.relationship("Proposal")


INVOICE_STATUS_LABELS = {"draft": "Draft", "issued": "Issued", "cancelled": "Cancelled"}
INVOICE_PAYMENT_STATUS_LABELS = {"unpaid": "Unpaid", "partially_paid": "Partially Paid", "paid": "Paid", "overdue": "Overdue"}


def gen_invoice_number():
    while True:
        stamp = datetime.utcnow().strftime("%Y%m")
        candidate = f"KYT-INV-{stamp}-{_random_digits(5)}"
        if not Invoice.query.filter_by(invoice_number=candidate).first():
            return candidate


def compute_invoice_payment_status(invoice):
    """Derived, not stored. paid_minor sums only VERIFIED transactions
    linked to this specific invoice — a submitted-but-unverified proof
    never counts, matching the same rule as project-level balances."""
    if invoice.status == "cancelled":
        return "cancelled"
    if invoice.status == "draft":
        return "draft"
    paid_minor = db.session.query(
        db.func.coalesce(db.func.sum(PaymentTransaction.amount_minor), 0)
    ).filter_by(invoice_id=invoice.id, status="verified").scalar()
    if paid_minor >= invoice.amount_minor and invoice.amount_minor > 0:
        return "paid"
    if paid_minor > 0:
        return "partially_paid"
    if invoice.due_date and invoice.due_date < date.today():
        return "overdue"
    return "unpaid"


def serialize_invoice(inv):
    payment_status = compute_invoice_payment_status(inv)
    paid_minor = db.session.query(
        db.func.coalesce(db.func.sum(PaymentTransaction.amount_minor), 0)
    ).filter_by(invoice_id=inv.id, status="verified").scalar()
    return {
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "order_id": inv.project.order_id if inv.project else None,
        "proposal_number": inv.proposal.proposal_number if inv.proposal else None,
        "title": inv.title,
        "notes": inv.notes,
        "amount": from_minor_units(inv.amount_minor),
        "amount_formatted": format_inr(from_minor_units(inv.amount_minor)),
        "paid": from_minor_units(paid_minor),
        "paid_formatted": format_inr(from_minor_units(paid_minor)),
        "currency_code": inv.currency_code,
        "status": inv.status,
        "status_label": INVOICE_STATUS_LABELS.get(inv.status, inv.status),
        "payment_status": payment_status,
        "payment_status_label": INVOICE_PAYMENT_STATUS_LABELS.get(payment_status, payment_status),
        "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "created_at": fmt_dt(inv.created_at),
    }


PROPOSAL_STATUS_LABELS = {
    "draft": "Draft", "sent": "Sent", "viewed": "Viewed", "changes_requested": "Changes Requested",
    "accepted": "Accepted", "rejected": "Rejected", "expired": "Expired", "superseded": "Superseded",
}
# A proposal is editable by admin only in this state — once sent, silently
# changing it while the customer may be looking at it is exactly the
# opaque-pricing problem this phase exists to fix; further changes go
# through the explicit new-version flow instead.
PROPOSAL_EDITABLE_STATUSES = {"draft"}
PROPOSAL_VERSIONABLE_STATUSES = {"sent", "viewed", "changes_requested", "rejected", "expired"}


def gen_proposal_number():
    while True:
        candidate = "KYT-PRP-" + _random_digits(6)
        if not Proposal.query.filter_by(proposal_number=candidate).first():
            return candidate


def line_total_minor(line):
    return int(round((line.quantity or 0) * (line.unit_price_minor or 0)))


def recompute_proposal_totals(proposal):
    """Server-authoritative — the browser's line totals are never trusted,
    even when they'd add up correctly."""
    subtotal = sum(line_total_minor(l) for l in proposal.lines)
    proposal.subtotal_minor = subtotal
    proposal.total_minor = subtotal  # no discount/tax engine yet — see class docstring


def serialize_proposal_line(line):
    return {
        "id": line.id,
        "title": line.title,
        "description": line.description,
        "quantity": line.quantity,
        "unit_price": from_minor_units(line.unit_price_minor),
        "unit_price_formatted": format_inr(from_minor_units(line.unit_price_minor)),
        "line_total": from_minor_units(line_total_minor(line)),
        "line_total_formatted": format_inr(from_minor_units(line_total_minor(line))),
        "sort_order": line.sort_order,
    }


def serialize_proposal(p, include_lines=True, viewer="admin"):
    data = {
        "id": p.id,
        "proposal_number": p.proposal_number,
        "version": p.version,
        "order_id": p.project.order_id if p.project else None,
        "title": p.title,
        "status": p.status,
        "status_label": PROPOSAL_STATUS_LABELS.get(p.status, p.status),
        "currency_code": p.currency_code,
        "subtotal": from_minor_units(p.subtotal_minor),
        "total": from_minor_units(p.total_minor),
        "total_formatted": format_inr(from_minor_units(p.total_minor)),
        "scope_summary": p.scope_summary,
        "deliverables": p.deliverables,
        "exclusions": p.exclusions,
        "timeline_label": p.timeline_label,
        "revision_count": p.revision_count,
        "revision_window_label": p.revision_window_label,
        "additional_revision_note": p.additional_revision_note,
        "payment_terms_summary": p.payment_terms_summary,
        "support_duration_label": p.support_duration_label,
        "validity_date": p.validity_date.isoformat() if p.validity_date else None,
        "created_at": fmt_dt(p.created_at),
        "updated_at": fmt_dt(p.updated_at),
        "sent_at": fmt_dt(p.sent_at) if p.sent_at else None,
        "viewed_at": fmt_dt(p.viewed_at) if p.viewed_at else None,
        "responded_at": fmt_dt(p.responded_at) if p.responded_at else None,
        "accepted_at": fmt_dt(p.accepted_at) if p.accepted_at else None,
        "rejected_at": fmt_dt(p.rejected_at) if p.rejected_at else None,
        "customer_comment": p.customer_comment,
        "is_editable": p.status in PROPOSAL_EDITABLE_STATUSES,
        "is_current": p.status != "superseded",
    }
    if viewer == "admin":
        data["created_by"] = p.created_by
    if include_lines:
        data["lines"] = [serialize_proposal_line(l) for l in p.lines]
    return data


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(30), default="Other", nullable=False)
    upload_date = db.Column(db.DateTime, default=_now, nullable=False)
    uploader = db.Column(db.String(100))
    size = db.Column(db.Integer, default=0)
    mime_type = db.Column(db.String(120))
    visibility = db.Column(db.String(20), default="client")  # client | internal
    file_path = db.Column(db.String(500), nullable=False)


DOCUMENT_CATEGORIES = {
    "Agreement", "Invoice", "Assets", "Source Files",
    "Final Deliverables", "Completion Certificate", "Payment Proof", "Other",
}


class Revision(db.Model):
    __tablename__ = "revisions"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)

    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)  # pending/accepted/rejected/completed
    admin_response = db.Column(db.Text)
    upload_enabled = db.Column(db.Boolean, default=False, nullable=False)

    requested_at = db.Column(db.DateTime, default=_now, nullable=False)
    decided_at = db.Column(db.DateTime)

    files = db.relationship("RevisionFile", backref="revision", lazy="dynamic")


REVISION_STATUS_LABELS = {
    "pending": "Pending Review", "accepted": "Accepted", "rejected": "Rejected", "completed": "Completed",
}


class RevisionFile(db.Model):
    __tablename__ = "revision_files"

    id = db.Column(db.Integer, primary_key=True)
    revision_id = db.Column(db.Integer, db.ForeignKey("revisions.id"), nullable=True, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    size = db.Column(db.Integer, default=0)
    mime_type = db.Column(db.String(120))
    file_path = db.Column(db.String(500), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=_now, nullable=False)


class TimelineEvent(db.Model):
    __tablename__ = "timeline_events"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    event_type = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=_now, nullable=False)


# ==============================================================================
# SECTION 3C — PROJECT MANAGEMENT MODELS (Milestones/Tasks/Requirements/
# Approvals/Messages/Notifications) — shared by Client, Project Details,
# and Admin. Single source of truth: no separate client-side or admin-side
# copies of any of these tables.
# ==============================================================================
class Milestone(db.Model):
    __tablename__ = "milestones"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default="upcoming", nullable=False)  # upcoming/in_progress/completed/delayed
    order_index = db.Column(db.Integer, default=0, nullable=False)
    due_date = db.Column(db.Date)
    completed_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)

    tasks = db.relationship("Task", backref="milestone", lazy="dynamic")


MILESTONE_STATUS_LABELS = {
    "upcoming": "Upcoming", "in_progress": "In Progress", "completed": "Completed", "delayed": "Delayed",
}


class Task(db.Model):
    __tablename__ = "tasks"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    milestone_id = db.Column(db.Integer, db.ForeignKey("milestones.id"), nullable=True, index=True)

    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default="todo", nullable=False)  # todo/in_progress/done
    priority = db.Column(db.String(20), default="normal", nullable=False)  # low/normal/high/urgent
    assigned_to = db.Column(db.String(200))  # admin full_name at time of assignment (display only)
    due_date = db.Column(db.Date)
    order_index = db.Column(db.Integer, default=0, nullable=False)
    completed_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)


TASK_STATUS_LABELS = {"todo": "To Do", "in_progress": "In Progress", "done": "Done"}


class Requirement(db.Model):
    __tablename__ = "requirements"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    # pending (admin asked, client hasn't responded) -> submitted (client responded)
    # -> under_review (admin looking at it) -> accepted | revision_required (back to submitted expected)
    # not_applicable: admin marks it out of scope for this project — the
    # onboarding-checklist equivalent of "N/A", added in Phase 7 rather than
    # a second requirement/checklist model (7D).
    status = db.Column(db.String(20), default="pending", nullable=False)
    due_date = db.Column(db.Date)  # optional — when the client needs to provide this by (Phase 7N)
    note = db.Column(db.Text)  # client's submission note
    admin_notes = db.Column(db.Text)  # reviewer feedback when requesting revision

    requested_at = db.Column(db.DateTime, default=_now, nullable=False)
    submitted_at = db.Column(db.DateTime)
    reviewed_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)

    files = db.relationship("RequirementFile", backref="requirement", lazy="dynamic")


REQUIREMENT_STATUS_LABELS = {
    "pending": "Pending", "submitted": "Submitted", "under_review": "Under Review",
    "accepted": "Accepted", "revision_required": "Revision Required", "not_applicable": "Not Applicable",
}


class RequirementFile(db.Model):
    __tablename__ = "requirement_files"

    id = db.Column(db.Integer, primary_key=True)
    requirement_id = db.Column(db.Integer, db.ForeignKey("requirements.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    size = db.Column(db.Integer, default=0)
    mime_type = db.Column(db.String(120))
    file_path = db.Column(db.String(500), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=_now, nullable=False)


class Approval(db.Model):
    __tablename__ = "approvals"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    requirement_id = db.Column(db.Integer, db.ForeignKey("requirements.id"), nullable=True, index=True)

    item_title = db.Column(db.String(200), nullable=False)
    item_description = db.Column(db.Text)
    status = db.Column(db.String(20), default="pending", nullable=False)  # pending/approved/changes_requested
    comments = db.Column(db.Text)  # client's request-changes comment

    requested_by = db.Column(db.String(200))  # admin full_name
    submitted_at = db.Column(db.DateTime, default=_now, nullable=False)
    decided_at = db.Column(db.DateTime)


APPROVAL_STATUS_LABELS = {"pending": "Pending", "approved": "Approved", "changes_requested": "Changes Requested"}


# ==============================================================================
# SECTION 3AD — CHANGE REQUESTS (Phase 6). Distinct from ordinary Messages:
# a change request is a structured business object with a lifecycle, not
# free-form chat. Distinct from Proposal: a chargeable change request gets
# LINKED to a separate change-order Proposal (reusing the exact Phase-5
# create/send/accept flow) rather than mutating the historically-accepted
# proposal or introducing a second pricing authority — see
# admin_update_change_request()'s proposal_id handling.
# ==============================================================================
class ChangeRequest(db.Model):
    __tablename__ = "change_requests"

    id = db.Column(db.Integer, primary_key=True)
    change_request_id = db.Column(db.String(20), unique=True, nullable=False)

    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)

    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(30), default="other", nullable=False)
    status = db.Column(db.String(20), default="submitted", nullable=False)
    # submitted -> under_review -> proposal_required | approved (if included free) -> implemented
    # rejected / cancelled reachable from submitted/under_review/proposal_required

    impact = db.Column(db.String(20))  # 'included' (free, within agreed terms) | 'chargeable' | None (not yet classified)
    estimated_cost_minor = db.Column(db.Integer)  # paise; only meaningful once classified chargeable
    estimated_timeline_label = db.Column(db.String(100))
    admin_response = db.Column(db.Text)

    proposal_id = db.Column(db.Integer, db.ForeignKey("proposals.id"), nullable=True)  # the change-order proposal, if one was needed

    submitted_by = db.Column(db.String(20), default="customer", nullable=False)  # 'customer' | 'admin' — server-derived only

    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)
    resolved_at = db.Column(db.DateTime)

    project = db.relationship("Project")
    proposal = db.relationship("Proposal")


CHANGE_REQUEST_TYPE_LABELS = {
    "scope_change": "Scope Change", "additional_feature": "Additional Feature", "revision": "Revision",
    "content_change": "Content Change", "design_change": "Design Change", "timeline_change": "Timeline Change",
    "other": "Other",
}
CHANGE_REQUEST_STATUS_LABELS = {
    "submitted": "Submitted", "under_review": "Under Review", "proposal_required": "Proposal Required",
    "approved": "Approved", "rejected": "Rejected", "implemented": "Implemented", "cancelled": "Cancelled",
}
CHANGE_REQUEST_RESOLVED_STATUSES = {"rejected", "implemented", "cancelled"}


def gen_change_request_id():
    while True:
        candidate = "KYT-CHG-" + _random_digits(6)
        if not ChangeRequest.query.filter_by(change_request_id=candidate).first():
            return candidate


def serialize_change_request(cr, viewer="admin"):
    data = {
        "id": cr.id,
        "change_request_id": cr.change_request_id,
        "order_id": cr.project.order_id if cr.project else None,
        "title": cr.title,
        "description": cr.description,
        "type": cr.type,
        "type_label": CHANGE_REQUEST_TYPE_LABELS.get(cr.type, cr.type),
        "status": cr.status,
        "status_label": CHANGE_REQUEST_STATUS_LABELS.get(cr.status, cr.status),
        "impact": cr.impact,
        "estimated_cost": from_minor_units(cr.estimated_cost_minor) if cr.estimated_cost_minor is not None else None,
        "estimated_cost_formatted": format_inr(from_minor_units(cr.estimated_cost_minor)) if cr.estimated_cost_minor is not None else None,
        "estimated_timeline_label": cr.estimated_timeline_label,
        "admin_response": cr.admin_response,
        "proposal_number": cr.proposal.proposal_number if cr.proposal else None,
        "proposal_status": cr.proposal.status if cr.proposal else None,
        "submitted_by": cr.submitted_by,
        "created_at": fmt_dt(cr.created_at),
        "updated_at": fmt_dt(cr.updated_at),
        "resolved_at": fmt_dt(cr.resolved_at) if cr.resolved_at else None,
    }
    return data


# ==============================================================================
# SECTION 3AE — PROJECT NOTES (Phase 6H). Internal admin-only follow-up
# notes on a Project — the same pattern as LeadNote (Phase 4), applied to a
# different entity rather than bolted onto LeadNote itself, which would
# require a confusing dual lead_id/project_id shape on one model. Never
# rendered in any customer-facing view.
# ==============================================================================
class ProjectNote(db.Model):
    __tablename__ = "project_notes"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    note = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    project = db.relationship("Project", backref=db.backref("notes", lazy="dynamic", order_by="ProjectNote.created_at.desc()"))


def serialize_project_note(n):
    return {"id": n.id, "note": n.note, "created_by": n.created_by, "created_at": fmt_dt(n.created_at)}


class Conversation(db.Model):
    """One conversation per project — client and admin share it, not
    separate client-side/admin-side threads."""
    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), unique=True, nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)

    messages = db.relationship("Message", backref="conversation", lazy="dynamic")


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)  # denorm, for fast queries

    sender_type = db.Column(db.String(10), nullable=False)  # 'customer' | 'admin'
    sender_name = db.Column(db.String(200))
    body = db.Column(db.Text, nullable=False)

    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    read_at = db.Column(db.DateTime)  # set when the OTHER party views it


class Notification(db.Model):
    """One centralized notification system — never split per surface."""
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    recipient_type = db.Column(db.String(10), nullable=False, index=True)  # 'customer' | 'admin'
    recipient_id = db.Column(db.String(20), index=True)  # customer_id / admin_id; NULL admin row = broadcast to all admins

    type = db.Column(db.String(50), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True, index=True)
    project = db.relationship("Project")

    read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)


class SupportTicket(db.Model):
    """Started as a minimal backing store for the My Account 'Report a
    Problem' form (message/page_context/status only) — extended in Phase 9
    rather than replaced, since a second support model would duplicate this
    one. Old generic reports (no project, status open/closed) and new
    project-scoped support requests share this same table; every added
    column is nullable so historical rows stay valid without a data
    migration. Deliberately NOT reusing ChangeRequest for this: a support
    issue and a scope-change request are different business objects with
    different lifecycles, even though their shapes are similar — see
    convert_support_ticket_to_change_request() for the explicit, audited
    bridge between the two when admin decides a "bug" is really new work."""
    __tablename__ = "support_tickets"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True, index=True)  # null = general/portal-wide report, not tied to one project

    title = db.Column(db.String(200))  # nullable — old generic reports never had one
    message = db.Column(db.Text, nullable=False)
    page_context = db.Column(db.String(200))

    category = db.Column(db.String(20))  # bug/minor_correction/question/access/maintenance/other — nullable until admin classifies
    priority = db.Column(db.String(10), default="normal", nullable=False)  # low/normal/high/urgent — customer submissions are always forced to 'normal' server-side; only admin can raise it (9G)
    status = db.Column(db.String(20), default="submitted", nullable=False)  # submitted/acknowledged/in_progress/waiting_for_customer/resolved/closed/reopened (legacy rows: open/closed)

    admin_response = db.Column(db.Text)
    resolved_at = db.Column(db.DateTime)
    resolved_by = db.Column(db.String(200))
    first_response_at = db.Column(db.DateTime)  # operational visibility only — never a promised SLA (9K)

    converted_change_request_id = db.Column(db.Integer, db.ForeignKey("change_requests.id"), nullable=True)  # set when admin decides this is really new/chargeable work, not a bug (9Q)

    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)

    customer = db.relationship("Customer")
    project = db.relationship("Project")
    converted_change_request = db.relationship("ChangeRequest")


SUPPORT_STATUS_LABELS = {
    "submitted": "Submitted", "acknowledged": "Acknowledged", "in_progress": "In Progress",
    "waiting_for_customer": "Waiting for You", "resolved": "Resolved", "closed": "Closed", "reopened": "Reopened",
    "open": "Open",  # legacy value from before Phase 9 — kept valid, never written by new code
}
SUPPORT_CATEGORY_LABELS = {
    "bug": "Bug / Issue", "minor_correction": "Minor Correction", "question": "Question",
    "access": "Access / Help", "maintenance": "Maintenance", "other": "Other",
}
SUPPORT_RESOLVED_STATUSES = {"resolved", "closed"}


def gen_support_ticket_ref(ticket):
    return f"KYT-SUP-{ticket.id:06d}"


def serialize_support_ticket(t, viewer="admin"):
    data = {
        "id": t.id,
        "ref": gen_support_ticket_ref(t),
        "order_id": t.project.order_id if t.project else None,
        "project_name": (t.project.project_name or t.project.project_type) if t.project else None,
        "title": t.title,
        "message": t.message,
        "category": t.category,
        "category_label": SUPPORT_CATEGORY_LABELS.get(t.category) if t.category else None,
        "priority": t.priority,
        "status": t.status,
        "status_label": SUPPORT_STATUS_LABELS.get(t.status, t.status),
        "admin_response": t.admin_response,
        "resolved_at": fmt_dt(t.resolved_at) if t.resolved_at else None,
        "converted_change_request_id": t.converted_change_request.change_request_id if t.converted_change_request else None,
        "created_at": fmt_dt(t.created_at),
        "updated_at": fmt_dt(t.updated_at),
    }
    if viewer == "admin":
        data["customer_id"] = t.customer.customer_id if t.customer else None
        data["customer_name"] = t.customer.full_name if t.customer else "Anonymous"
        data["resolved_by"] = t.resolved_by
        data["page_context"] = t.page_context
    return data


class AuditLog(db.Model):
    """Admin-only internal record. Never exposed to any client-facing endpoint."""
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(200))
    action = db.Column(db.String(100), nullable=False)
    entity_type = db.Column(db.String(50))
    entity_id = db.Column(db.String(50))
    before_json = db.Column(db.Text)
    after_json = db.Column(db.Text)
    ip_address = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=_now, nullable=False)


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)


class LoginAttempt(db.Model):
    """Backing store for brute-force protection, survives process restarts."""
    __tablename__ = "login_attempts"

    id = db.Column(db.Integer, primary_key=True)
    identifier = db.Column(db.String(200), nullable=False, index=True)  # email or ip or order_id
    scope = db.Column(db.String(30), nullable=False)  # 'login' | 'order_login' | 'password_reset' | 'admin_login'
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    success = db.Column(db.Boolean, default=False, nullable=False)
    ip_address = db.Column(db.String(64))  # powers the My Account "security activity" feed


# ==============================================================================
# SECTION 3B — ADMIN PANEL MODELS (KYTRON Admin Panel)
# ==============================================================================
class Admin(db.Model):
    """A staff account for the Admin Panel. Entirely separate from Customer —
    admins authenticate against this table, never against customers."""
    __tablename__ = "admins"

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.String(20), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), default="admin", nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    must_change_password = db.Column(db.Boolean, default=True, nullable=False)
    phone = db.Column(db.String(40))
    session_version = db.Column(db.Integer, nullable=False, default=1)  # "sign out everywhere"

    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    lockout_until = db.Column(db.DateTime)
    last_login_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)

    def to_public_dict(self):
        return {
            "admin_id": self.admin_id,
            "name": self.full_name,
            "email": self.email,
            "phone": self.phone,
            "role": self.role,
            "must_change_password": self.must_change_password,
        }


class SystemSettings(db.Model):
    """Single-row key/value store for admin-configurable policy values
    (deadline thresholds, etc). Read/written through get_setting()/set_setting()
    below so every call site stays a one-line change if new keys are added."""
    __tablename__ = "system_settings"

    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(255), nullable=False)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now, nullable=False)


SETTINGS_DEFAULTS = {
    "deadline_due_soon_days": "7",     # <= this many days out (and not yet critical) => Due Soon
    "deadline_critical_days": "2",     # <= this many days out => Critical
}


def get_setting(key):
    row = SystemSettings.query.get(key)
    if row is not None:
        return row.value
    return SETTINGS_DEFAULTS.get(key)


def get_setting_int(key):
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return int(SETTINGS_DEFAULTS.get(key, 0))


def set_setting(key, value):
    row = SystemSettings.query.get(key)
    if row is None:
        row = SystemSettings(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)


# ---- Catalog tables (dynamic, admin-manageable in the future) --------------
class ProjectTypeModel(db.Model):
    __tablename__ = "catalog_project_types"
    id = db.Column(db.String(40), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    icon = db.Column(db.String(60))
    description = db.Column(db.String(255))
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)


class CategoryModel(db.Model):
    __tablename__ = "catalog_categories"
    id = db.Column(db.String(40), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    parent_id = db.Column(db.String(40))  # supports Level1 -> Level2 -> Level3
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)


class ServiceModel(db.Model):
    __tablename__ = "catalog_services"
    id = db.Column(db.String(40), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    icon = db.Column(db.String(60))
    description = db.Column(db.String(255))
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)


class FeatureModel(db.Model):
    __tablename__ = "catalog_features"
    id = db.Column(db.String(40), primary_key=True)
    project_type_id = db.Column(db.String(40), db.ForeignKey("catalog_project_types.id"), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(255))
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)


# ==============================================================================
# SECTION 3C — AI CONSULTANT MODULE: MODELS
# ==============================================================================
# Imported here — after every business model above, before self-migration
# below — so the AI Consultant's own tables (living in ai_consultant.db
# via the 'ai_consultant' bind configured in Section 2) get picked up by
# the same automatic migration system as everything else. See
# ai_models.py (root-level file — this module intentionally has no
# consultant/ package; see ai_consultant.py's module docstring).
import ai_models  # noqa: F401 — import registers the models with SQLAlchemy


# ==============================================================================
# SECTION 4 — AUTOMATIC DATABASE CREATION / SELF-MIGRATION
# ==============================================================================
# SQLAlchemy's create_all() only creates *missing tables* — it never alters an
# existing table. To satisfy "add new columns automatically, never touch
# existing data", we diff each model's expected columns against what SQLite
# actually has (via PRAGMA table_info) and issue additive ALTER TABLE
# statements for whatever is missing. This runs on every startup and is a
# no-op once the schema is caught up.

_SQLA_TO_SQLITE_TYPE = {
    "INTEGER": "INTEGER",
    "VARCHAR": "TEXT",
    "TEXT": "TEXT",
    "BOOLEAN": "INTEGER",
    "DATETIME": "TEXT",
    "FLOAT": "REAL",
    "DATE": "TEXT",
}


def _sqlite_type_for(column):
    type_name = column.type.__class__.__name__.upper()
    return _SQLA_TO_SQLITE_TYPE.get(type_name, "TEXT")


def _default_literal_for(column):
    """A safe, static default SQLite can use in ADD COLUMN (no server-side callables)."""
    if column.default is not None and getattr(column.default, "is_scalar", False):
        val = column.default.arg
        if isinstance(val, bool):
            return "1" if val else "0"
        if isinstance(val, (int, float)):
            return str(val)
        if isinstance(val, str):
            return "'" + val.replace("'", "''") + "'"
    if not column.nullable:
        sqlite_type = _sqlite_type_for(column)
        return "0" if sqlite_type in ("INTEGER", "REAL") else "''"
    return None


def run_self_migration():
    """Create the DB/tables if missing, then patch in any new columns —
    for the default database AND every configured bind (currently none;
    the Consultant module adds its own 'consultant' bind — see
    consultant/models.py), so the "zero manual migrations" rule holds
    generically for any future bind too.

    NOTE: this targets the Flask-SQLAlchemy 3.x API (`db.metadatas` /
    `db.engines`, one MetaData + Engine per bind key, default bind key is
    None). If this project is pinned to Flask-SQLAlchemy 2.x, that API
    doesn't exist and this needs adjusting to `db.get_engine(app, bind=...)`
    instead — worth confirming the installed version before first run.
    """
    db.create_all()  # creates every DB file, and any brand-new tables/models, across all binds

    for bind_key, metadata in db.metadatas.items():
        engine = db.engines[bind_key]
        inspector = sa_inspect(engine)
        existing_tables = set(inspector.get_table_names())

        with engine.begin() as conn:
            for table in metadata.sorted_tables:
                if table.name not in existing_tables:
                    # Brand-new table: create_all() already handled it above.
                    continue

                existing_cols = {row["name"] for row in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name in existing_cols:
                        continue
                    sqlite_type = _sqlite_type_for(column)
                    default_literal = _default_literal_for(column)
                    stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {sqlite_type}'
                    if default_literal is not None:
                        stmt += f" DEFAULT {default_literal}"
                    try:
                        conn.execute(text(stmt))
                        logger.info("Migration: added column %s.%s (bind=%s)", table.name, column.name, bind_key)
                    except Exception as exc:  # pragma: no cover - defensive, additive-only
                        logger.warning("Migration skipped for %s.%s (bind=%s): %s", table.name, column.name, bind_key, exc)

    logger.info("Database self-migration check complete.")


def seed_catalog_if_empty():
    """Populate the catalog tables once, matching the original frontend SEED data."""
    if ProjectTypeModel.query.first() is not None:
        return  # already seeded — never overwrite existing (possibly admin-edited) data

    project_types = [
        ("web_app", "Web Application", "app-window", "Custom dashboards, portals, and SaaS platforms."),
        ("mobile_app", "Mobile App", "smartphone", "Native or cross-platform iOS and Android builds."),
        ("ecommerce", "E-Commerce Store", "shopping-cart", "Storefronts, checkout flows, and inventory systems."),
        ("website", "Marketing Website", "globe", "Brand sites, landing pages, and content platforms."),
        ("automation", "Automation / Integration", "workflow", "APIs, internal tools, and workflow automation."),
        ("other", "Something Else", "sparkles", "Not sure yet? Tell us in the description step."),
    ]
    for i, (pid, name, icon, desc) in enumerate(project_types):
        db.session.add(ProjectTypeModel(id=pid, name=name, icon=icon, description=desc, sort_order=i))

    categories = [
        ("frontend", "Frontend"), ("backend", "Backend"), ("design", "UI/UX Design"),
        ("devops", "DevOps & Hosting"), ("data", "Data & Analytics"), ("qa", "QA & Testing"),
    ]
    for i, (cid, name) in enumerate(categories):
        db.session.add(CategoryModel(id=cid, name=name, sort_order=i))

    services = [
        ("new_build", "New Build", "hammer", "Ground-up development of a new product."),
        ("redesign", "Redesign", "paintbrush", "Refresh an existing product's look and feel."),
        ("migration", "Platform Migration", "move-right", "Move an existing system to new infrastructure."),
        ("maintenance", "Ongoing Maintenance", "wrench", "Continued support for a live product."),
        ("consulting", "Technical Consulting", "lightbulb", "Architecture review and technical strategy."),
        ("audit", "Security / Performance Audit", "shield-check", "Independent review of an existing system."),
    ]
    for i, (sid, name, icon, desc) in enumerate(services):
        db.session.add(ServiceModel(id=sid, name=name, icon=icon, description=desc, sort_order=i))

    features_by_type = {
        "web_app": [
            ("auth", "User Authentication", "Login, signup, and role-based access."),
            ("dashboard", "Analytics Dashboard", "Reporting and data visualization."),
            ("api", "Public API", "REST or GraphQL endpoints for integrations."),
            ("notifications", "Notifications", "Email or in-app alerts."),
        ],
        "mobile_app": [
            ("push", "Push Notifications", "Native alerts for iOS and Android."),
            ("offline", "Offline Mode", "Local data sync and offline access."),
            ("auth", "User Authentication", "Login, signup, and role-based access."),
            ("payments", "In-App Payments", "Subscriptions or one-time purchases."),
        ],
        "ecommerce": [
            ("catalog", "Product Catalog", "Categories, variants, and inventory."),
            ("checkout", "Checkout & Payments", "Cart, tax, shipping, and gateways."),
            ("accounts", "Customer Accounts", "Order history and saved details."),
            ("promotions", "Discounts & Promotions", "Coupon codes and campaigns."),
        ],
        "website": [
            ("cms", "Content Management", "Editable pages and blog."),
            ("seo", "SEO Optimization", "Structured data and performance tuning."),
            ("forms", "Lead Capture Forms", "Contact and inquiry forms."),
            ("multilingual", "Multi-Language Support", "Localized content delivery."),
        ],
        "automation": [
            ("integrations", "Third-Party Integrations", "Connect existing tools and services."),
            ("scheduling", "Scheduled Jobs", "Recurring background tasks."),
            ("webhooks", "Webhooks", "Real-time event delivery."),
            ("reporting", "Automated Reporting", "Scheduled exports and summaries."),
        ],
        "other": [
            ("discovery", "Discovery Workshop", "We help define scope together."),
        ],
    }
    for type_id, feats in features_by_type.items():
        for i, (fid, name, desc) in enumerate(feats):
            db.session.add(FeatureModel(id=fid, project_type_id=type_id, name=name, description=desc, sort_order=i))

    db.session.commit()
    logger.info("Catalog seeded.")


def backfill_payment_currency():
    """One-time, additive correction: Payment rows created before
    currency_code/₹ existed still say currency_symbol='$' (the old column
    default). This never touches any amount — only the display/code fields
    — and is safe to run every startup since the WHERE clause naturally
    becomes a no-op once every row is corrected."""
    stale = Payment.query.filter(
        db.or_(Payment.currency_symbol == "$", Payment.currency_symbol.is_(None),
               Payment.currency_code.is_(None))
    ).all()
    if not stale:
        return
    for p in stale:
        p.currency_symbol = "₹"
        p.currency_code = "INR"
    db.session.commit()
    logger.info("Currency backfill: corrected %d payment record(s) to INR/₹.", len(stale))


ADMIN_BOOTSTRAP_FILE = os.path.join(INSTANCE_DIR, "admin_bootstrap.txt")


def bootstrap_admin_if_empty():
    """Create the first Admin account if none exists yet, the same way the
    secret key is bootstrapped: generate it once, persist the one-time
    credential to a local file (never hardcode, never print a fixed
    password), and never touch it again once an Admin row exists."""
 
    email = os.environ.get("KYTRON_ADMIN_EMAIL", "admin@kytron.local").strip().lower()
    configured_password = os.environ.get("KYTRON_ADMIN_PASSWORD", "").strip()
    reset_existing = os.environ.get("KYTRON_ADMIN_RESET", "").strip() == "1"

    existing_admin = Admin.query.first()

    if existing_admin is not None:
        if reset_existing and configured_password:
            existing_admin.email = email
            existing_admin.password_hash = generate_password_hash(configured_password)
            existing_admin.must_change_password = True
            db.session.commit()
        return

    temp_password = configured_password or gen_temp_password()
 
    admin = Admin(
        admin_id=gen_admin_id(),
        full_name="Kytron Admin",
        email=email,
        password_hash=generate_password_hash(temp_password),
        role="owner",
        must_change_password=True,
    )
    db.session.add(admin)
    db.session.commit()

    with open(ADMIN_BOOTSTRAP_FILE, "w") as f:
        f.write(
            "KYTRON ADMIN PANEL — first-run credentials\n"
            "Generated once. This file is not read by the app after admin\n"
            "accounts exist; delete it after you have logged in and changed\n"
            "the password.\n\n"
            f"Email:    {email}\n"
            f"Password: {temp_password}\n"
        )
    logger.info("Admin bootstrap account created. Credentials written to %s", ADMIN_BOOTSTRAP_FILE)


# ==============================================================================
# SECTION 5 — HELPERS: IDs, PASSWORDS, TOKENS, RESPONSES
# ==============================================================================
def _random_digits(n):
    # Numeric-only random suffix (no letters), still cryptographically random
    # and non-sequential — matches the KYT-CUS-482913 / KYT-ORD-... ID spec.
    return "".join(secrets.choice(string.digits) for _ in range(n))


def gen_customer_id():
    while True:
        candidate = "KYT-CUS-" + _random_digits(6)
        if not Customer.query.filter_by(customer_id=candidate).first():
            return candidate


def gen_order_id():
    while True:
        stamp = datetime.utcnow().strftime("%Y%d%m")  # YYYY DD MM, per spec
        tail = _random_digits(6)
        candidate = f"KYT-ORD-{stamp}{tail}"
        if not Project.query.filter_by(order_id=candidate).first():
            return candidate


def gen_admin_id():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        candidate = "KYT-ADM-" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not Admin.query.filter_by(admin_id=candidate).first():
            return candidate


def gen_temp_password():
    upper = secrets.choice(string.ascii_uppercase)
    lower = secrets.choice(string.ascii_lowercase)
    digit = secrets.choice(string.digits)
    special = secrets.choice("!@#$%^&*")
    rest = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(4))
    chars = list(upper + lower + digit + special + rest)
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def gen_file_id():
    return secrets.token_urlsafe(16)


def json_error(message, status=400, errors=None, reason=None):
    payload = {"message": message}
    if errors:
        payload["errors"] = errors
    if reason:
        payload["reason"] = reason
    return jsonify(payload), status


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NAME_RE = re.compile(r"^[A-Za-z](?:[A-Za-z'\-]|[ ](?! ))*[A-Za-z'\-]?$")


def is_valid_email(value):
    return bool(value) and bool(EMAIL_RE.match(value.strip()))


def clean_name(value):
    """Trim, collapse internal whitespace to single spaces."""
    return re.sub(r"\s+", " ", (value or "").strip())


def is_valid_name(value):
    value = clean_name(value)
    if not value or len(value) > 100:
        return False
    return bool(NAME_RE.match(value))


# Minimal national-number length map for common countries (digits only,
# excluding the dial code). Falls back to a generic 7-15 digit range for
# any country not listed here.
COUNTRY_PHONE_LENGTHS = {
    "+91": (10, 10),   # India
    "+1": (10, 10),    # US / Canada
    "+44": (10, 10),   # UK
    "+61": (9, 9),      # Australia
    "+971": (9, 9),     # UAE
    "+65": (8, 8),      # Singapore
    "+49": (10, 11),    # Germany
    "+33": (9, 9),      # France
    "+86": (11, 11),    # China
    "+81": (10, 10),    # Japan
}


def is_valid_phone(digits, country_code):
    if not digits:
        return True  # phone is optional
    if not digits.isdigit():
        return False
    lo, hi = COUNTRY_PHONE_LENGTHS.get(country_code, (7, 15))
    return lo <= len(digits) <= hi


def is_valid_budget_text(value):
    """Guards any free-text numeric budget field: positive, no negatives,
    no scientific notation. (The current UI uses a fixed range <select>,
    which is safe by construction, but this stays available for any
    numeric budget input added later.)"""
    if value in (None, ""):
        return True
    if not re.match(r"^\d+(\.\d+)?$", str(value).strip()):
        return False
    return float(value) > 0


MAX_DESCRIPTION_CHARS = 5000


def clean_description(value):
    value = (value or "").strip()
    value = re.sub(r"[ \t]{2,}", " ", value)   # collapse excess horizontal whitespace
    value = re.sub(r"\n{3,}", "\n\n", value)    # collapse excess blank lines
    return value[:MAX_DESCRIPTION_CHARS]


# --- Notification -------------------------------------------------------
# SMTP configuration is environment/config based so credentials are never
# hard-coded. Set these env vars in production:
#   KYTRON_SMTP_HOST, KYTRON_SMTP_PORT, KYTRON_SMTP_USER, KYTRON_SMTP_PASSWORD,
#   KYTRON_SMTP_FROM, KYTRON_SMTP_USE_TLS (default "1")
# If KYTRON_SMTP_HOST is unset, emails are logged instead of sent — safe for
# local development. Swap the body of send_email for a different provider
# (SES, Postmark, etc.) and every call site in this file keeps working.
SMTP_HOST = os.environ.get("KYTRON_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("KYTRON_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("KYTRON_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("KYTRON_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("KYTRON_SMTP_FROM", SMTP_USER)
SMTP_USE_TLS = os.environ.get("KYTRON_SMTP_USE_TLS", "1") == "1"

# Where contact.html submissions get routed — defaults to the sending
# address if a dedicated inbox isn't configured.
CONTACT_TO_EMAIL = os.environ.get("KYTRON_CONTACT_EMAIL", SMTP_FROM)


def send_email(to_address, subject, body, html_body=None):
    """Modular send point — later, `html_body` can be produced from
    templates.html without changing any call site. Returns True/False so
    callers that must report real delivery status (e.g. the contact form,
    or credential emails) can, without changing any existing fire-and-forget
    call site, which already ignores the return value.

    SECURITY: never log `body` or `html_body`. Both routinely carry
    temporary passwords, password-reset links/tokens, and OTPs — the
    no-SMTP fallback below used to log the full body for local-dev
    visibility, which meant a customer's temporary password could land in
    plaintext in server logs. Only safe, non-secret diagnostic fields are
    logged now, in every branch."""
    if not to_address:
        logger.warning("EMAIL DELIVERY SKIPPED — no recipient address — subject=%r", subject)
        return False
    if not SMTP_HOST:
        logger.warning(
            "EMAIL DELIVERY FAILED — SMTP is not configured — recipient=%s subject=%r",
            to_address, subject,
        )
        return False
    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_address
        msg.attach(MIMEText(body, "plain"))
        if html_body:
            msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_address], msg.as_string())
        logger.info("EMAIL DELIVERED — recipient=%s subject=%r", to_address, subject)
        return True
    except Exception:
        # logger.exception intentionally receives no `body`/`html_body` —
        # only the exception traceback and the (non-secret) recipient.
        logger.exception("EMAIL DELIVERY FAILED — SMTP error — recipient=%s subject=%r", to_address, subject)
        return False


def send_project_update_email(customer, subject, body, html_body=None):
    """Single choke point for every PROJECT-UPDATE / PROJECT-LIFECYCLE email
    (submission confirmation, approval, rejection, revision activity, other
    status changes). Gated on the customer's project_update_email preference.

    Mandatory account/security emails (new-account/temp-password, password
    reset, verification, etc.) must never be routed through this helper —
    call send_email() directly for those so they always send regardless of
    this preference."""
    if not customer:
        return False
    if not customer.project_update_email:
        return False
    return send_email(customer.email, subject, body, html_body=html_body)


# ==============================================================================
# SECTION 6 — SECURITY: CSRF (origin check), RATE LIMITING, HEADERS
# ==============================================================================
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.before_request
def enforce_origin_check():
    """
    Lightweight CSRF defense that needs zero frontend changes: for any
    state-changing request, if the browser sent an Origin/Referer header,
    it must match this host. Requests without those headers (same-origin
    fetch in some browsers, or non-browser API clients) fall through to
    the session's SameSite=Lax cookie protection.
    """
    if request.method not in STATE_CHANGING_METHODS:
        return
    if not request.path.startswith("/api/"):
        return

    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    host = request.host

    def host_matches(url_value):
        if not url_value:
            return True
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url_value)
            if not parsed.netloc:
                return True
            return parsed.netloc == host
        except Exception:
            return True

    if origin and not host_matches(origin):
        return json_error("Cross-site request blocked.", 403)
    if not origin and referer and not host_matches(referer):
        return json_error("Cross-site request blocked.", 403)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"

    if request.endpoint == "serve_portfolio":
        frame_src = "'self' https:"
    else:
        frame_src = "'self'"

    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        f"frame-src {frame_src}; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )

    return response


RATE_LIMIT_WINDOW_SECONDS = 15 * 60
RATE_LIMIT_MAX_ATTEMPTS = {
    "login": 8, "order_login": 8, "password_reset": 5, "admin_login": 8, "email_check": 30, "contact_form": 5,
    "project_submit": 5,  # public, unauthenticated, creates DB rows + sends an email — the same abuse profile as contact_form
    "support_submit": 8,
}


def is_rate_limited(identifier, scope):
    window_start = _now() - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    count = LoginAttempt.query.filter(
        LoginAttempt.identifier == identifier,
        LoginAttempt.scope == scope,
        LoginAttempt.created_at >= window_start,
        LoginAttempt.success.is_(False),
    ).count()
    return count >= RATE_LIMIT_MAX_ATTEMPTS.get(scope, 8)


def record_attempt(identifier, scope, success):
    db.session.add(LoginAttempt(identifier=identifier, scope=scope, success=success))
    db.session.commit()


def write_audit_log(action, entity_type=None, entity_id=None, before=None, after=None, actor=None):
    db.session.add(AuditLog(
        actor=actor or "client_portal",
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        before_json=json.dumps(before) if before is not None else None,
        after_json=json.dumps(after) if after is not None else None,
        ip_address=request.remote_addr,
    ))
    db.session.commit()


def add_timeline_event(project_id, title, description="", event_type="info"):
    db.session.add(TimelineEvent(
        project_id=project_id, title=title, description=description, event_type=event_type,
    ))


def log_activity(action, title, description="", event_type="info", project=None,
                  entity_type=None, entity_id=None, before=None, after=None,
                  actor=None, client_visible=True):
    """Single call site for both halves of the activity system:
      - AuditLog (internal-only, complete record of everything — via the
        existing write_audit_log())
      - TimelineEvent (client-visible project history — via the existing
        add_timeline_event()), added only when client_visible=True and a
        project is given.
    Routes should call this instead of write_audit_log() directly whenever
    the action also belongs on the client-facing project timeline, so the
    two logs can never drift apart. Purely internal actions (settings
    changes, admin-only operations) should keep calling write_audit_log()
    directly, as before.
    """
    if client_visible and project is not None:
        add_timeline_event(project.id, title, description, event_type)
    write_audit_log(action, entity_type=entity_type, entity_id=entity_id,
                     before=before, after=after, actor=actor)


# ==============================================================================
# SECTION 7A — NOTIFICATION INFRASTRUCTURE
# ==============================================================================
def create_notification(recipient_type, recipient_id, notif_type, title, body="", project=None):
    """One centralized notification system — recipient_type is 'customer' or
    'admin'; recipient_id is that Customer.customer_id / Admin.admin_id
    string (matching the ID convention used everywhere else), or None for
    an admin-team broadcast. Never split into separate client/admin tables.
    """
    db.session.add(Notification(
        recipient_type=recipient_type,
        recipient_id=recipient_id,
        type=notif_type,
        title=title,
        body=body or "",
        project_id=project.id if project is not None else None,
    ))
    db.session.commit()


def notify_customer(customer, notif_type, title, body="", project=None):
    if not customer:
        return
    create_notification("customer", customer.customer_id, notif_type, title, body, project=project)


def notify_admins(notif_type, title, body="", project=None):
    """Broadcasts to the whole admin team (recipient_id=None). If per-admin
    targeting is ever needed, call create_notification('admin', admin_id, ...)
    directly instead — the underlying table already supports it."""
    create_notification("admin", None, notif_type, title, body, project=project)


# ==============================================================================
# SECTION 7B — SHARED MESSAGING INFRASTRUCTURE
# ==============================================================================
def get_project_conversation(project, create=True):
    """Enforces exactly one Conversation per project."""
    convo = Conversation.query.filter_by(project_id=project.id).first()
    if not convo and create:
        convo = Conversation(project_id=project.id)
        db.session.add(convo)
        db.session.commit()
    return convo


def send_message(project, sender_type, sender_name, body):
    convo = get_project_conversation(project)
    msg = Message(conversation_id=convo.id, project_id=project.id,
                  sender_type=sender_type, sender_name=sender_name, body=body)
    db.session.add(msg)
    convo.updated_at = _now()
    db.session.commit()

    preview = (body or "")[:140]
    if sender_type == "customer":
        notify_admins("message_received", f"New message on {project.order_id}", preview, project=project)
    else:
        notify_customer(project.customer, "message_received",
                         f"New message on {project.project_name or project.order_id}", preview, project=project)
    return msg


def mark_messages_read(conversation, reader_type):
    """reader_type is who is DOING the reading; marks the other party's
    unread messages as read and returns how many were updated."""
    if not conversation:
        return 0
    other = "admin" if reader_type == "customer" else "customer"
    unread = Message.query.filter_by(conversation_id=conversation.id, sender_type=other, read_at=None).all()
    if not unread:
        return 0
    now = _now()
    for m in unread:
        m.read_at = now
    db.session.commit()
    return len(unread)


def get_unread_message_count(project, reader_type):
    convo = Conversation.query.filter_by(project_id=project.id).first()
    if not convo:
        return 0
    other = "admin" if reader_type == "customer" else "customer"
    return Message.query.filter_by(conversation_id=convo.id, sender_type=other, read_at=None).count()


# ==============================================================================
# SECTION 7D — SHARED FILE AUTHORIZATION
# ==============================================================================
def client_can_access_project_file(project):
    """The one policy check for 'can the current request see a
    client-visible file belonging to this project' — same dual-session
    rule as resolve_client_project(), reused so every download route
    (documents, revision files, requirement files) checks access exactly
    the same way instead of five slightly different copies."""
    customer = current_customer()
    if customer and project.customer_id == customer.id:
        return True
    if session.get("authenticated_order_id") == project.order_id:
        return True
    return False


def send_stored_file(file_path, download_name):
    """Single place that turns a stored relative file_path into a Flask
    file response. Never trust a path built from user input directly —
    file_path always comes from a DB row written by save_upload()."""
    directory = os.path.join(UPLOAD_DIR, os.path.dirname(file_path))
    filename = os.path.basename(file_path)
    return send_from_directory(directory, filename, as_attachment=True, download_name=download_name)


# ==============================================================================
# SECTION 7C — SHARED VALIDATION / PAGINATION HELPERS
# ==============================================================================
def require_fields(data, *field_names):
    """Returns an {field: message} errors dict for any missing/blank
    top-level string fields; empty dict means all present."""
    errors = {}
    for name in field_names:
        value = data.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors[name] = "This field is required."
    return errors


def validate_choice(value, allowed, field_label="value"):
    """Returns an error string if value isn't one of allowed, else None.
    Used for every status/priority/type field so the frontend can never
    push an arbitrary string into a status machine."""
    if value not in allowed:
        return f"Invalid {field_label}."
    return None


def parse_pagination(args, default_per_page=20, max_per_page=100):
    """Reads ?page=&per_page= from a request.args-like mapping. Always
    returns sane values even on garbage input."""
    try:
        page = max(int(args.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(args.get("per_page", default_per_page))
    except (TypeError, ValueError):
        per_page = default_per_page
    per_page = max(1, min(per_page, max_per_page))
    return page, per_page


def paginate_query(query, args, default_per_page=20, max_per_page=100):
    """Applies parse_pagination() to a SQLAlchemy query and returns
    (items, pagination_meta_dict)."""
    page, per_page = parse_pagination(args, default_per_page, max_per_page)
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return items, {
        "page": page, "per_page": per_page, "total": total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


# ==============================================================================
# SECTION 7 — AUTH DECORATORS / SESSION HELPERS
# ==============================================================================
def current_customer():
    customer_id = session.get("customer_id")
    if not customer_id:
        return None
    customer = Customer.query.get(customer_id)
    if not customer or customer.account_disabled:
        return None
    # "Sign out everywhere": a session issued before the last
    # logout-all-sessions call carries a stale session_version and is
    # rejected here, without a second token/session store.
    if session.get("session_version") != customer.session_version:
        session.clear()
        return None
    return customer


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        customer = current_customer()
        if not customer:
            return json_error("Authentication required.", 401)
        g.customer = customer
        return fn(*args, **kwargs)
    return wrapper


def current_admin():
    admin_id = session.get("admin_id")
    if not admin_id:
        return None
    admin = Admin.query.get(admin_id)
    if not admin or not admin.is_active:
        return None
    if session.get("admin_session_version") != admin.session_version:
        session.pop("admin_id", None)
        session.pop("admin_session_version", None)
        return None
    return admin


def admin_required(fn):
    """Guards every /api/admin/* route. Completely separate session key
    (admin_id) from the customer session (customer_id), so being logged
    into the client portal never grants admin access and vice versa."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        admin = current_admin()
        if not admin:
            return json_error("Admin authentication required.", 401)
        g.admin = admin
        return fn(*args, **kwargs)
    return wrapper


def project_session_required(fn):
    """Guards the order-login dashboard (project_details.html)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        order_id = session.get("authenticated_order_id")
        if not order_id:
            return json_error("Authentication required.", 401)
        project = Project.query.filter_by(order_id=order_id).first()
        if not project:
            session.pop("authenticated_order_id", None)
            return json_error("Project not found.", 404)
        g.project = project
        return fn(*args, **kwargs)
    return wrapper


# ==============================================================================
# SECTION 7B — SHARED PROJECT ACCESS HELPER
# ==============================================================================
def resolve_client_project(order_id):
    """The single place that decides whether the current request may act on
    a given project as a client. Accepts EITHER the account-level session
    (customer_id, with an ownership check) OR the order-level session
    (authenticated_order_id) — the same dual-check pattern already used by
    client_download_document(). Returns (project, error_response); exactly
    one of the two is None.

    On success, sets g.customer and g.project so downstream code never has
    to re-derive them.
    """
    project = Project.query.filter_by(order_id=(order_id or "").upper()).first()
    if not project:
        return None, json_error("Project not found.", 404)

    customer = current_customer()
    if customer and project.customer_id == customer.id:
        g.customer = customer
        g.project = project
        return project, None

    if session.get("authenticated_order_id") == project.order_id:
        g.customer = project.customer
        g.project = project
        return project, None

    return None, json_error("You do not have access to this project.", 403)


def client_project_required(fn):
    """Decorator for /api/client/projects/<order_id>/... routes. Wraps
    resolve_client_project() so every such route gets identical
    authorization without repeating the ownership check by hand."""
    @wraps(fn)
    def wrapper(order_id, *args, **kwargs):
        project, err = resolve_client_project(order_id)
        if err:
            return err
        return fn(order_id, *args, **kwargs)
    return wrapper


def resolve_admin_project(order_id):
    """Same idea as resolve_client_project() but for the admin side, where
    the only check is 'does this approved project exist' (admin_required
    already gates the route). Used by every admin milestone/task/
    requirement/approval route so the lookup+404 isn't repeated five times."""
    project = Project.query.filter_by(order_id=(order_id or "").upper(), is_approved=True).first()
    if not project:
        return None, json_error("Project not found.", 404)
    return project, None


def admin_project_required(fn):
    @wraps(fn)
    def wrapper(order_id, *args, **kwargs):
        project, err = resolve_admin_project(order_id)
        if err:
            return err
        g.project = project
        return fn(order_id, *args, **kwargs)
    return wrapper


# ==============================================================================
# SECTION 8 — SERIALIZERS
# ==============================================================================
IST_OFFSET = timedelta(hours=5, minutes=30)


def fmt_dt(dt, fmt="%b %d, %Y"):
    """Formats stored UTC timestamps in IST for user-facing display,
    per the India-only deployment target — never show raw UTC to users."""
    if not dt:
        return None
    return (dt + IST_OFFSET).strftime(fmt)


# ---- Deadline Management (badges, buckets, filters) ------------------------
DEADLINE_BUCKET_LABELS = {
    "on_track": "On Track",
    "due_soon": "Due Soon",
    "critical": "Critical",
    "due_today": "Due Today",
    "overdue": "Overdue",
    "none": "No Deadline Set",
}
DEADLINE_BUCKET_EMOJI = {
    "on_track": "\U0001F7E2", "due_soon": "\U0001F7E1", "critical": "\U0001F7E0",
    "due_today": "\U0001F534", "overdue": "\u26AB", "none": "\u26AA",
}


def compute_deadline_bucket(deadline_date, today=None):
    """Returns (bucket_key, days_remaining|None). Thresholds come from
    SystemSettings so admins can tune policy without a code change."""
    if not deadline_date:
        return "none", None
    today = today or date.today()
    days_remaining = (deadline_date - today).days

    if days_remaining < 0:
        return "overdue", days_remaining
    if days_remaining == 0:
        return "due_today", days_remaining

    critical_days = get_setting_int("deadline_critical_days")
    due_soon_days = get_setting_int("deadline_due_soon_days")
    if days_remaining <= critical_days:
        return "critical", days_remaining
    if days_remaining <= due_soon_days:
        return "due_soon", days_remaining
    return "on_track", days_remaining


def serialize_deadline(project):
    bucket, days_remaining = compute_deadline_bucket(project.deadline_date)
    return {
        "deadline_date": project.deadline_date.isoformat() if project.deadline_date else None,
        "deadline_label": project.final_deadline,
        "days_remaining": days_remaining,
        "bucket": bucket,
        "bucket_label": DEADLINE_BUCKET_LABELS[bucket],
        "badge": DEADLINE_BUCKET_EMOJI[bucket],
    }


def parse_deadline_input(value):
    """Accepts an ISO date string (yyyy-mm-dd, what an <input type=date> sends)
    and returns a date object, or None if blank/unparseable."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def serialize_payment(payment):
    if not payment:
        return {
            "status": "pending", "status_label": "Pending", "currency_symbol": "₹", "currency_code": "INR",
            "final_cost": 0, "advance_paid": 0, "remaining_amount": 0,
            "extra_charges": 0, "addon_charges": 0, "revision_charges": 0,
            "total_due": 0, "has_transactions": False,
        }
    total_due = round((payment.final_cost or 0) + (payment.extra_charges or 0)
                       + (payment.addon_charges or 0) + (payment.revision_charges or 0), 2)
    return {
        "status": payment.status,
        "status_label": PAYMENT_STATUS_LABELS.get(payment.status, "Pending"),
        "currency_symbol": payment.currency_symbol or "₹",
        "currency_code": payment.currency_code or "INR",
        "final_cost": payment.final_cost or 0,
        "advance_paid": payment.advance_paid or 0,
        "remaining_amount": payment.remaining_amount or 0,
        "extra_charges": payment.extra_charges or 0,
        "addon_charges": payment.addon_charges or 0,
        "revision_charges": payment.revision_charges or 0,
        "total_due": total_due,
        "has_transactions": payment.project.payment_transactions.count() > 0 if payment.project else False,
    }


def serialize_payment_transaction(t):
    return {
        "id": t.id,
        "amount": from_minor_units(t.amount_minor),
        "amount_formatted": format_inr(from_minor_units(t.amount_minor)),
        "currency_code": t.currency_code,
        "method": t.method,
        "method_label": PAYMENT_TXN_METHOD_LABELS.get(t.method, t.method),
        "reference": t.reference,
        "payment_date": t.payment_date.isoformat() if t.payment_date else None,
        "note": t.note,
        "has_proof": t.proof_document_id is not None,
        "status": t.status,
        "status_label": PAYMENT_TXN_STATUS_LABELS.get(t.status, t.status),
        "submitted_by": t.submitted_by,
        "submitted_at": fmt_dt(t.submitted_at),
        "reviewed_by": t.reviewed_by,
        "reviewed_at": fmt_dt(t.reviewed_at) if t.reviewed_at else None,
        "review_note": t.review_note,
        "receipt_number": t.receipt_number,
        "invoice_number": t.invoice.invoice_number if t.invoice_id and t.invoice else None,
    }


def render_payment_receipt_html(txn, project):
    """A minimal, honest printable receipt — real order/customer/transaction
    data only. Deliberately no GSTIN/tax-registration/legal-entity fields:
    Kytron isn't currently configured for tax registration, and inventing
    those would misrepresent the business. Kept as server-rendered HTML
    (no PDF dependency) so the browser's own Print/Save-as-PDF covers the
    "downloadable" requirement without adding a new library."""
    customer_name = project.customer.full_name if project.customer else "Customer"
    rows = f"""
      <tr><td>Receipt No.</td><td>{escape(txn.receipt_number or '—')}</td></tr>
      <tr><td>Order ID</td><td>{escape(project.order_id)}</td></tr>
      <tr><td>Project</td><td>{escape(project.project_name or project.project_type or 'Project')}</td></tr>
      <tr><td>Billed to</td><td>{escape(customer_name)}</td></tr>
      <tr><td>Date received</td><td>{escape(txn.payment_date.isoformat() if txn.payment_date else fmt_dt(txn.submitted_at))}</td></tr>
      <tr><td>Method</td><td>{escape(PAYMENT_TXN_METHOD_LABELS.get(txn.method, txn.method))}</td></tr>
      <tr><td>Reference</td><td>{escape(txn.reference or '—')}</td></tr>
      <tr><td>Verified by</td><td>{escape(txn.reviewed_by or '—')}</td></tr>
      <tr><td>Verified on</td><td>{escape(fmt_dt(txn.reviewed_at) if txn.reviewed_at else '—')}</td></tr>
    """
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Receipt {escape(txn.receipt_number or '')}</title>
    <style>
      body{{font-family:system-ui,-apple-system,sans-serif;background:#0B0F19;color:#F3F4F6;padding:2rem;max-width:32rem;margin:0 auto;}}
      .card{{background:#111827;border:1px solid #1F2937;border-radius:1rem;padding:2rem;}}
      h1{{font-size:1.1rem;margin-bottom:0.25rem;}} .sub{{color:#9CA3AF;font-size:0.8rem;margin-bottom:1.5rem;}}
      .amount{{font-size:2rem;font-weight:800;color:#60A5FA;margin-bottom:1.5rem;}}
      table{{width:100%;border-collapse:collapse;font-size:0.85rem;}}
      td{{padding:0.5rem 0;border-bottom:1px solid #1F2937;}} td:first-child{{color:#9CA3AF;width:40%;}}
      .status{{display:inline-block;background:rgba(34,197,94,0.12);color:#86EFAC;padding:0.25rem 0.75rem;border-radius:9999px;font-size:0.75rem;font-weight:700;margin-bottom:1rem;}}
      .foot{{margin-top:1.5rem;font-size:0.75rem;color:#6B7280;}}
      @media print{{body{{background:#fff;color:#111;}} .card{{border-color:#ddd;}} td{{border-color:#eee;}}}}
    </style></head><body>
      <div class="card">
        <h1>Kytron Solutions</h1>
        <p class="sub">Payment Receipt</p>
        <span class="status">Verified</span>
        <div class="amount">{escape(format_inr(from_minor_units(txn.amount_minor)))}</div>
        <table>{rows}</table>
        <p class="foot">This receipt confirms a verified payment recorded against the order above. It is not a tax invoice.</p>
      </div>
    </body></html>"""
    return Response(html, mimetype="text/html")


def render_proposal_html(proposal):
    """Same server-rendered, browser-print-to-PDF approach as the payment
    receipt — reuses that pattern rather than standing up a second
    PDF-generation system (Phase 5U). No GSTIN/tax/legal-entity fields:
    Kytron isn't configured for tax registration, so none are invented."""
    project = proposal.project
    customer_name = project.customer.full_name if project and project.customer else "Customer"
    lines_html = "".join(f"""
      <tr>
        <td>{escape(l.title)}{f'<div class="line-desc">{escape(l.description)}</div>' if l.description else ''}</td>
        <td class="num">{l.quantity:g}</td>
        <td class="num">{escape(format_inr(from_minor_units(l.unit_price_minor)))}</td>
        <td class="num">{escape(format_inr(from_minor_units(line_total_minor(l))))}</td>
      </tr>""" for l in proposal.lines)

    def section(label, value):
        return f'<div class="sec"><h3>{escape(label)}</h3><p>{escape(value)}</p></div>' if value else ""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Proposal {escape(proposal.proposal_number)}</title>
    <style>
      body{{font-family:system-ui,-apple-system,sans-serif;background:#0B0F19;color:#F3F4F6;padding:2rem;max-width:40rem;margin:0 auto;}}
      .card{{background:#111827;border:1px solid #1F2937;border-radius:1rem;padding:2rem;}}
      h1{{font-size:1.2rem;margin-bottom:0.1rem;}} .sub{{color:#9CA3AF;font-size:0.8rem;margin-bottom:0.25rem;}}
      .meta{{display:flex;justify-content:space-between;font-size:0.75rem;color:#9CA3AF;margin-bottom:1.5rem;border-bottom:1px solid #1F2937;padding-bottom:1rem;}}
      table{{width:100%;border-collapse:collapse;font-size:0.8rem;margin:0.75rem 0 1.5rem;}}
      th{{text-align:left;color:#9CA3AF;font-weight:600;padding:0.4rem 0;border-bottom:1px solid #1F2937;font-size:0.7rem;text-transform:uppercase;}}
      td{{padding:0.6rem 0;border-bottom:1px solid #1F2937;vertical-align:top;}}
      .num{{text-align:right;white-space:nowrap;}}
      .line-desc{{font-size:0.75rem;color:#9CA3AF;margin-top:0.2rem;}}
      .total-row td{{border-top:2px solid #374151;border-bottom:none;font-weight:800;font-size:0.95rem;color:#60A5FA;}}
      .sec{{margin-bottom:1.1rem;}} .sec h3{{font-size:0.7rem;text-transform:uppercase;letter-spacing:0.04em;color:#9CA3AF;margin-bottom:0.3rem;}}
      .sec p{{font-size:0.825rem;white-space:pre-wrap;}}
      .foot{{margin-top:1.5rem;font-size:0.75rem;color:#6B7280;}}
      @media print{{body{{background:#fff;color:#111;}} .card{{border-color:#ddd;}} td,th{{border-color:#eee;}} .total-row td{{color:#111;}}}}
    </style></head><body>
      <div class="card">
        <h1>Kytron Solutions</h1>
        <p class="sub">{escape(proposal.title)}</p>
        <div class="meta">
          <span>Proposal {escape(proposal.proposal_number)} (v{proposal.version}) &middot; Prepared for {escape(customer_name)}</span>
          <span>{escape(fmt_dt(proposal.created_at))}{f' &middot; Valid until {escape(proposal.validity_date.isoformat())}' if proposal.validity_date else ''}</span>
        </div>
        {section("What we will build", proposal.scope_summary)}
        {section("Deliverables", proposal.deliverables)}
        <table><thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Amount</th></tr></thead>
        <tbody>{lines_html}<tr class="total-row"><td colspan="3">Total</td><td class="num">{escape(format_inr(from_minor_units(proposal.total_minor)))}</td></tr></tbody></table>
        {section("Timeline", proposal.timeline_label)}
        {section("Revisions", (f"{proposal.revision_count} included round(s)" + (f", {proposal.revision_window_label}" if proposal.revision_window_label else "")) if proposal.revision_count is not None else None)}
        {section("Additional revisions", proposal.additional_revision_note)}
        {section("Payment terms", proposal.payment_terms_summary)}
        {section("Support", proposal.support_duration_label)}
        {section("Not included", proposal.exclusions)}
        <p class="foot">This proposal is not a tax invoice. Prices are in INR.</p>
      </div>
    </body></html>"""
    return Response(html, mimetype="text/html")


def render_invoice_html(inv):
    """Same server-rendered print-friendly pattern as the proposal/receipt
    views — one consistent output mechanism, not a competing PDF system
    (8S). No GST calculation, no CGST/SGST/IGST breakdown: Kytron has no
    configured tax registration. The customer's own GSTIN (if they've
    given us one, on the Customer record) is shown as their reference
    only — it does not imply this is a GST-compliant tax invoice."""
    project = inv.project
    customer = project.customer if project else None
    customer_name = customer.full_name if customer else "Customer"
    payment_status = compute_invoice_payment_status(inv)
    paid_minor = db.session.query(
        db.func.coalesce(db.func.sum(PaymentTransaction.amount_minor), 0)
    ).filter_by(invoice_id=inv.id, status="verified").scalar()

    rows = f"""
      <tr><td>Invoice No.</td><td>{escape(inv.invoice_number)}</td></tr>
      <tr><td>Order ID</td><td>{escape(project.order_id if project else '—')}</td></tr>
      <tr><td>Billed to</td><td>{escape(customer_name)}</td></tr>
      {f'<tr><td>Customer GSTIN</td><td>{escape(customer.gstin)}</td></tr>' if customer and customer.gstin else ''}
      <tr><td>Issue date</td><td>{escape(inv.issue_date.isoformat() if inv.issue_date else '—')}</td></tr>
      <tr><td>Due date</td><td>{escape(inv.due_date.isoformat() if inv.due_date else '—')}</td></tr>
      <tr><td>Status</td><td>{escape(INVOICE_PAYMENT_STATUS_LABELS.get(payment_status, payment_status))}</td></tr>
      <tr><td>Paid so far</td><td>{escape(format_inr(from_minor_units(paid_minor)))}</td></tr>
    """
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Invoice {escape(inv.invoice_number)}</title>
    <style>
      body{{font-family:system-ui,-apple-system,sans-serif;background:#0B0F19;color:#F3F4F6;padding:2rem;max-width:32rem;margin:0 auto;}}
      .card{{background:#111827;border:1px solid #1F2937;border-radius:1rem;padding:2rem;}}
      h1{{font-size:1.1rem;margin-bottom:0.1rem;}} .sub{{color:#9CA3AF;font-size:0.8rem;margin-bottom:1.5rem;}}
      .amount{{font-size:2rem;font-weight:800;color:#60A5FA;margin-bottom:1.5rem;}}
      table{{width:100%;border-collapse:collapse;font-size:0.85rem;}}
      td{{padding:0.5rem 0;border-bottom:1px solid #1F2937;}} td:first-child{{color:#9CA3AF;width:40%;}}
      .foot{{margin-top:1.5rem;font-size:0.75rem;color:#6B7280;}}
      @media print{{body{{background:#fff;color:#111;}} .card{{border-color:#ddd;}} td{{border-color:#eee;}}}}
    </style></head><body>
      <div class="card">
        <h1>Kytron Solutions</h1>
        <p class="sub">{escape(inv.title)}</p>
        <div class="amount">{escape(format_inr(from_minor_units(inv.amount_minor)))}</div>
        <table>{rows}</table>
        {f'<p class="foot">{escape(inv.notes)}</p>' if inv.notes else ''}
        <p class="foot">This is a request for payment, not a GST tax invoice.</p>
      </div>
    </body></html>"""
    return Response(html, mimetype="text/html")


def serialize_document(doc):
    return {
        "id": doc.id,
        "file_name": doc.original_name,
        "category": doc.category,
        "upload_date": fmt_dt(doc.upload_date),
        "file_size": doc.size,
        "download_url": f"/api/client/documents/{doc.id}/download",
    }


def serialize_revision_center(project):
    latest = project.revisions.order_by(Revision.requested_at.desc()).first()
    used = project.revisions.filter(Revision.status != "rejected").count()
    remaining = max(project.revision_total - used, 0)
    data = {
        "remaining": remaining,
        "total": project.revision_total,
        "window_label": project.revision_window_label or "Standard Window",
        "status": latest.status if latest else "none",
        "status_label": REVISION_STATUS_LABELS.get(latest.status, "No Active Request") if latest else "No Active Request",
        "upload_enabled": bool(latest and latest.upload_enabled),
        "history": [
            {
                "id": r.id,
                "description": r.description,
                "status": r.status,
                "status_label": REVISION_STATUS_LABELS.get(r.status, r.status),
                "requested_at": fmt_dt(r.requested_at),
            }
            for r in project.revisions.order_by(Revision.requested_at.desc()).all()
        ],
    }
    return data


def serialize_timeline(project):
    events = project.timeline_events.order_by(TimelineEvent.created_at.asc()).all()
    return [
        {"title": e.title, "description": e.description, "timestamp": fmt_dt(e.created_at, "%b %d, %Y — %I:%M %p")}
        for e in events
    ]


def serialize_support(project):
    return {
        "status": project.support_status or "not_started",
        "status_label": {
            "active": "Active", "expiring": "Expiring Soon", "expired": "Expired", "not_started": "Not Started",
        }.get(project.support_status or "not_started", "Not Started"),
        "duration_label": project.support_duration_label,
        "expiry_date": project.support_expiry,
    }


def serialize_project_full(project):
    return {
        "project_name": project.project_name or (project.project_type or "Project"),
        "order_id": project.order_id,
        "customer_id": project.customer.customer_id if project.customer else None,
        "category": ", ".join(project.categories_list()) or None,
        "project_type": project.project_type,
        "service": project.service,
        "final_deadline": project.final_deadline,
        "approved_features": project.approved_features_list(),
        "description": project.description,
        # NOTE: project.admin_notes is deliberately NOT included — it's
        # labeled "internal" in the admin UI and was being leaked directly
        # into the authenticated customer's own project payload (found
        # during the Phase 7 client-visibility audit; nothing in
        # Project_details.html/Client.html ever rendered it, but it was
        # sitting in the raw JSON response, inspectable via devtools). See
        # Phase 7X: customer-visible vs admin-internal must be enforced
        # server-side, not just by what the frontend happens to render.
        "status": project.status,
        "status_label": STATUS_LABELS.get(project.status, project.status),
        "payment": serialize_payment(project.payment),
        "payment_transactions": [serialize_payment_transaction(t) for t in
                                  project.payment_transactions.order_by(PaymentTransaction.submitted_at.desc()).all()],
        "documents": [serialize_document(d) for d in project.documents.order_by(Document.upload_date.desc()).all()],
        "revision": serialize_revision_center(project),
        "timeline": serialize_timeline(project),
        "support": serialize_support(project),
        "delivered_at": fmt_dt(project.delivered_at) if project.delivered_at else None,
        "delivery_notes": project.delivery_notes if project.delivered_at else None,
        "handover_url": project.handover_url if project.delivered_at else None,
        "handover_notes": project.handover_notes if project.delivered_at else None,
        "next_action": compute_customer_next_action(project),
        "invoices": [serialize_invoice(i) for i in
                     Invoice.query.filter_by(project_id=project.id).filter(Invoice.status != "draft")
                     .order_by(Invoice.created_at.desc()).all()],
        "support_tickets": [serialize_support_ticket(t, viewer="client") for t in
                            SupportTicket.query.filter_by(project_id=project.id).order_by(SupportTicket.created_at.desc()).all()],
    }


def compute_project_progress(project):
    """Milestone-based progress. Returns (percent|None, current_phase|None).
    None when no milestones exist yet, rather than a fake 0%."""
    milestones = project.milestones.order_by(Milestone.order_index.asc()).all() if hasattr(project, "milestones") else []
    if not milestones:
        return None, None
    completed = sum(1 for m in milestones if m.status == "completed")
    percent = round(completed / len(milestones) * 100)
    current = next((m for m in milestones if m.status == "in_progress"), None) \
        or next((m for m in milestones if m.status != "completed"), None)
    return percent, (current.title if current else None)


def project_has_pending_action(project):
    """True when the client has something waiting on them for this
    project — feeds the dashboard 'Action Required' flag/count."""
    if project.requirements.filter(Requirement.status == "pending").count():
        return True
    if project.approvals.filter(Approval.status == "pending").count():
        return True
    return False


def serialize_project_summary(project):
    percent, current_phase = compute_project_progress(project)
    return {
        "order_id": project.order_id,
        "title": project.project_name or project.project_type,
        "project_type": project.project_type,
        "status": project.status,
        "submitted_at": fmt_dt(project.created_at),
        "updated_at": fmt_dt(project.updated_at),
        # Extension fields for the Client.html project card (degrade to
        # None/False when no milestone/requirement/approval data exists yet
        # rather than inventing placeholder values).
        "progress_percent": percent,
        "current_phase": current_phase,
        "start_date": fmt_dt(project.created_at),
        "expected_completion": project.final_deadline,
        "has_pending_action": project_has_pending_action(project),
    }


def serialize_milestone(m):
    return {
        "id": m.id,
        "title": m.title,
        "description": m.description,
        "status": m.status,
        "status_label": MILESTONE_STATUS_LABELS.get(m.status, m.status),
        "order_index": m.order_index,
        "due_date": m.due_date.isoformat() if m.due_date else None,
        "completed_at": fmt_dt(m.completed_at) if m.completed_at else None,
    }


def serialize_task(t):
    return {
        "id": t.id,
        "milestone_id": t.milestone_id,
        "title": t.title,
        "description": t.description,
        "status": t.status,
        "status_label": TASK_STATUS_LABELS.get(t.status, t.status),
        "priority": t.priority,
        "assigned_to": t.assigned_to,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "order_index": t.order_index,
    }


def serialize_requirement(r, include_files=True):
    data = {
        "id": r.id,
        "title": r.title,
        "description": r.description,
        "status": r.status,
        "status_label": REQUIREMENT_STATUS_LABELS.get(r.status, r.status),
        "due_date": r.due_date.isoformat() if r.due_date else None,
        "note": r.note,
        "admin_notes": r.admin_notes,
        "requested_at": fmt_dt(r.requested_at),
        "submitted_at": fmt_dt(r.submitted_at) if r.submitted_at else None,
        "reviewed_at": fmt_dt(r.reviewed_at) if r.reviewed_at else None,
    }
    if include_files:
        data["files"] = [{
            "id": f.id, "file_name": f.original_name, "size": f.size,
            "uploaded_at": fmt_dt(f.uploaded_at),
            "download_url": f"/api/client/requirement-files/{f.id}/download",
        } for f in r.files.order_by(RequirementFile.uploaded_at.desc()).all()]
    return data


def serialize_approval(a):
    return {
        "id": a.id,
        "requirement_id": a.requirement_id,
        "item_title": a.item_title,
        "item_description": a.item_description,
        "status": a.status,
        "status_label": APPROVAL_STATUS_LABELS.get(a.status, a.status),
        "comments": a.comments,
        "requested_by": a.requested_by,
        "submitted_at": fmt_dt(a.submitted_at),
        "decided_at": fmt_dt(a.decided_at) if a.decided_at else None,
    }


def serialize_message(m, viewer_type):
    return {
        "id": m.id,
        "sender": m.sender_name,
        "sender_type": m.sender_type,
        "mine": m.sender_type == viewer_type,
        "body": m.body,
        "timestamp": fmt_dt(m.created_at, "%b %d, %Y — %I:%M %p"),
        "read": m.read_at is not None,
    }


def serialize_notification(n):
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "read": n.read,
        "project_order_id": n.project.order_id if n.project_id and n.project else None,
        "created_at": fmt_dt(n.created_at, "%b %d, %Y — %I:%M %p"),
    }


def serialize_conversation_summary(convo, viewer_type):
    project = convo.project
    last = convo.messages.order_by(Message.created_at.desc()).first()
    other = "admin" if viewer_type == "customer" else "customer"
    unread = Message.query.filter_by(conversation_id=convo.id, sender_type=other, read_at=None).count()
    return {
        "id": convo.id,
        "project_order_id": project.order_id,
        "project_name": project.project_name or project.project_type,
        "last_message": last.body if last else None,
        "unread_count": unread,
        "updated_at": fmt_dt(convo.updated_at, "%b %d, %Y — %I:%M %p"),
    }


# ---- Admin-facing serializers (KYTRON Admin Panel) -------------------------
def serialize_project_request(project):
    """Card view for the Project Requests queue — new/unapproved submissions."""
    customer = project.customer
    return {
        "order_id": project.order_id,
        "customer_name": customer.full_name if customer else None,
        "customer_id": customer.customer_id if customer else None,
        "customer_email": customer.email if customer else None,
        "category": ", ".join(project.categories_list()) or None,
        "service": project.service,
        "project_type": project.project_type,
        "budget": project.budget_range,
        "preferred_deadline": project.preferred_deadline,
        "submission_date": fmt_dt(project.created_at, "%b %d, %Y — %I:%M %p"),
        "status": project.status,
        "description": project.description,
    }


def serialize_project_request_detail(project):
    """Full detail for the Accept/Reject review screen."""
    customer = project.customer
    return {
        "order_id": project.order_id,
        "customer": {
            "customer_id": customer.customer_id if customer else None,
            "name": customer.full_name if customer else None,
            "email": customer.email if customer else None,
            "phone": customer.phone if customer else None,
            "company": customer.company if customer else None,
        },
        "project_name": project.project_name,
        "project_type": project.project_type,
        "service": project.service,
        "categories": project.categories_list(),
        "requested_features": project.requested_features_list(),
        "budget_range": project.budget_range,
        "preferred_deadline": project.preferred_deadline,
        "description": project.description,
        "status": project.status,
        "is_approved": project.is_approved,
        "submission_date": fmt_dt(project.created_at, "%b %d, %Y — %I:%M %p"),
        "documents": [serialize_document(d) for d in project.documents.order_by(Document.upload_date.desc()).all()],
    }


def serialize_project_admin_summary(project):
    """Row view for the accepted Projects table (Section: Projects)."""
    return {
        "order_id": project.order_id,
        "title": project.project_name or project.project_type,
        "customer_name": project.customer.full_name if project.customer else None,
        "customer_id": project.customer.customer_id if project.customer else None,
        "category": ", ".join(project.categories_list()) or None,
        "project_type": project.project_type,
        "status": project.status,
        "status_label": STATUS_LABELS.get(project.status, project.status),
        "priority": project.priority or "normal",
        "payment_status": project.payment.status if project.payment else "pending",
        "support_status": project.support_status or "not_started",
        "final_deadline": project.final_deadline,
        "deadline": serialize_deadline(project),
        "created_at": fmt_dt(project.created_at),
        "updated_at": fmt_dt(project.updated_at),
    }


def serialize_revision_admin_row(revision):
    """Row view for the Revision Requests tab (admin-side, cross-project)."""
    project = revision.project
    return {
        "id": revision.id,
        "order_id": project.order_id if project else None,
        "project_name": project.project_name or project.project_type if project else None,
        "customer_name": project.customer.full_name if project and project.customer else None,
        "customer_id": project.customer.customer_id if project and project.customer else None,
        "description": revision.description,
        "request_date": fmt_dt(revision.requested_at, "%b %d, %Y — %I:%M %p"),
        "remaining_revisions": max((project.revision_total - project.revisions.filter(
            Revision.status != "rejected").count()), 0) if project else None,
        "revision_window": project.revision_window_label if project else None,
        "status": revision.status,
        "status_label": REVISION_STATUS_LABELS.get(revision.status, revision.status),
        "admin_response": revision.admin_response,
        "upload_enabled": revision.upload_enabled,
        "files": [
            {"id": f.id, "file_name": f.original_name, "size": f.size,
             "download_url": f"/api/admin/revision-files/{f.id}/download"}
            for f in revision.files.order_by(RevisionFile.uploaded_at.desc()).all()
        ],
    }


def serialize_project_workspace(project):
    """The full payload backing every tab of the Projects Workspace."""
    payment = project.payment
    used_revisions = project.revisions.filter(Revision.status != "rejected").count()
    return {
        "order_id": project.order_id,
        "overview": {
            "project_name": project.project_name or project.project_type or "Project",
            "customer_name": project.customer.full_name if project.customer else None,
            "customer_id": project.customer.customer_id if project.customer else None,
            "order_id": project.order_id,
            "status": project.status,
            "status_label": STATUS_LABELS.get(project.status, project.status),
            "deadline": serialize_deadline(project),
            "priority": project.priority or "normal",
            "payment_status": payment.status if payment else "pending",
            "payment_status_label": PAYMENT_STATUS_LABELS.get(payment.status if payment else "pending", "Pending"),
            "revision_status": REVISION_STATUS_LABELS.get(
                project.revisions.order_by(Revision.requested_at.desc()).first().status, "No Active Request"
            ) if project.revisions.first() else "No Active Request",
        },
        "details": {
            "approved_features": project.approved_features_list(),
            "requested_features": project.requested_features_list(),
            "final_cost": payment.final_cost if payment else 0,
            "approved_deadline": project.deadline_date.isoformat() if project.deadline_date else None,
            "approved_deadline_label": project.final_deadline,
            "support_duration_label": project.support_duration_label,
            "revision_count": project.revision_total,
            "revision_window_label": project.revision_window_label,
            "admin_notes": project.admin_notes,
            "status": project.status,
        },
        "payment": serialize_payment(payment),
        "payment_transactions": [serialize_payment_transaction(t) for t in
                                  project.payment_transactions.order_by(PaymentTransaction.submitted_at.desc()).all()],
        "documents": [serialize_document_admin(d) for d in
                      project.documents.order_by(Document.upload_date.desc()).all()],
        "revisions": {
            "total": project.revision_total,
            "used": used_revisions,
            "remaining": max(project.revision_total - used_revisions, 0),
            "window_label": project.revision_window_label,
            "history": [serialize_revision_admin_row(r) for r in
                        project.revisions.order_by(Revision.requested_at.desc()).all()],
        },
        "timeline": serialize_timeline(project),
        "milestones": [serialize_milestone(m) for m in project.milestones.order_by(Milestone.order_index.asc()).all()],
        "tasks": [serialize_task(t) for t in project.tasks.order_by(Task.order_index.asc()).all()],
        "requirements": [serialize_requirement(r) for r in project.requirements.order_by(Requirement.requested_at.desc()).all()],
        "approvals": [serialize_approval(a) for a in project.approvals.order_by(Approval.submitted_at.desc()).all()],
        "settings": {
            "status": project.status,
            "deadline_date": project.deadline_date.isoformat() if project.deadline_date else None,
            "support_duration_label": project.support_duration_label,
            "revision_count": project.revision_total,
            "revision_window_label": project.revision_window_label,
            "approved_features": project.approved_features_list(),
            "admin_notes": project.admin_notes,
        },
        "delivery": {
            "readiness": compute_delivery_readiness(project),
            "delivered_at": fmt_dt(project.delivered_at) if project.delivered_at else None,
            "delivered_by": project.delivered_by,
            "delivery_notes": project.delivery_notes,
            "handover_url": project.handover_url,
            "handover_notes": project.handover_notes,
        },
        "commercial_summary": compute_commercial_summary(project),
    }


def serialize_document_admin(doc):
    return {
        "id": doc.id,
        "file_name": doc.original_name,
        "category": doc.category,
        "upload_date": fmt_dt(doc.upload_date, "%b %d, %Y — %I:%M %p"),
        "file_size": doc.size,
        "visibility": doc.visibility,
        "uploader": doc.uploader,
        "download_url": f"/api/admin/documents/{doc.id}/download",
    }


# ==============================================================================
# SECTION 9 — FILE UPLOAD HELPERS
# ==============================================================================
def allowed_upload(filename):
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_UPLOAD_EXTENSIONS


def save_upload(file_storage, subfolder):
    original_name = file_storage.filename or "upload"
    if not allowed_upload(original_name):
        return None, "File type not allowed."

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_UPLOAD_BYTES:
        return None, "File exceeds the 50 MB limit."
    if size == 0:
        return None, "File is empty."

    ext = original_name.rsplit(".", 1)[1].lower()
    safe_base = secure_filename(original_name.rsplit(".", 1)[0]) or "file"
    stored_name = f"{secrets.token_hex(12)}_{safe_base}.{ext}"
    dest_dir = os.path.join(UPLOAD_DIR, subfolder)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, stored_name)
    file_storage.save(dest_path)

    mime_type = file_storage.mimetype or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    return {
        "original_name": original_name,
        "stored_name": stored_name,
        "size": size,
        "mime_type": mime_type,
        "file_path": os.path.join(subfolder, stored_name),
    }, None


# ==============================================================================
# SECTION 9B — AI CONSULTANT MODULE: ROUTES
# ==============================================================================
# Imported here — after every helper/decorator/serializer above exists on
# this module — because ai_routes.py imports admin_required, json_error,
# current_customer, and db from `app`.
from ai_routes import ai_consultant_public_bp, ai_consultant_admin_bp
app.register_blueprint(ai_consultant_public_bp)
app.register_blueprint(ai_consultant_admin_bp)


# ==============================================================================
# SECTION 10 — AUTH ENDPOINTS
# ==============================================================================
@app.route("/api/auth/session", methods=["GET"])
def auth_session():
    customer = current_customer()
    if not customer:
        return jsonify({"logged_in": False, "customer": None})
    return jsonify({"logged_in": True, "customer": customer.to_public_dict()})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    errors = {}
    if not is_valid_email(email):
        errors["email"] = "Enter a valid email address."
    if not password:
        errors["password"] = "Enter your password."
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    rate_key = f"{email}:{request.remote_addr}"
    if is_rate_limited(rate_key, "login"):
        return json_error("Too many attempts. Please try again in a few minutes.", 429)

    customer = Customer.query.filter_by(email=email).first()
    if not customer or customer.lockout_until and customer.lockout_until > _now():
        record_attempt(rate_key, "login", False)
        return json_error("We could not log you in. Check your credentials and try again.", 401)

    if not check_password_hash(customer.password_hash, password):
        record_attempt(rate_key, "login", False)
        customer.failed_login_attempts = (customer.failed_login_attempts or 0) + 1
        if customer.failed_login_attempts >= 8:
            customer.lockout_until = _now() + timedelta(minutes=15)
        db.session.commit()
        return json_error("We could not log you in. Check your credentials and try again.", 401)

    if customer.account_disabled:
        record_attempt(rate_key, "login", False)
        return json_error("This account has been disabled. Contact support for help.", 403)

    customer.failed_login_attempts = 0
    customer.lockout_until = None
    db.session.commit()
    record_attempt(rate_key, "login", True)

    session.clear()
    session.permanent = True  # 5-day "remember me" window, per PERMANENT_SESSION_LIFETIME
    session["customer_id"] = customer.id
    session["session_version"] = customer.session_version

    return jsonify({"customer": customer.to_public_dict()})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"message": "Logged out."})


@app.route("/api/auth/logout-all-sessions", methods=["POST"])
@login_required
def auth_logout_all_sessions():
    """Invalidates every session for this customer (including the current
    one) by bumping session_version — no server-side session store needed,
    since every session's stored version now fails the check in
    current_customer()."""
    g.customer.session_version = (g.customer.session_version or 1) + 1
    db.session.commit()
    write_audit_log("logout_all_sessions", "customer", g.customer.customer_id)
    session.clear()
    return jsonify({"message": "You have been signed out of all devices."})


@app.route("/api/auth/password-reset", methods=["POST"])
def auth_password_reset():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not is_valid_email(email):
        return json_error("Enter a valid email address.", 400, errors={"email": "Enter a valid email address."})

    rate_key = f"{email}:{request.remote_addr}"
    if is_rate_limited(rate_key, "password_reset"):
        return json_error("Too many attempts. Please try again later.", 429)
    record_attempt(rate_key, "password_reset", True)

    # Always respond with the same generic message so we never reveal
    # whether an email address exists in the system.
    customer = Customer.query.filter_by(email=email).first()
    if customer:
        raw_token = secrets.token_urlsafe(32)
        db.session.add(PasswordResetToken(
            customer_id=customer.id,
            token_hash=generate_password_hash(raw_token),
            expires_at=_now() + timedelta(hours=1),
        ))
        db.session.commit()
        # Points at the client portal SPA (finalized routing has no separate
        # reset page) with the token as a query param — client.html is
        # expected to detect reset_token/customer on load and open its
        # password-reset step. See integration note: this hasn't been
        # confirmed against client.html's actual JS, which isn't part of
        # this batch.
        reset_link = f"{request.url_root.rstrip('/')}/client?reset_token={raw_token}&customer={customer.customer_id}"
        send_email(customer.email, "Reset your Kytron Client Portal password",
                    f"Use this link within 1 hour to reset your password: {reset_link}")
        write_audit_log("password_reset_requested", "customer", customer.customer_id)

    return jsonify({"message": "If an account exists for that email, a reset link is on its way."})


@app.route("/api/auth/password-reset/confirm", methods=["POST"])
def auth_password_reset_confirm():
    """Completes the reset flow started above. The emailed link points at
    /client?reset_token=...&customer=..., but client.html isn't part of
    this batch, so it hasn't been confirmed to read those params and call
    this endpoint yet — the backend logic is fully implemented and ready
    once that's wired up."""
    data = request.get_json(silent=True) or {}
    raw_token = data.get("token") or ""
    customer_id = data.get("customer_id") or ""
    new_password = data.get("new_password") or ""

    if len(new_password) < 8:
        return json_error("Password must be at least 8 characters.", 400,
                            errors={"new_password": "Password must be at least 8 characters."})

    customer = Customer.query.filter_by(customer_id=customer_id).first()
    if not customer:
        return json_error("Invalid or expired reset link.", 400)

    candidates = PasswordResetToken.query.filter_by(customer_id=customer.id, used_at=None).filter(
        PasswordResetToken.expires_at >= _now()
    ).all()
    matched = next((c for c in candidates if check_password_hash(c.token_hash, raw_token)), None)
    if not matched:
        return json_error("Invalid or expired reset link.", 400)

    customer.password_hash = generate_password_hash(new_password)
    customer.must_change_password = False
    matched.used_at = _now()
    db.session.commit()
    write_audit_log("password_reset_completed", "customer", customer.customer_id)
    return jsonify({"message": "Password updated. You can now log in."})


# ==============================================================================
# SECTION 11 — CATALOG ENDPOINTS
# ==============================================================================
@app.route("/api/catalog/project-types", methods=["GET"])
def catalog_project_types():
    rows = ProjectTypeModel.query.filter_by(is_active=True).order_by(ProjectTypeModel.sort_order).all()
    return jsonify([{"id": r.id, "name": r.name, "icon": r.icon, "description": r.description} for r in rows])


@app.route("/api/catalog/categories", methods=["GET"])
def catalog_categories():
    parent = request.args.get("parent")  # supports Level1 -> Level2 -> Level3 drill-down
    query = CategoryModel.query.filter_by(is_active=True)
    if parent is not None:
        query = query.filter_by(parent_id=parent)
    else:
        query = query.filter(CategoryModel.parent_id.is_(None))
    rows = query.order_by(CategoryModel.sort_order).all()
    return jsonify([{"id": r.id, "name": r.name} for r in rows])


@app.route("/api/catalog/services", methods=["GET"])
def catalog_services():
    rows = ServiceModel.query.filter_by(is_active=True).order_by(ServiceModel.sort_order).all()
    return jsonify([{"id": r.id, "name": r.name, "icon": r.icon, "description": r.description} for r in rows])


@app.route("/api/catalog/features", methods=["GET"])
def catalog_features():
    project_type = request.args.get("project_type")
    if not project_type:
        return jsonify([])
    rows = (FeatureModel.query.filter_by(project_type_id=project_type, is_active=True)
            .order_by(FeatureModel.sort_order).all())
    return jsonify([{"id": r.id, "name": r.name, "description": r.description} for r in rows])


# ==============================================================================
# SECTION 11B — CONTACT FORM (contact.html)
# ==============================================================================
CONTACT_MESSAGE_MAX_CHARS = 5000


@app.route("/api/contact", methods=["POST"])
def submit_contact_form():
    """Server-authoritative from this point on: the Lead record is created
    and committed FIRST, before any email is attempted, so a misconfigured
    or down SMTP provider can no longer lose an enquiry entirely (previously
    the whole request failed with a 502 and nothing was ever persisted —
    see Phase-4 audit note below). Email is now a best-effort notification
    layered on top of a real database record, not the only record."""
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get("name") or "")
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()[:30] or None
    business_name = (data.get("business_name") or "").strip()[:150] or None
    business_category = (data.get("business_category") or "").strip()[:80] or None
    enquiry_type = (data.get("enquiry_type") or "").strip()[:80] or None
    budget_range = (data.get("budget_range") or "").strip()[:50] or None
    preferred_timeline = (data.get("preferred_timeline") or "").strip()[:50] or None
    preferred_contact_method = (data.get("preferred_contact_method") or "").strip()[:20] or None
    subject = (data.get("subject") or "").strip() or "New enquiry"
    message = clean_description(data.get("message") or "")[:CONTACT_MESSAGE_MAX_CHARS]

    # Source is only ever set to what we actually know — never guessed from
    # message content (see Phase-4 spec: "do not claim an Instagram lead is
    # an Instagram lead merely because the user mentioned Instagram").
    source = (data.get("source") or "").strip().lower()
    if source not in LEAD_SOURCE_LABELS:
        source = "contact_form"

    errors = {}
    if not is_valid_name(name):
        errors["name"] = "Enter your name using letters only."
    if not is_valid_email(email):
        errors["email"] = "Enter a valid email address."
    if not message:
        errors["message"] = "Enter a message."
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    identifier = request.remote_addr or "unknown"
    if is_rate_limited(identifier, "contact_form"):
        return json_error("Too many messages sent. Please try again later.", 429)
    record_attempt(identifier, "contact_form", success=False)

    existing_lead = find_open_lead_by_contact(email)
    if existing_lead:
        # Same person, still-open thread — add as a note rather than a new
        # unrelated lead (Phase-4 spec Part J: conservative duplicate
        # handling, exact-email match only).
        existing_lead.message = message  # most recent enquiry text stays the headline
        if phone and not existing_lead.phone:
            existing_lead.phone = phone
        if business_name and not existing_lead.business_name:
            existing_lead.business_name = business_name
        db.session.add(LeadNote(
            lead_id=existing_lead.id, note=f"New enquiry via contact form: {message}",
            created_by="System (contact form)",
        ))
        db.session.commit()
        lead = existing_lead
        is_new = False
    else:
        matched_customer = Customer.query.filter_by(email=email).first()
        lead = Lead(
            lead_id=gen_lead_id(), name=name, business_name=business_name, email=email, phone=phone,
            business_category=business_category, source=source, enquiry_type=enquiry_type, message=message,
            budget_range=budget_range, preferred_timeline=preferred_timeline,
            preferred_contact_method=preferred_contact_method, status="new",
            customer_id=matched_customer.id if matched_customer else None,
        )
        db.session.add(lead)
        db.session.commit()
        is_new = True

    write_audit_log("lead_created" if is_new else "lead_reenquiry", "lead", lead.id,
                     after={"email": email, "source": source})
    notify_admins("lead_created" if is_new else "lead_reenquiry",
                  f"{'New enquiry' if is_new else 'Follow-up enquiry'} from {name}",
                  message[:200])

    # Email is now a courtesy notification, not the persistence layer — its
    # failure no longer loses the enquiry, so it doesn't fail the request.
    send_email(CONTACT_TO_EMAIL, f"Contact form: {subject}", f"From: {name} <{email}>\n\n{message}")

    return jsonify({"message": "Thanks — we'll review your enquiry and get back to you.", "lead_id": lead.lead_id})


# ==============================================================================
# SECTION 12 — CLIENT PROJECT ENDPOINTS (client.html)
# ==============================================================================
@app.route("/api/client/uploads", methods=["POST"])
def client_upload_file():
    if "file" not in request.files:
        return json_error("No file provided.", 400)
    saved, error = save_upload(request.files["file"], "projects")
    if error:
        return json_error(error, 400)

    file_id = gen_file_id()
    # Staged uploads live on disk immediately; they are linked to a Project
    # row once /api/client/projects is submitted with matching file ids.
    staged_dir = os.path.join(UPLOAD_DIR, "projects", "_staged")
    os.makedirs(staged_dir, exist_ok=True)
    manifest_path = os.path.join(staged_dir, f"{file_id}.json")
    with open(manifest_path, "w") as f:
        json.dump(saved, f)

    return jsonify({"file_id": file_id, "file_name": saved["original_name"], "size": saved["size"]})


@app.route("/api/client/check-email", methods=["POST"])
def client_check_email():
    """Real-time email lookup used by client.html while the user types,
    and by the 'Existing Customer' flow to prefill + lock contact fields."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not is_valid_email(email):
        return json_error("Enter a valid email address.", 400)

    identifier = request.remote_addr or "unknown"
    if is_rate_limited(identifier, "email_check"):
        return json_error("Too many checks — please slow down and try again shortly.", 429)
    record_attempt(identifier, "email_check", success=False)

    customer = Customer.query.filter_by(email=email).first()
    if not customer:
        return jsonify({"exists": False})

    return jsonify({
        "exists": True,
        "name": customer.full_name,
        "country_code": customer.country_code or "+91",
        "phone": customer.phone or "",
        "company": customer.company or "",
        "project_update_email": bool(customer.project_update_email),
    })


@app.route("/api/client/projects", methods=["POST"])
def client_submit_project():
    identifier = request.remote_addr or "unknown"
    if is_rate_limited(identifier, "project_submit"):
        return json_error("Too many project requests submitted. Please try again later.", 429)
    record_attempt(identifier, "project_submit", success=False)

    form = request.form
    errors = {}

    full_name = clean_name(form.get("full_name") or "")
    email = (form.get("email") or "").strip().lower()
    country_code = (form.get("country_code") or "+91").strip()
    phone = re.sub(r"\D", "", form.get("phone") or "")
    company = (form.get("company") or "").strip()
    # Explicit opt-in: a missing/absent field means opted out, never assume
    # opted-in just because the checkbox wasn't submitted.
    project_update_email = (form.get("project_update_email") or "").strip().lower() in ("true", "1", "on", "yes")
    project_notes = clean_description(form.get("project_notes") or "")
    budget_range = form.get("budget_range") or ""
    project_type = form.get("project_type") or ""
    agree_to_terms = form.get("agree_to_terms")
    submitted_customer_id = (form.get("customer_id") or "").strip()

    categories = [c for c in form.getlist("categories[]") if c]
    services = [s for s in form.getlist("services[]") if s]
    features = [f for f in form.getlist("features[]") if f]
    uploaded_file_ids = [fid for fid in form.getlist("uploaded_file_ids[]") if fid]

    if not is_valid_name(full_name):
        errors["full_name"] = "Enter your full name using letters only."
    if not is_valid_email(email):
        errors["email"] = "Enter a valid email address."
    if phone and not is_valid_phone(phone, country_code):
        errors["phone"] = "Enter a valid phone number for the selected country."
    if not project_notes:
        errors["project_notes"] = "Describe your project."
    if not project_type:
        errors["project_type"] = "Select a project type."
    if not agree_to_terms:
        errors["agree_to_terms"] = "You must agree to the terms to continue."
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    # --- Resolve customer: logged-in session > submitted customer_id > new ---
    customer = current_customer()
    created_new_customer = False

    if not customer and submitted_customer_id:
        customer = Customer.query.filter_by(customer_id=submitted_customer_id).first()

    if not customer:
        existing_by_email = Customer.query.filter_by(email=email).first()
        if existing_by_email:
            # Client portal never silently reassigns an existing account —
            # ask them to sign in instead of creating a duplicate/orphan project.
            return json_error(
                "An account already exists for this email. Please sign in to continue.",
                409, errors={"email": "An account already exists for this email."},
            )
        temp_password = gen_temp_password()
        customer = Customer(
            customer_id=gen_customer_id(),
            full_name=full_name,
            email=email,
            country_code=country_code or "+91",
            phone=phone or None,
            company=company or None,
            project_update_email=project_update_email,
            password_hash=generate_password_hash(temp_password),
            must_change_password=True,
        )
        db.session.add(customer)
        db.session.flush()  # obtain customer.id before commit
        created_new_customer = True

    # Idempotency guard against double-submit (double-click, refresh after a
    # slow response, network retry) — the single genuinely dangerous
    # duplicate-creation risk on this route. Deliberately simple: no new
    # model, no client-side idempotency key required. If this exact
    # customer already has a just-created Registered project with the same
    # description in the last 2 minutes, treat this as a resubmission of
    # the same request and return the existing project instead of creating
    # a second one.
    recent_duplicate = Project.query.filter(
        Project.customer_id == customer.id, Project.status == "Registered",
        Project.description == project_notes,
        Project.created_at >= _now() - timedelta(minutes=2),
    ).order_by(Project.created_at.desc()).first()
    if recent_duplicate:
        return jsonify({
            "order_id": recent_duplicate.order_id,
            "customer_id": customer.customer_id,
            "status": recent_duplicate.status,
            "new_customer_account": False,
            "credential_email_sent": None,
        }), 201

    order_id = gen_order_id()
    project = Project(
        order_id=order_id,
        customer_id=customer.id,
        project_type=project_type,
        project_name=(form.get("project_name") or "").strip() or None,
        categories=json.dumps(categories),
        requested_features=json.dumps(features),
        approved_features=json.dumps([]),
        budget_range=budget_range,
        preferred_deadline=form.get("preferred_deadline") or None,
        description=project_notes,
        status="Registered",
        is_approved=False,
    )
    if services:
        project.service = services[0]
    db.session.add(project)
    db.session.flush()

    db.session.add(Payment(project_id=project.id, estimated_budget=0.0, currency_symbol="₹", currency_code="INR"))

    # Attach any files that were pre-uploaded via /api/client/uploads.
    staged_dir = os.path.join(UPLOAD_DIR, "projects", "_staged")
    for file_id in uploaded_file_ids:
        manifest_path = os.path.join(staged_dir, f"{file_id}.json")
        if not os.path.exists(manifest_path):
            continue
        with open(manifest_path) as f:
            saved = json.load(f)
        db.session.add(Document(
            project_id=project.id,
            original_name=saved["original_name"],
            stored_name=saved["stored_name"],
            category="Assets",
            uploader=full_name,
            size=saved["size"],
            mime_type=saved["mime_type"],
            visibility="client",
            file_path=saved["file_path"],
        ))
        os.remove(manifest_path)

    add_timeline_event(project.id, "Project Registered", "Your project brief was received.", "registered")
    db.session.commit()

    write_audit_log("project_registered", "project", order_id, after={"customer_id": customer.customer_id})

    credential_email_sent = None  # None = not applicable (existing customer, no new credentials to send)
    if created_new_customer:
        temp_password_notice = (
            f"Welcome to Kytron. Your Customer ID is {customer.customer_id}. "
            f"Temporary password: {temp_password}. Please change it after your first login."
        )
        credential_email_sent = send_email(customer.email, "Your Kytron Client Portal account", temp_password_notice)
        # Registration itself already committed above — email delivery is
        # reported, not allowed to roll back a successfully created
        # customer/project. Never log temp_password here; send_email()'s
        # own no-SMTP/failure paths already avoid it.
        write_audit_log(
            "credential_email_sent" if credential_email_sent else "credential_email_failed",
            "customer", customer.customer_id,
            after={"order_id": order_id, "recipient": customer.email},
        )

    send_project_update_email(customer, f"Project received — Order {order_id}",
                f"We received your project brief. Order ID: {order_id}.")

    return jsonify({
        "order_id": order_id,
        "customer_id": customer.customer_id,
        "status": project.status,
        "new_customer_account": created_new_customer,
        # True/False only when a credential email was actually attempted
        # (new account); null/omitted-equivalent for an existing customer,
        # where there's nothing new to deliver.
        "credential_email_sent": credential_email_sent,
    }), 201


@app.route("/api/client/projects/find", methods=["POST"])
def client_find_project():
    data = request.get_json(silent=True) or {}
    order_id = (data.get("order_id") or "").strip().upper()
    email = (data.get("email") or "").strip().lower()

    if not order_id or not is_valid_email(email):
        return json_error("Enter your Order ID and email.", 400)

    project = Project.query.filter_by(order_id=order_id).first()
    if not project or not project.customer or project.customer.email != email:
        return json_error("We couldn't match that Order ID with the email provided.", 404)

    return jsonify(serialize_project_summary(project))


@app.route("/api/client/projects/mine", methods=["GET"])
@login_required
def client_my_projects():
    projects = g.customer.projects.order_by(Project.created_at.desc()).all()
    return jsonify([serialize_project_summary(p) for p in projects])


@app.route("/api/client/account/notification-preferences", methods=["GET"])
@login_required
def client_get_notification_preferences():
    return jsonify({"project_update_email": bool(g.customer.project_update_email)})


@app.route("/api/client/account/notification-preferences", methods=["PUT"])
@login_required
def client_update_notification_preferences():
    data = request.get_json(silent=True) or {}
    if "project_update_email" not in data:
        return json_error("project_update_email is required.", 400,
                            errors={"project_update_email": "This field is required."})

    before = {
        "project_update_email": g.customer.project_update_email,
        "notif_marketing_email": g.customer.notif_marketing_email,
        "notif_inapp_enabled": g.customer.notif_inapp_enabled,
    }
    g.customer.project_update_email = bool(data.get("project_update_email"))
    # Additive: the richer per-category split My_account.html's UI presents.
    # Optional so the original single-field PUT contract client.html already
    # uses keeps working unchanged.
    if "notif_marketing_email" in data:
        g.customer.notif_marketing_email = bool(data.get("notif_marketing_email"))
    if "notif_inapp_enabled" in data:
        g.customer.notif_inapp_enabled = bool(data.get("notif_inapp_enabled"))
    db.session.commit()

    after = {
        "project_update_email": g.customer.project_update_email,
        "notif_marketing_email": g.customer.notif_marketing_email,
        "notif_inapp_enabled": g.customer.notif_inapp_enabled,
    }
    write_audit_log("notification_preferences_updated", "customer", g.customer.customer_id,
                     before=before, after=after)

    return jsonify(after)


# ==============================================================================
# SECTION 12B — CLIENT DASHBOARD / DOCUMENTS / MESSAGES / NOTIFICATIONS
# (client.html — was "NOT YET IMPLEMENTED" against the documented contract)
# ==============================================================================
@app.route("/api/client/dashboard/summary", methods=["GET"])
@login_required
def client_dashboard_summary():
    projects = g.customer.projects.all()
    project_ids = [p.id for p in projects]

    pending_actions = 0
    upcoming_milestones = 0
    if project_ids:
        pending_actions = (
            Requirement.query.filter(Requirement.project_id.in_(project_ids),
                                       Requirement.status.in_(["pending", "revision_required"])).count()
            + Approval.query.filter(Approval.project_id.in_(project_ids), Approval.status == "pending").count()
        )
        upcoming_milestones = Milestone.query.filter(
            Milestone.project_id.in_(project_ids), Milestone.status != "completed",
            Milestone.due_date.isnot(None),
        ).count()

    unread_messages = sum(get_unread_message_count(p, "customer") for p in projects)

    return jsonify({
        "active_projects": sum(1 for p in projects if p.status not in ("Completed", "Cancelled")),
        "pending_actions": pending_actions,
        "upcoming_milestones": upcoming_milestones,
        "unread_messages": unread_messages,
    })


@app.route("/api/client/actions", methods=["GET"])
@login_required
def client_actions():
    projects = {p.id: p for p in g.customer.projects.all()}
    if not projects:
        return jsonify([])

    items = []
    reqs = Requirement.query.filter(Requirement.project_id.in_(projects.keys()),
                                      Requirement.status.in_(["pending", "revision_required"])).all()
    for r in reqs:
        p = projects[r.project_id]
        items.append({
            "id": f"requirement-{r.id}", "type": "requirement",
            "title": r.title, "project_order_id": p.order_id,
            "project_name": p.project_name or p.project_type,
            "cta_label": "Submit response" if r.status == "pending" else "Revise & resubmit",
            "due_label": None,
        })

    approvals = Approval.query.filter(Approval.project_id.in_(projects.keys()), Approval.status == "pending").all()
    for a in approvals:
        p = projects[a.project_id]
        items.append({
            "id": f"approval-{a.id}", "type": "approval",
            "title": a.item_title, "project_order_id": p.order_id,
            "project_name": p.project_name or p.project_type,
            "cta_label": "Review & approve", "due_label": None,
        })

    return jsonify(items)


@app.route("/api/client/activity", methods=["GET"])
@login_required
def client_activity():
    """Client-visible activity is the existing TimelineEvent feed — never
    AuditLog, which is internal-only (see log_activity())."""
    project_ids = [p.id for p in g.customer.projects.all()]
    if not project_ids:
        return jsonify([])

    events = (TimelineEvent.query
              .filter(TimelineEvent.project_id.in_(project_ids))
              .order_by(TimelineEvent.created_at.desc())
              .limit(50).all())
    projects_by_id = {p.id: p for p in g.customer.projects.all()}
    return jsonify([{
        "actor": "KYTRON Team",
        "action": e.title,
        "project_order_id": projects_by_id[e.project_id].order_id,
        "project_name": projects_by_id[e.project_id].project_name or projects_by_id[e.project_id].project_type,
        "timestamp": fmt_dt(e.created_at, "%b %d, %Y — %I:%M %p"),
    } for e in events])


@app.route("/api/client/upcoming", methods=["GET"])
@login_required
def client_upcoming():
    projects_by_id = {p.id: p for p in g.customer.projects.all()}
    if not projects_by_id:
        return jsonify([])

    upcoming = []
    milestones = Milestone.query.filter(
        Milestone.project_id.in_(projects_by_id.keys()), Milestone.status != "completed",
        Milestone.due_date.isnot(None),
    ).order_by(Milestone.due_date.asc()).limit(20).all()
    for m in milestones:
        p = projects_by_id[m.project_id]
        upcoming.append({
            "date": m.due_date.isoformat(), "label": m.title,
            "project_order_id": p.order_id, "project_name": p.project_name or p.project_type,
            "type": "milestone",
        })

    tasks = Task.query.filter(
        Task.project_id.in_(projects_by_id.keys()), Task.status != "done", Task.due_date.isnot(None),
    ).order_by(Task.due_date.asc()).limit(20).all()
    for t in tasks:
        p = projects_by_id[t.project_id]
        upcoming.append({
            "date": t.due_date.isoformat(), "label": t.title,
            "project_order_id": p.order_id, "project_name": p.project_name or p.project_type,
            "type": "task",
        })

    upcoming.sort(key=lambda x: x["date"])
    return jsonify(upcoming[:20])


@app.route("/api/client/documents", methods=["GET"])
@login_required
def client_documents():
    project_ids = [p.id for p in g.customer.projects.all()]
    if not project_ids:
        return jsonify([])

    projects_by_id = {p.id: p for p in g.customer.projects.all()}
    docs = (Document.query
            .filter(Document.project_id.in_(project_ids), Document.visibility == "client")
            .order_by(Document.upload_date.desc()).all())

    out = []
    for d in docs:
        item = serialize_document(d)
        p = projects_by_id[d.project_id]
        item["project_order_id"] = p.order_id
        item["project_name"] = p.project_name or p.project_type
        out.append(item)
    return jsonify(out)


@app.route("/api/client/messages/conversations", methods=["GET"])
@login_required
def client_conversations():
    project_ids = [p.id for p in g.customer.projects.all()]
    if not project_ids:
        return jsonify([])
    convos = (Conversation.query
              .filter(Conversation.project_id.in_(project_ids))
              .order_by(Conversation.updated_at.desc()).all())
    return jsonify([serialize_conversation_summary(c, "customer") for c in convos])


@app.route("/api/client/messages/<int:conv_id>", methods=["GET"])
@login_required
def client_conversation_messages(conv_id):
    convo = Conversation.query.get_or_404(conv_id)
    if not convo.project or convo.project.customer_id != g.customer.id:
        return json_error("You do not have access to this conversation.", 403)

    msgs = convo.messages.order_by(Message.created_at.asc()).all()
    mark_messages_read(convo, "customer")
    return jsonify([serialize_message(m, "customer") for m in msgs])


@app.route("/api/client/messages/<int:conv_id>", methods=["POST"])
@login_required
def client_reply_conversation(conv_id):
    convo = Conversation.query.get_or_404(conv_id)
    if not convo.project or convo.project.customer_id != g.customer.id:
        return json_error("You do not have access to this conversation.", 403)

    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return json_error("Message can't be empty.", 400, errors={"body": "This field is required."})

    msg = send_message(convo.project, "customer", g.customer.full_name, body)
    return jsonify(serialize_message(msg, "customer")), 201


@app.route("/api/client/notifications", methods=["GET"])
@login_required
def client_notifications():
    notifs = (Notification.query
              .filter_by(recipient_type="customer", recipient_id=g.customer.customer_id)
              .order_by(Notification.created_at.desc()).limit(50).all())
    return jsonify([serialize_notification(n) for n in notifs])


@app.route("/api/client/notifications/<int:notif_id>/read", methods=["POST"])
@login_required
def client_mark_notification_read(notif_id):
    n = Notification.query.get_or_404(notif_id)
    if n.recipient_type != "customer" or n.recipient_id != g.customer.customer_id:
        return json_error("Notification not found.", 404)
    n.read = True
    db.session.commit()
    return jsonify(serialize_notification(n))


@app.route("/api/client/notifications/read-all", methods=["POST"])
@login_required
def client_mark_all_notifications_read():
    (Notification.query
     .filter_by(recipient_type="customer", recipient_id=g.customer.customer_id, read=False)
     .update({"read": True}, synchronize_session=False))
    db.session.commit()
    return jsonify({"message": "All notifications marked as read."})


# ==============================================================================
# SECTION 12A — MY ACCOUNT (client.html + my_account.html shared)
# ==============================================================================
@app.route("/my-account")
def serve_my_account():
    return send_from_directory(BASE_DIR, "My_account.html")

@app.route("/api/client/account/profile", methods=["PATCH"])
@login_required
def client_update_profile():
    data = request.get_json(silent=True) or {}
    errors = {}

    name = data.get("name")
    if name is not None:
        if not is_valid_name(clean_name(name)):
            errors["name"] = "Enter a valid name."

    phone = data.get("phone")
    if phone is not None and phone != "" and not is_valid_phone(phone, g.customer.country_code or "+91"):
        errors["phone"] = "Enter a valid phone number."

    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    before = {"name": g.customer.full_name, "display_name": g.customer.display_name, "phone": g.customer.phone}
    if name is not None:
        g.customer.full_name = clean_name(name)
    if "display_name" in data:
        g.customer.display_name = clean_name(data.get("display_name") or "") or None
    if phone is not None:
        g.customer.phone = phone or None
        g.customer.phone_verified = False  # changing the number invalidates prior verification
    db.session.commit()

    write_audit_log("profile_updated", "customer", g.customer.customer_id, before=before, after=data)
    return jsonify(g.customer.to_account_dict())


@app.route("/api/client/account/business", methods=["PATCH"])
@login_required
def client_update_business():
    data = request.get_json(silent=True) or {}
    before = {
        "company": g.customer.company, "website": g.customer.business_website,
        "business_type": g.customer.business_type, "address": g.customer.address,
        "city": g.customer.city, "state": g.customer.state,
        "pin_code": g.customer.pin_code, "gstin": g.customer.gstin,
    }
    for field, column in (
        ("company", "company"), ("website", "business_website"), ("business_type", "business_type"),
        ("address", "address"), ("city", "city"), ("state", "state"),
        ("pin_code", "pin_code"), ("gstin", "gstin"),
    ):
        if field in data:
            setattr(g.customer, column, (data.get(field) or "").strip() or None)
    db.session.commit()

    write_audit_log("business_info_updated", "customer", g.customer.customer_id, before=before, after=data)
    return jsonify(g.customer.to_account_dict())


@app.route("/api/client/account/photo", methods=["POST"])
@login_required
def client_upload_photo():
    file = request.files.get("file")
    if not file or not file.filename:
        return json_error("Choose a photo to upload.", 400)

    ext = (file.filename.rsplit(".", 1)[-1] or "").lower()
    if ext not in {"png", "jpg", "jpeg", "webp", "gif"}:
        return json_error("Photo must be a PNG, JPG, WEBP, or GIF.", 400)

    saved, error = save_upload(file, "account_photos")
    if error:
        return json_error(error, 400)

    g.customer.photo_path = saved["file_path"]
    db.session.commit()

    write_audit_log("account_photo_updated", "customer", g.customer.customer_id)
    return jsonify(g.customer.to_account_dict())


@app.route("/api/client/account/photo/<customer_id>", methods=["GET"])
def client_get_photo(customer_id):
    """Publicly reachable by design (an <img src> can't send auth headers),
    but only serves a photo that exists — no directory listing, no path
    built from anything but the stored DB value."""
    customer = Customer.query.filter_by(customer_id=customer_id.upper()).first()
    if not customer or not customer.photo_path:
        return json_error("Not found.", 404)
    return send_stored_file(customer.photo_path, "photo" + os.path.splitext(customer.photo_path)[1])


@app.route("/api/client/account/verification-status", methods=["GET"])
@login_required
def client_verification_status():
    return jsonify({
        "email_verified": bool(g.customer.email_verified),
        "phone_verified": bool(g.customer.phone_verified),
    })


@app.route("/api/client/account/verify/phone/send", methods=["POST"])
@login_required
def client_send_phone_otp():
    if not g.customer.phone:
        return json_error("Add a phone number first.", 400)

    rate_key = f"phone_otp:{g.customer.customer_id}"
    if is_rate_limited(rate_key, "password_reset"):  # reuses the same generic attempt-based throttle
        return json_error("Too many attempts. Please try again in a few minutes.", 429)

    code = _random_digits(6)
    g.customer.phone_otp_hash = generate_password_hash(code)
    g.customer.phone_otp_expires_at = _now() + timedelta(minutes=10)
    g.customer.phone_otp_attempts = 0
    db.session.commit()
    record_attempt(rate_key, "password_reset", True)

    # No SMS gateway wired up yet (see BACKEND_IMPLEMENTATION_PROMPT.md —
    # email/SMS notifications are documented as future work). Logging the
    # code server-side keeps this endpoint genuinely functional for testing
    # today without inventing a fake "always succeeds" verification.
    logger.info("Phone OTP for %s: %s (expires in 10 min)", g.customer.customer_id, code)
    return jsonify({"message": "A verification code has been sent to your phone."})


@app.route("/api/client/account/verify/phone/confirm", methods=["POST"])
@login_required
def client_confirm_phone_otp():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()

    if not g.customer.phone_otp_hash or not g.customer.phone_otp_expires_at:
        return json_error("Request a new code first.", 400)
    if g.customer.phone_otp_expires_at < _now():
        return json_error("That code has expired. Request a new one.", 400, reason="expired")
    if (g.customer.phone_otp_attempts or 0) >= 5:
        return json_error("Too many attempts. Request a new code.", 429)

    if not code or not check_password_hash(g.customer.phone_otp_hash, code):
        g.customer.phone_otp_attempts = (g.customer.phone_otp_attempts or 0) + 1
        db.session.commit()
        return json_error("Incorrect code.", 400)

    g.customer.phone_verified = True
    g.customer.phone_otp_hash = None
    g.customer.phone_otp_expires_at = None
    g.customer.phone_otp_attempts = 0
    db.session.commit()

    write_audit_log("phone_verified", "customer", g.customer.customer_id)
    return jsonify({"phone_verified": True})


@app.route("/api/client/account/change-password", methods=["POST"])
@login_required
def client_change_password():
    data = request.get_json(silent=True) or {}
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""

    if not check_password_hash(g.customer.password_hash, current_password):
        return json_error("Current password is incorrect.", 401, errors={"current_password": "Incorrect password."})

    if len(new_password) < 8:
        return json_error("Password must be at least 8 characters.", 400,
                            errors={"new_password": "Password must be at least 8 characters."})

    g.customer.password_hash = generate_password_hash(new_password)
    g.customer.must_change_password = False
    db.session.commit()

    write_audit_log("password_changed", "customer", g.customer.customer_id)
    return jsonify({"message": "Password updated."})


@app.route("/api/client/account/security-activity", methods=["GET"])
@login_required
def client_security_activity():
    attempts = (LoginAttempt.query
                .filter(LoginAttempt.identifier.like(f"{g.customer.email}%"),
                        LoginAttempt.scope.in_(["login", "order_login"]))
                .order_by(LoginAttempt.created_at.desc())
                .limit(20).all())
    return jsonify([{
        "event": "Login successful" if a.success else "Login attempt failed",
        "device": None,  # no user-agent capture today — documented limitation, not faked
        "location": a.ip_address,
        "timestamp": fmt_dt(a.created_at, "%b %d, %Y — %I:%M %p"),
    } for a in attempts])


@app.route("/api/client/account/preferences", methods=["GET"])
@login_required
def client_get_preferences():
    return jsonify({
        "language": g.customer.pref_language or "en",
        "country": g.customer.pref_country or "IN",
        "currency": g.customer.pref_currency or "INR",
        "appearance": g.customer.pref_appearance or "dark",
    })


@app.route("/api/client/account/preferences", methods=["PATCH"])
@login_required
def client_update_preferences():
    data = request.get_json(silent=True) or {}
    before = {
        "language": g.customer.pref_language, "country": g.customer.pref_country,
        "currency": g.customer.pref_currency, "appearance": g.customer.pref_appearance,
    }
    for field, column in (("language", "pref_language"), ("country", "pref_country"),
                           ("currency", "pref_currency"), ("appearance", "pref_appearance")):
        if field in data and data[field]:
            setattr(g.customer, column, str(data[field])[:20])
    db.session.commit()

    write_audit_log("preferences_updated", "customer", g.customer.customer_id, before=before, after=data)
    return jsonify({
        "language": g.customer.pref_language, "country": g.customer.pref_country,
        "currency": g.customer.pref_currency, "appearance": g.customer.pref_appearance,
    })


@app.route("/api/client/account/billing/history", methods=["GET"])
@login_required
def client_billing_history():
    """Real payment history across every project this customer owns —
    one row per VERIFIED PaymentTransaction (the actual money-movement
    ledger from Phase 3), not a single per-project summary row. Rewritten
    in Phase 11 after finding the original per-project version predated
    PaymentTransaction and never surfaced individual payments."""
    project_ids = [p.id for p in g.customer.projects.all()]
    txns = (PaymentTransaction.query
            .filter(PaymentTransaction.project_id.in_(project_ids), PaymentTransaction.status == "verified")
            .order_by(PaymentTransaction.reviewed_at.desc().nullslast(), PaymentTransaction.submitted_at.desc())
            .all()) if project_ids else []
    project_by_id = {p.id: p for p in g.customer.projects.all()}
    return jsonify([{
        "date": fmt_dt(t.reviewed_at) if t.reviewed_at else fmt_dt(t.submitted_at),
        "amount": from_minor_units(t.amount_minor),
        "currency_symbol": "₹",
        "project_order_id": project_by_id[t.project_id].order_id if t.project_id in project_by_id else None,
        "status": "paid", "status_label": "Paid",
        "method_label": PAYMENT_TXN_METHOD_LABELS.get(t.method, t.method),
        "receipt_url": f"/api/client/payment-transactions/{t.id}/receipt" if t.receipt_number else None,
    } for t in txns])


@app.route("/api/client/account/billing/documents", methods=["GET"])
@login_required
def client_billing_documents():
    """Real issued invoices across every project this customer owns —
    pulled from the actual Invoice model (Phase 8). Rewritten in Phase 11:
    the original version looked for Document rows with category='Invoice',
    but Invoice records are rendered dynamically and never create a
    Document row, so this always returned empty."""
    project_ids = [p.id for p in g.customer.projects.all()]
    invoices = (Invoice.query
                .filter(Invoice.project_id.in_(project_ids), Invoice.status == "issued")
                .order_by(Invoice.issue_date.desc().nullslast(), Invoice.created_at.desc())
                .all()) if project_ids else []
    return jsonify([{
        "id": inv.id, "file_name": f"Invoice {inv.invoice_number} — {inv.title}",
        "upload_date": inv.issue_date.isoformat() if inv.issue_date else fmt_dt(inv.created_at),
        "download_url": f"/api/client/invoices/{inv.id}/view",
    } for inv in invoices])


@app.route("/api/support/report-problem", methods=["POST"])
def support_report_problem():
    identifier = request.remote_addr or "unknown"
    if is_rate_limited(identifier, "support_submit"):
        return json_error("Too many reports submitted. Please try again later.", 429)
    record_attempt(identifier, "support_submit", success=False)

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return json_error("Describe the problem before submitting.", 400, errors={"message": "This field is required."})

    customer = current_customer()
    ticket = SupportTicket(
        customer_id=customer.id if customer else None,
        message=message[:4000],
        page_context=(data.get("page_context") or "")[:200] or None,
    )
    db.session.add(ticket)
    db.session.commit()

    notify_admins("support_ticket", "New support ticket",
                  message[:140], project=None)
    write_audit_log("support_ticket_submitted", "support_ticket", ticket.id,
                     actor=(customer.customer_id if customer else "anonymous"))
    return jsonify({"message": "Thanks — we've received your report and will follow up shortly."}), 201


@app.route("/api/client/documents/<int:document_id>/download", methods=["GET"])
def client_download_document(document_id):
    doc = Document.query.get_or_404(document_id)
    project = doc.project

    if doc.visibility != "client" or not client_can_access_project_file(project):
        return json_error("You do not have access to this document.", 403)

    return send_stored_file(doc.file_path, doc.original_name)


@app.route("/api/client/requirement-files/<int:file_id>/download", methods=["GET"])
def client_download_requirement_file(file_id):
    """Requirement submissions are inherently client-authored, so unlike
    Document there is no separate visibility flag to check — ownership of
    the parent project is the only gate."""
    rf = RequirementFile.query.get_or_404(file_id)
    project = rf.project

    if not client_can_access_project_file(project):
        return json_error("You do not have access to this file.", 403)

    return send_stored_file(rf.file_path, rf.original_name)


# ==============================================================================
# SECTION 13 — PROJECT DASHBOARD ENDPOINTS (project_details.html)
# ==============================================================================
@app.route("/api/client/projects/authenticate", methods=["POST"])
def project_authenticate():
    data = request.get_json(silent=True) or {}
    order_id = (data.get("order_id") or "").strip().upper()
    password = data.get("password") or ""

    errors = {}
    if not order_id:
        errors["order_id"] = "Enter your Order ID."
    if not password:
        errors["password"] = "Enter your password."
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    rate_key = f"{order_id}:{request.remote_addr}"
    if is_rate_limited(rate_key, "order_login"):
        return json_error("Too many attempts. Please try again in a few minutes.", 429)

    project = Project.query.filter_by(order_id=order_id).first()
    if not project or not project.customer:
        record_attempt(rate_key, "order_login", False)
        return json_error("We could not verify those details.", 404)

    customer = project.customer
    if customer.lockout_until and customer.lockout_until > _now():
        record_attempt(rate_key, "order_login", False)
        return json_error("We could not verify those details.", 401)

    if not check_password_hash(customer.password_hash, password):
        record_attempt(rate_key, "order_login", False)
        customer.failed_login_attempts = (customer.failed_login_attempts or 0) + 1
        if customer.failed_login_attempts >= 8:
            customer.lockout_until = _now() + timedelta(minutes=15)
        db.session.commit()
        return json_error("We could not verify those details. Check your Order ID and password.", 401)

    if not project.is_approved:
        record_attempt(rate_key, "order_login", True)
        return json_error("This project has not been approved yet.", 403, reason="not_approved")

    if customer.account_disabled:
        record_attempt(rate_key, "order_login", False)
        return json_error("This account has been disabled. Contact support for help.", 403)

    customer.failed_login_attempts = 0
    customer.lockout_until = None
    db.session.commit()
    record_attempt(rate_key, "order_login", True)

    session.permanent = True
    session["customer_id"] = customer.id
    session["session_version"] = customer.session_version
    session["authenticated_order_id"] = project.order_id

    return jsonify({"project": serialize_project_full(project)})


@app.route("/api/client/projects/session", methods=["GET"])
def project_session():
    order_id = session.get("authenticated_order_id")
    if not order_id:
        return jsonify({"authenticated": False, "project": None}), 401

    project = Project.query.filter_by(order_id=order_id).first()
    if not project:
        session.pop("authenticated_order_id", None)
        return jsonify({"authenticated": False, "project": None}), 404
    if not project.is_approved:
        return json_error("This project has not been approved yet.", 403, reason="not_approved")

    return jsonify({"authenticated": True, "project": serialize_project_full(project)})


@app.route("/api/client/projects/revisions", methods=["POST"])
@project_session_required
def project_submit_revision():
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    description = (data.get("description") or "").strip()

    if order_id and order_id != g.project.order_id:
        return json_error("Order mismatch.", 403)
    if not description:
        return json_error("Describe the revision you need.", 400,
                            errors={"description": "Describe the revision you need."})

    used = g.project.revisions.filter(Revision.status != "rejected").count()
    if used >= g.project.revision_total:
        return json_error("No revisions remaining on this project.", 400)

    revision = Revision(project_id=g.project.id, description=description, status="pending")
    db.session.add(revision)
    g.project.status = "On Revision"
    add_timeline_event(g.project.id, "Revision Requested", description[:200], "revision_requested")
    db.session.commit()

    write_audit_log("revision_requested", "project", g.project.order_id, after={"description": description})
    send_project_update_email(g.project.customer, f"Revision request received — {g.project.order_id}",
                "Our team will review your revision request shortly.")

    return jsonify({"message": "Revision request submitted.", "revision_id": revision.id}), 201


@app.route("/api/client/projects/revisions/uploads", methods=["POST"])
@project_session_required
def project_upload_revision_file():
    if "file" not in request.files:
        return json_error("No file provided.", 400)
    saved, error = save_upload(request.files["file"], "revisions")
    if error:
        return json_error(error, 400)

    # Files land against the most recent accepted (upload-enabled) revision;
    # if none is open yet they're still safely recorded against the project
    # and will be linked once the admin opens the upload window.
    open_revision = (g.project.revisions
                      .filter_by(upload_enabled=True)
                      .order_by(Revision.requested_at.desc())
                      .first())

    revision_file = RevisionFile(
        revision_id=open_revision.id if open_revision else None,
        project_id=g.project.id,
        original_name=saved["original_name"],
        stored_name=saved["stored_name"],
        size=saved["size"],
        mime_type=saved["mime_type"],
        file_path=saved["file_path"],
    )
    db.session.add(revision_file)
    db.session.commit()

    return jsonify({"file_id": revision_file.id, "file_name": saved["original_name"], "size": saved["size"]})


# ==============================================================================
# SECTION 13A — PROJECT WORKSPACE: MILESTONES / TASKS / REQUIREMENTS /
# APPROVALS / MESSAGES (project_details.html)
#
# All routes below use client_project_required, which accepts EITHER the
# account-level session OR the order-level session (see resolve_client_project()
# in Section 7B) — the dual-check pattern project_details.html's own comments
# ask for, so the page keeps working exactly as it does today (order-login)
# while also supporting a future account-session-only flow.
# ==============================================================================
@app.route("/api/client/projects/<order_id>/milestones", methods=["GET"])
@client_project_required
def client_project_milestones(order_id):
    milestones = g.project.milestones.order_by(Milestone.order_index.asc()).all()
    return jsonify([serialize_milestone(m) for m in milestones])


@app.route("/api/client/projects/<order_id>/tasks", methods=["GET"])
@client_project_required
def client_project_tasks(order_id):
    tasks = g.project.tasks.order_by(Task.order_index.asc()).all()
    return jsonify([serialize_task(t) for t in tasks])


@app.route("/api/client/projects/<order_id>/requirements", methods=["GET"])
@client_project_required
def client_project_requirements(order_id):
    reqs = g.project.requirements.order_by(Requirement.requested_at.desc()).all()
    return jsonify([serialize_requirement(r) for r in reqs])


@app.route("/api/client/projects/<order_id>/requirements", methods=["POST"])
@client_project_required
def client_submit_requirement(order_id):
    """Submits the client's response to whichever requirement is currently
    actionable (pending, or revision_required after admin feedback). An
    explicit requirement_id may be passed to target a specific one instead
    — otherwise the oldest actionable requirement is used, since the
    contract's POST body ({note}, multipart file) carries no id by default."""
    is_multipart = bool(request.files)
    note = (request.form.get("note") if is_multipart else (request.get_json(silent=True) or {}).get("note")) or ""
    note = note.strip()
    requirement_id = request.form.get("requirement_id") if is_multipart else (request.get_json(silent=True) or {}).get("requirement_id")

    query = g.project.requirements.filter(Requirement.status.in_(["pending", "revision_required"]))
    if requirement_id:
        requirement = query.filter(Requirement.id == requirement_id).first()
    else:
        requirement = query.order_by(Requirement.requested_at.asc()).first()

    if not requirement:
        return json_error("No requirement is currently awaiting your response.", 404)
    if not note and "file" not in request.files:
        return json_error("Add a note or a file before submitting.", 400)

    before_status = requirement.status
    requirement.note = note or requirement.note
    requirement.status = "submitted"
    requirement.submitted_at = _now()
    requirement.updated_at = _now()

    if "file" in request.files and request.files["file"].filename:
        saved, error = save_upload(request.files["file"], "requirements")
        if error:
            return json_error(error, 400)
        db.session.add(RequirementFile(
            requirement_id=requirement.id, project_id=g.project.id,
            original_name=saved["original_name"], stored_name=saved["stored_name"],
            size=saved["size"], mime_type=saved["mime_type"], file_path=saved["file_path"],
        ))

    db.session.commit()

    log_activity("requirement_submitted", f"Requirement submitted: {requirement.title}", note[:200],
                 "requirement", project=g.project, entity_type="requirement", entity_id=requirement.id,
                 before={"status": before_status}, after={"status": "submitted"},
                 actor=g.customer.customer_id)
    notify_admins("requirement_submitted", f"Requirement submitted on {g.project.order_id}",
                  requirement.title, project=g.project)

    return jsonify(serialize_requirement(requirement)), 201


@app.route("/api/client/projects/<order_id>/approvals", methods=["GET"])
@client_project_required
def client_project_approvals(order_id):
    approvals = g.project.approvals.order_by(Approval.submitted_at.desc()).all()
    return jsonify([serialize_approval(a) for a in approvals])


@app.route("/api/client/projects/<order_id>/approvals/<int:approval_id>/approve", methods=["POST"])
@client_project_required
def client_approve_approval(order_id, approval_id):
    approval = Approval.query.filter_by(id=approval_id, project_id=g.project.id).first()
    if not approval:
        return json_error("Approval not found.", 404)
    if approval.status != "pending":
        return json_error("This item has already been decided.", 409)

    approval.status = "approved"
    approval.decided_at = _now()
    db.session.commit()

    log_activity("approval_approved", f"Approved: {approval.item_title}", "", "approval",
                 project=g.project, entity_type="approval", entity_id=approval.id,
                 after={"status": "approved"}, actor=g.customer.customer_id)
    notify_admins("approval_approved", f"Approval granted on {g.project.order_id}",
                  approval.item_title, project=g.project)

    return jsonify(serialize_approval(approval))


@app.route("/api/client/projects/<order_id>/approvals/<int:approval_id>/request-changes", methods=["POST"])
@client_project_required
def client_request_approval_changes(order_id, approval_id):
    data = request.get_json(silent=True) or {}
    comment = (data.get("comment") or "").strip()
    if not comment:
        return json_error("Describe the changes you need.", 400, errors={"comment": "This field is required."})

    approval = Approval.query.filter_by(id=approval_id, project_id=g.project.id).first()
    if not approval:
        return json_error("Approval not found.", 404)
    if approval.status != "pending":
        return json_error("This item has already been decided.", 409)

    approval.status = "changes_requested"
    approval.comments = comment
    approval.decided_at = _now()
    db.session.commit()

    log_activity("approval_changes_requested", f"Changes requested: {approval.item_title}", comment[:200],
                 "approval", project=g.project, entity_type="approval", entity_id=approval.id,
                 after={"status": "changes_requested", "comment": comment}, actor=g.customer.customer_id)
    notify_admins("approval_changes_requested", f"Changes requested on {g.project.order_id}",
                  approval.item_title, project=g.project)

    return jsonify(serialize_approval(approval))


# ==============================================================================
# SECTION 13AA — PROJECT WORKSPACE: PAYMENT TRANSACTIONS (client-submitted,
# admin-verified — see PaymentTransaction model comment for why this is a
# separate ledger from Payment's agreed-terms fields).
# ==============================================================================
@app.route("/api/client/projects/<order_id>/payment-transactions", methods=["GET"])
@client_project_required
def client_list_payment_transactions(order_id):
    txns = g.project.payment_transactions.order_by(PaymentTransaction.submitted_at.desc()).all()
    return jsonify([serialize_payment_transaction(t) for t in txns])


@app.route("/api/client/projects/<order_id>/payment-transactions", methods=["POST"])
@client_project_required
def client_submit_payment_transaction(order_id):
    """Customer reports a payment they made (UPI/bank transfer/cash/card).
    This never marks money as verified — it only creates a 'submitted'
    ledger entry; only admin can move it to verified/rejected, and only a
    verified transaction ever changes the project's balance."""
    project = g.project
    if not project.payment:
        return json_error("This project doesn't have financial terms set up yet.", 409)

    is_multipart = bool(request.files) or request.content_type and "multipart" in request.content_type
    form = request.form if is_multipart else (request.get_json(silent=True) or {})

    errors = {}
    try:
        amount = float(form.get("amount"))
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors["amount"] = "Enter a valid payment amount."

    method = (form.get("method") or "").strip()
    if method not in PAYMENT_TXN_METHOD_LABELS:
        errors["method"] = "Select a payment method."

    payment_date = None
    if form.get("payment_date"):
        payment_date = parse_deadline_input(form.get("payment_date"))
        if not payment_date:
            errors["payment_date"] = "Enter a valid date."

    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    proof_document_id = None
    if "proof" in request.files and request.files["proof"].filename:
        saved, error = save_upload(request.files["proof"], "payments")
        if error:
            return json_error(error, 400)
        doc = Document(
            project_id=project.id, original_name=saved["original_name"], stored_name=saved["stored_name"],
            category="Payment Proof", uploader=g.customer.full_name if g.customer else "Customer",
            size=saved["size"], mime_type=saved["mime_type"], visibility="internal", file_path=saved["file_path"],
        )
        db.session.add(doc)
        db.session.flush()
        proof_document_id = doc.id

    txn = PaymentTransaction(
        project_id=project.id, amount_minor=to_minor_units(amount),
        currency_code=project.payment.currency_code or "INR",
        method=method, reference=(form.get("reference") or "").strip() or None,
        payment_date=payment_date, note=(form.get("note") or "").strip() or None,
        proof_document_id=proof_document_id, status="submitted", submitted_by="customer",
    )
    db.session.add(txn)
    db.session.commit()

    log_activity("payment_txn_submitted", f"Payment reported: {format_inr(amount)}",
                 f"Via {PAYMENT_TXN_METHOD_LABELS[method]} — pending verification.", "payment_submitted",
                 project=project, entity_type="payment_transaction", entity_id=txn.id,
                 after={"amount": amount, "method": method}, actor=g.customer.customer_id)
    notify_admins("payment_submitted", f"Payment reported on {project.order_id}",
                  f"{format_inr(amount)} via {PAYMENT_TXN_METHOD_LABELS[method]}", project=project)

    return jsonify(serialize_payment_transaction(txn)), 201


# ==============================================================================
# SECTION 13AB — PROJECT WORKSPACE: PROPOSALS (client-facing decisions)
# Only non-draft proposals are ever visible here — a draft is admin's
# internal working copy and was never sent.
# ==============================================================================
@app.route("/api/client/projects/<order_id>/proposals", methods=["GET"])
@client_project_required
def client_list_proposals(order_id):
    proposals = Proposal.query.filter_by(project_id=g.project.id).filter(
        Proposal.status != "draft"
    ).order_by(Proposal.proposal_number, Proposal.version.desc()).all()
    return jsonify([serialize_proposal(p, include_lines=False, viewer="client") for p in proposals])


@app.route("/api/client/projects/<order_id>/proposals/<proposal_number>", methods=["GET"])
@client_project_required
def client_get_proposal(proposal_number):
    proposal = Proposal.query.filter_by(
        proposal_number=proposal_number.upper(), project_id=g.project.id
    ).filter(Proposal.status != "draft").order_by(Proposal.version.desc()).first()
    if not proposal:
        return json_error("Proposal not found.", 404)

    if proposal.status == "sent":
        proposal.status = "viewed"
        proposal.viewed_at = _now()
        db.session.commit()

    return jsonify(serialize_proposal(proposal, viewer="client"))


@app.route("/api/client/projects/<order_id>/proposals/<proposal_number>/accept", methods=["POST"])
@client_project_required
def client_accept_proposal(proposal_number):
    proposal = Proposal.query.filter_by(
        proposal_number=proposal_number.upper(), project_id=g.project.id
    ).order_by(Proposal.version.desc()).first()
    if not proposal:
        return json_error("Proposal not found.", 404)
    if proposal.status not in ("sent", "viewed"):
        return json_error("This proposal can no longer be accepted.", 409)
    if proposal.validity_date and proposal.validity_date < date.today():
        return json_error("This proposal has expired — please ask Kytron for an updated one.", 409)

    proposal.status = "accepted"
    proposal.responded_at = _now()
    proposal.accepted_at = _now()
    db.session.commit()

    log_activity("proposal_accepted", f"Proposal {proposal.proposal_number} accepted",
                 f"{format_inr(from_minor_units(proposal.total_minor))} — awaiting project approval.", "proposal_accepted",
                 project=g.project, entity_type="proposal", entity_id=proposal.id, actor=g.customer.customer_id if g.customer else None)
    notify_admins("proposal_accepted", f"Proposal accepted on {g.project.order_id}",
                  f"{proposal.title} — {format_inr(from_minor_units(proposal.total_minor))}", project=g.project)

    return jsonify(serialize_proposal(proposal, viewer="client"))


@app.route("/api/client/projects/<order_id>/proposals/<proposal_number>/request-changes", methods=["POST"])
@client_project_required
def client_request_proposal_changes(proposal_number):
    proposal = Proposal.query.filter_by(
        proposal_number=proposal_number.upper(), project_id=g.project.id
    ).order_by(Proposal.version.desc()).first()
    if not proposal:
        return json_error("Proposal not found.", 404)
    if proposal.status not in ("sent", "viewed"):
        return json_error("This proposal can't be changed from its current status.", 409)

    data = request.get_json(silent=True) or {}
    comment = (data.get("comment") or "").strip()
    if not comment:
        return json_error("Let us know what you'd like changed.", 400, errors={"comment": "This field is required."})

    proposal.status = "changes_requested"
    proposal.responded_at = _now()
    proposal.customer_comment = comment
    db.session.commit()

    log_activity("proposal_changes_requested", f"Changes requested: {proposal.proposal_number}", comment[:200],
                 "proposal_changes_requested", project=g.project, entity_type="proposal", entity_id=proposal.id,
                 actor=g.customer.customer_id if g.customer else None)
    notify_admins("proposal_changes_requested", f"Changes requested on {g.project.order_id}",
                  comment[:200], project=g.project)

    return jsonify(serialize_proposal(proposal, viewer="client"))


@app.route("/api/client/projects/<order_id>/proposals/<proposal_number>/reject", methods=["POST"])
@client_project_required
def client_reject_proposal(proposal_number):
    proposal = Proposal.query.filter_by(
        proposal_number=proposal_number.upper(), project_id=g.project.id
    ).order_by(Proposal.version.desc()).first()
    if not proposal:
        return json_error("Proposal not found.", 404)
    if proposal.status not in ("sent", "viewed"):
        return json_error("This proposal can't be rejected from its current status.", 409)

    data = request.get_json(silent=True) or {}
    proposal.status = "rejected"
    proposal.responded_at = _now()
    proposal.rejected_at = _now()
    proposal.customer_comment = (data.get("comment") or "").strip() or None
    db.session.commit()

    log_activity("proposal_rejected", f"Proposal {proposal.proposal_number} rejected",
                 proposal.customer_comment or "", "proposal_rejected", project=g.project,
                 entity_type="proposal", entity_id=proposal.id, actor=g.customer.customer_id if g.customer else None)
    notify_admins("proposal_rejected", f"Proposal rejected on {g.project.order_id}",
                  proposal.customer_comment or proposal.title, project=g.project)

    return jsonify(serialize_proposal(proposal, viewer="client"))


@app.route("/api/client/proposals/<int:proposal_id>/view", methods=["GET"])
def client_view_proposal(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.status == "draft":
        return json_error("Proposal not found.", 404)
    project, err = resolve_client_project(proposal.project.order_id if proposal.project else "")
    if err:
        return err
    return render_proposal_html(proposal)


# ==============================================================================
# SECTION 13BB — PROJECT WORKSPACE: INVOICES (client-facing, Phase 8)
# Only issued/cancelled invoices are ever visible — a draft is admin's
# internal working copy, exactly like a draft Proposal.
# ==============================================================================
@app.route("/api/client/projects/<order_id>/invoices", methods=["GET"])
@client_project_required
def client_list_invoices(order_id):
    invoices = Invoice.query.filter_by(project_id=g.project.id).filter(Invoice.status != "draft").order_by(Invoice.created_at.desc()).all()
    return jsonify([serialize_invoice(i) for i in invoices])


@app.route("/api/client/invoices/<int:invoice_id>/view", methods=["GET"])
def client_view_invoice(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    if inv.status == "draft":
        return json_error("Invoice not found.", 404)
    project, err = resolve_client_project(inv.project.order_id if inv.project else "")
    if err:
        return err
    return render_invoice_html(inv)


@app.route("/api/client/payment-transactions/<int:txn_id>/receipt", methods=["GET"])
def client_payment_receipt(txn_id):
    txn = PaymentTransaction.query.get_or_404(txn_id)
    project, err = resolve_client_project(txn.project.order_id if txn.project else "")
    if err:
        return err
    if txn.status != "verified":
        return json_error("A receipt is only available once a payment has been verified.", 409)
    return render_payment_receipt_html(txn, project)


@app.route("/api/client/projects/<order_id>/messages", methods=["GET"])
@client_project_required
def client_project_messages(order_id):
    convo = get_project_conversation(g.project)
    msgs = convo.messages.order_by(Message.created_at.asc()).all()
    mark_messages_read(convo, "customer")
    return jsonify([serialize_message(m, "customer") for m in msgs])


@app.route("/api/client/projects/<order_id>/messages", methods=["POST"])
@client_project_required
def client_send_project_message(order_id):
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return json_error("Message can't be empty.", 400, errors={"body": "This field is required."})

    msg = send_message(g.project, "customer", g.customer.full_name, body)
    return jsonify(serialize_message(msg, "customer")), 201


# ==============================================================================
# SECTION 13BA — PROJECT WORKSPACE: CHANGE REQUESTS (client-facing, Phase 6)
# ==============================================================================
@app.route("/api/client/projects/<order_id>/change-requests", methods=["GET"])
@client_project_required
def client_list_change_requests(order_id):
    items = ChangeRequest.query.filter_by(project_id=g.project.id).order_by(ChangeRequest.created_at.desc()).all()
    return jsonify([serialize_change_request(c, viewer="client") for c in items])


@app.route("/api/client/projects/<order_id>/change-requests", methods=["POST"])
@client_project_required
def client_create_change_request(order_id):
    """A formal, structured request — distinct from an ordinary message.
    Sender is always the authenticated customer; there is no way to submit
    this as anyone else."""
    project = g.project
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:200]
    description = (data.get("description") or "").strip()
    cr_type = (data.get("type") or "other").strip()
    errors = {}
    if not title:
        errors["title"] = "Give it a short title."
    if not description:
        errors["description"] = "Describe what you'd like changed."
    if cr_type not in CHANGE_REQUEST_TYPE_LABELS:
        cr_type = "other"
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    cr = ChangeRequest(
        change_request_id=gen_change_request_id(), project_id=project.id, title=title,
        description=description, type=cr_type, status="submitted", submitted_by="customer",
    )
    db.session.add(cr)
    db.session.commit()

    log_activity("change_request_created", f"New request: {title}", description[:200], "change_request",
                 project=project, entity_type="change_request", entity_id=cr.id,
                 after={"type": cr_type}, actor=g.customer.customer_id if g.customer else None)
    notify_admins("change_request_created", f"New change request on {project.order_id}", title, project=project)

    return jsonify(serialize_change_request(cr, viewer="client")), 201


@app.route("/api/client/projects/<order_id>/change-requests/<change_request_id>", methods=["GET"])
@client_project_required
def client_get_change_request(change_request_id):
    cr = ChangeRequest.query.filter_by(change_request_id=change_request_id.upper(), project_id=g.project.id).first()
    if not cr:
        return json_error("Change request not found.", 404)
    return jsonify(serialize_change_request(cr, viewer="client"))


# ==============================================================================
# SECTION 13BC — PROJECT WORKSPACE: SUPPORT REQUESTS (client-facing, Phase 9)
# Does not touch project.status at all — creating or resolving a support
# request never reopens or advances a completed project (9B/9AF). Never
# creates chargeable work automatically (9R) — only admin can classify and
# bridge to a ChangeRequest.
# ==============================================================================
@app.route("/api/client/projects/<order_id>/support", methods=["GET"])
@client_project_required
def client_list_support_tickets(order_id):
    items = SupportTicket.query.filter_by(project_id=g.project.id).order_by(SupportTicket.created_at.desc()).all()
    return jsonify([serialize_support_ticket(t, viewer="client") for t in items])


@app.route("/api/client/projects/<order_id>/support", methods=["POST"])
@client_project_required
def client_create_support_ticket(order_id):
    project = g.project
    identifier = g.customer.customer_id if g.customer else (request.remote_addr or "unknown")
    if is_rate_limited(identifier, "support_submit"):
        return json_error("Too many support requests submitted. Please try again later.", 429)
    record_attempt(identifier, "support_submit", success=False)

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:200]
    message = (data.get("message") or "").strip()
    if not title:
        return json_error("Give it a short title.", 400, errors={"title": "This field is required."})
    if not message:
        return json_error("Describe what's happening.", 400, errors={"message": "This field is required."})

    # Priority is never taken from the customer — always starts at
    # 'normal' regardless of what's in the request body (9G: reserve real
    # urgency classification for admin).
    ticket = SupportTicket(
        customer_id=g.customer.id if g.customer else None, project_id=project.id,
        title=title, message=message[:4000], priority="normal", status="submitted",
    )
    db.session.add(ticket)
    db.session.commit()

    notify_admins("support_ticket_created", f"Support request on {project.order_id}", title, project=project)
    write_audit_log("support_ticket_submitted", "support_ticket", ticket.id,
                     actor=g.customer.customer_id if g.customer else "anonymous")
    return jsonify(serialize_support_ticket(ticket, viewer="client")), 201


@app.route("/api/client/support/<int:ticket_id>", methods=["GET"])
@login_required
def client_get_support_ticket(ticket_id):
    ticket = SupportTicket.query.filter_by(id=ticket_id, customer_id=g.customer.id).first()
    if not ticket:
        return json_error("Support request not found.", 404)
    return jsonify(serialize_support_ticket(ticket, viewer="client"))


@app.route("/api/client/support/<int:ticket_id>/reopen", methods=["POST"])
@login_required
def client_reopen_support_ticket(ticket_id):
    """Only a resolved/closed request can be reopened, and only its own
    customer can do it — the reopen itself is audited, and prior
    resolution history (admin_response, resolved_at/by) is preserved, not
    cleared (9P)."""
    ticket = SupportTicket.query.filter_by(id=ticket_id, customer_id=g.customer.id).first()
    if not ticket:
        return json_error("Support request not found.", 404)
    if ticket.status not in SUPPORT_RESOLVED_STATUSES:
        return json_error("Only a resolved request can be reopened.", 409)

    before = {"status": ticket.status}
    ticket.status = "reopened"
    db.session.commit()

    write_audit_log("support_ticket_reopened", "support_ticket", ticket.id, before=before, after={"status": "reopened"},
                     actor=g.customer.customer_id)
    notify_admins("support_ticket_reopened", f"Support request reopened: {ticket.title or gen_support_ticket_ref(ticket)}",
                  ticket.message[:140], project=ticket.project)
    return jsonify(serialize_support_ticket(ticket, viewer="client"))


# ==============================================================================
# SECTION 13B — ADMIN PANEL: AUTH
# ==============================================================================
@app.route("/api/admin/auth/session", methods=["GET"])
def admin_auth_session():
    admin = current_admin()
    if not admin:
        return jsonify({"logged_in": False, "admin": None}), 401
    return jsonify({"logged_in": True, "admin": admin.to_public_dict()})


@app.route("/api/admin/auth/login", methods=["POST"])
def admin_auth_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    errors = {}
    if not is_valid_email(email):
        errors["email"] = "Enter a valid email address."
    if not password:
        errors["password"] = "Enter your password."
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    rate_key = f"admin:{email}:{request.remote_addr}"
    if is_rate_limited(rate_key, "admin_login"):
        return json_error("Too many attempts. Please try again in a few minutes.", 429)

    admin = Admin.query.filter_by(email=email).first()
    if not admin or not admin.is_active or (admin.lockout_until and admin.lockout_until > _now()):
        record_attempt(rate_key, "admin_login", False)
        return json_error("We could not log you in. Check your credentials and try again.", 401)

    if not check_password_hash(admin.password_hash, password):
        record_attempt(rate_key, "admin_login", False)
        admin.failed_login_attempts = (admin.failed_login_attempts or 0) + 1
        if admin.failed_login_attempts >= 8:
            admin.lockout_until = _now() + timedelta(minutes=15)
        db.session.commit()
        return json_error("We could not log you in. Check your credentials and try again.", 401)

    admin.failed_login_attempts = 0
    admin.lockout_until = None
    admin.last_login_at = _now()
    db.session.commit()
    record_attempt(rate_key, "admin_login", True)

    # Deliberately a separate session key from customer_id / authenticated_order_id
    # so admin and client-portal sessions never collide in the same browser.
    session.permanent = True
    session["admin_id"] = admin.id
    session["admin_session_version"] = admin.session_version

    return jsonify({"admin": admin.to_public_dict()})


@app.route("/api/admin/auth/logout", methods=["POST"])
@admin_required
def admin_auth_logout():
    session.pop("admin_id", None)
    session.pop("admin_session_version", None)
    return jsonify({"message": "Logged out."})


@app.route("/api/admin/auth/logout-all-sessions", methods=["POST"])
@admin_required
def admin_auth_logout_all_sessions():
    g.admin.session_version = (g.admin.session_version or 1) + 1
    db.session.commit()
    write_audit_log("logout_all_sessions", "admin", g.admin.admin_id, actor=g.admin.admin_id)
    session.pop("admin_id", None)
    session.pop("admin_session_version", None)
    return jsonify({"message": "You have been signed out of all devices."})


@app.route("/api/admin/account/profile", methods=["PATCH"])
@admin_required
def admin_update_profile():
    data = request.get_json(silent=True) or {}
    errors = {}

    name = data.get("name")
    if name is not None and not is_valid_name(clean_name(name)):
        errors["name"] = "Enter a valid name."
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    before = {"name": g.admin.full_name, "phone": g.admin.phone}
    if name is not None:
        g.admin.full_name = clean_name(name)
    if "phone" in data:
        g.admin.phone = (data.get("phone") or "").strip() or None
    db.session.commit()

    write_audit_log("admin_profile_updated", "admin", g.admin.admin_id, before=before, after=data, actor=g.admin.admin_id)
    return jsonify(g.admin.to_public_dict())


@app.route("/api/admin/account/change-password", methods=["POST"])
@admin_required
def admin_change_password():
    data = request.get_json(silent=True) or {}
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""

    if not check_password_hash(g.admin.password_hash, current_password):
        return json_error("Current password is incorrect.", 401, errors={"current_password": "Incorrect password."})
    if len(new_password) < 8:
        return json_error("Password must be at least 8 characters.", 400,
                            errors={"new_password": "Password must be at least 8 characters."})

    g.admin.password_hash = generate_password_hash(new_password)
    g.admin.must_change_password = False
    db.session.commit()

    write_audit_log("admin_password_changed", "admin", g.admin.admin_id, actor=g.admin.admin_id)
    return jsonify({"message": "Password updated."})


# ==============================================================================
# SECTION 13C — ADMIN PANEL: DASHBOARD
# ==============================================================================
@app.route("/api/admin/dashboard/stats", methods=["GET"])
@admin_required
def admin_dashboard_stats():
    approved = Project.query.filter_by(is_approved=True)

    stats = {
        "total_customers": Customer.query.count(),
        "total_projects": Project.query.count(),
        "new_requests": Project.query.filter_by(is_approved=False, status="Registered").count(),
        "active_projects": approved.filter(Project.status.in_(["Under Process", "Testing Phase", "On Revision"])).count(),
        "under_process": approved.filter_by(status="Under Process").count(),
        "testing_phase": approved.filter_by(status="Testing Phase").count(),
        "on_revision": approved.filter_by(status="On Revision").count(),
        "completed": approved.filter_by(status="Completed").count(),
        "cancelled": Project.query.filter_by(status="Cancelled").count(),
        "pending_payments": (approved.join(Payment)
                             .filter(Payment.status.in_(["pending", "partial"])).count()),
        "active_support_cases": approved.filter_by(support_status="active").count(),
    }

    # --- Deadline Management summary cards ---
    today = date.today()
    week_end = today + timedelta(days=7)
    active_with_deadline = approved.filter(
        Project.status.notin_(["Completed", "Cancelled"]), Project.deadline_date.isnot(None)
    )
    stats["due_today"] = active_with_deadline.filter(Project.deadline_date == today).count()
    stats["due_this_week"] = active_with_deadline.filter(
        Project.deadline_date >= today, Project.deadline_date <= week_end
    ).count()
    stats["overdue"] = active_with_deadline.filter(Project.deadline_date < today).count()

    return jsonify(stats)


# ==============================================================================
# SECTION 13D — ADMIN PANEL: PROJECT REQUESTS (new / unapproved submissions)
# ==============================================================================
@app.route("/api/admin/project-requests", methods=["GET"])
@admin_required
def admin_list_project_requests():
    requests_query = (Project.query
                       .filter_by(is_approved=False, status="Registered")
                       .order_by(Project.created_at.desc()))
    return jsonify([serialize_project_request(p) for p in requests_query.all()])


@app.route("/api/admin/project-requests/<order_id>", methods=["GET"])
@admin_required
def admin_get_project_request(order_id):
    project = Project.query.filter_by(order_id=order_id.upper()).first()
    if not project:
        return json_error("Project request not found.", 404)
    return jsonify(serialize_project_request_detail(project))


@app.route("/api/admin/project-requests/<order_id>/accept", methods=["POST"])
@admin_required
def admin_accept_project_request(order_id):
    project = Project.query.filter_by(order_id=order_id.upper()).first()
    if not project:
        return json_error("Project request not found.", 404)
    if project.is_approved:
        return json_error("This project has already been accepted.", 409)
    if project.status != "Registered":
        return json_error("This request is no longer pending (already rejected/cancelled).", 409)

    data = request.get_json(silent=True) or {}
    errors = {}

    # Optional: source commercial terms from an accepted proposal instead of
    # requiring admin to retype them. Read-only — this never writes back to
    # the Proposal, and admin can still override any individual field by
    # supplying it explicitly in the request body. See Section 3AC docstring
    # for why this "informs, never writes" design keeps this route as the
    # single remaining approval boundary.
    source_proposal = None
    if data.get("proposal_id"):
        source_proposal = Proposal.query.filter_by(id=data["proposal_id"], project_id=project.id).first()
        if not source_proposal:
            return json_error("That proposal doesn't belong to this project.", 400, errors={"proposal_id": "Not found."})
        if source_proposal.status != "accepted":
            return json_error("Only an accepted proposal can be used to prefill acceptance terms.", 409)

    def _from_body_or_proposal(key, proposal_value):
        return data[key] if key in data else proposal_value

    final_cost = _from_body_or_proposal(
        "final_cost", from_minor_units(source_proposal.total_minor) if source_proposal else None)
    approved_deadline_raw = (data.get("approved_deadline") or "").strip()
    approved_features = data.get("approved_features")
    if approved_features is None:
        approved_features = ([d.strip() for d in source_proposal.deliverables.split("\n") if d.strip()]
                              if source_proposal and source_proposal.deliverables else [])
    advance_percentage = data.get("advance_percentage", 0)
    support_duration_label = (_from_body_or_proposal(
        "support_duration_label", source_proposal.support_duration_label if source_proposal else "") or "").strip()
    revision_count = _from_body_or_proposal(
        "revision_count", source_proposal.revision_count if source_proposal else None)
    revision_window_label = (_from_body_or_proposal(
        "revision_window_label", source_proposal.revision_window_label if source_proposal else "") or "").strip()
    initial_status = data.get("initial_status") or "Under Process"
    priority = (data.get("priority") or "normal").strip().lower()

    try:
        final_cost = float(final_cost)
        if final_cost < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors["final_cost"] = "Enter a valid final cost."

    try:
        advance_percentage = float(advance_percentage)
        if not (0 <= advance_percentage <= 100):
            raise ValueError
    except (TypeError, ValueError):
        errors["advance_percentage"] = "Advance percentage must be between 0 and 100."

    try:
        revision_count = int(revision_count)
        if revision_count < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors["revision_count"] = "Enter a valid revision count."

    deadline_date = parse_deadline_input(approved_deadline_raw)
    if not approved_deadline_raw or not deadline_date:
        errors["approved_deadline"] = "Set a valid approved deadline."
    if not isinstance(approved_features, list):
        errors["approved_features"] = "Approved features must be a list."
    if initial_status not in STATUS_LABELS or initial_status == "Registered":
        errors["initial_status"] = "Choose a valid initial status."
    if priority not in {"low", "normal", "high", "urgent"}:
        priority = "normal"
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    approved_deadline_label = deadline_date.strftime("%b %d, %Y")
    before = {"status": project.status, "is_approved": project.is_approved}

    project.is_approved = True
    project.status = initial_status
    project.final_deadline = approved_deadline_label
    project.deadline_date = deadline_date
    project.priority = priority
    project.approved_features = json.dumps(approved_features)
    project.revision_total = revision_count
    project.revision_window_label = revision_window_label or None
    project.support_duration_label = support_duration_label or None
    project.support_status = "active" if support_duration_label else "not_started"

    advance_amount = round(final_cost * (advance_percentage / 100.0), 2)
    payment = project.payment
    if not payment:
        payment = Payment(project_id=project.id)
        db.session.add(payment)
    payment.final_cost = final_cost
    payment.advance_percentage = advance_percentage
    payment.advance_amount = advance_amount
    recompute_payment_balance(payment)  # was a separate, narrower formula (omitted extra/addon/revision
                                         # charges) — now the same shared logic used everywhere else

    add_timeline_event(project.id, "Project Accepted",
                        f"Your project was approved. Deadline: {approved_deadline_label}.", "project_accepted")
    db.session.commit()

    write_audit_log("project_accepted", "project", project.order_id, before=before, after={
        "status": project.status, "final_cost": final_cost, "advance_percentage": advance_percentage,
        "approved_deadline": approved_deadline_label, "revision_count": revision_count,
        "sourced_from_proposal": source_proposal.proposal_number if source_proposal else None,
    }, actor=g.admin.admin_id)

    if project.customer:
        send_project_update_email(project.customer, f"Your project has been approved — {project.order_id}",
                    "Great news — your project has been reviewed and approved. "
                    "Sign in to your Project Dashboard to see full details.")

    return jsonify({"message": "Project accepted.", "project": serialize_project_request_detail(project)})


@app.route("/api/admin/project-requests/<order_id>/reject", methods=["POST"])
@admin_required
def admin_reject_project_request(order_id):
    project = Project.query.filter_by(order_id=order_id.upper()).first()
    if not project:
        return json_error("Project request not found.", 404)
    if project.is_approved:
        return json_error("This project has already been accepted; it cannot be rejected.", 409)
    if project.status != "Registered":
        return json_error("This request is no longer pending.", 409)

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()

    before = {"status": project.status}
    project.status = "Cancelled"
    if reason:
        note = f"[Request rejected] {reason}"
        project.admin_notes = f"{project.admin_notes}\n{note}" if project.admin_notes else note
    db.session.commit()

    write_audit_log("project_rejected", "project", project.order_id, before=before,
                     after={"status": "Cancelled", "reason": reason or None}, actor=g.admin.admin_id)

    if project.customer:
        send_project_update_email(project.customer, f"Update on your project request — {project.order_id}",
                    "After review, we're unable to move forward with this project request. "
                    "Please reach out if you'd like to discuss further.")

    return jsonify({"message": "Project request rejected."})


# ==============================================================================
# SECTION 13E — ADMIN PANEL: PROJECTS WORKSPACE
# ==============================================================================
PROJECT_DEADLINE_FILTERS = {"all", "due_soon", "critical", "due_today", "overdue"}


@app.route("/api/admin/projects", methods=["GET"])
@admin_required
def admin_list_projects():
    query = Project.query.filter_by(is_approved=True)

    status_filter = (request.args.get("status") or "").strip()
    if status_filter and status_filter in STATUS_LABELS:
        query = query.filter(Project.status == status_filter)

    search = (request.args.get("search") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.join(Customer, isouter=True).filter(
            db.or_(Project.order_id.ilike(like), Project.project_name.ilike(like),
                   Customer.full_name.ilike(like), Customer.customer_id.ilike(like))
        )

    deadline_filter = (request.args.get("deadline_filter") or "all").strip()
    if deadline_filter not in PROJECT_DEADLINE_FILTERS:
        deadline_filter = "all"

    projects = query.order_by(Project.updated_at.desc()).all()

    if deadline_filter != "all":
        projects = [p for p in projects if compute_deadline_bucket(p.deadline_date)[0] == deadline_filter]

    return jsonify([serialize_project_admin_summary(p) for p in projects])


@app.route("/api/admin/projects/<order_id>", methods=["GET"])
@admin_required
def admin_get_project_workspace(order_id):
    project = Project.query.filter_by(order_id=order_id.upper(), is_approved=True).first()
    if not project:
        return json_error("Project not found.", 404)
    return jsonify(serialize_project_workspace(project))


@app.route("/api/admin/projects/<order_id>", methods=["PATCH"])
@admin_required
def admin_update_project(order_id):
    """Backs both the Project Details tab and the Project Settings tab —
    both edit the same underlying fields, just grouped differently in the UI."""
    project = Project.query.filter_by(order_id=order_id.upper(), is_approved=True).first()
    if not project:
        return json_error("Project not found.", 404)

    data = request.get_json(silent=True) or {}
    errors = {}
    before = {
        "status": project.status, "deadline_date": project.deadline_date.isoformat() if project.deadline_date else None,
        "revision_total": project.revision_total,
    }
    timeline_notes = []

    if "status" in data:
        new_status = data["status"]
        if new_status not in STATUS_LABELS or new_status == "Registered":
            errors["status"] = "Choose a valid status."
        elif new_status != project.status:
            project.status = new_status
            timeline_notes.append(("Status Changed", f"Status changed to {STATUS_LABELS[new_status]}.", "status_changed"))
            if new_status == "Completed":
                timeline_notes.append(("Project Completed", "The project was marked complete.", "project_completed"))

    if "deadline_date" in data:
        new_deadline = parse_deadline_input(data["deadline_date"])
        if data["deadline_date"] and not new_deadline:
            errors["deadline_date"] = "Enter a valid date."
        elif new_deadline != project.deadline_date:
            project.deadline_date = new_deadline
            project.final_deadline = new_deadline.strftime("%b %d, %Y") if new_deadline else None
            timeline_notes.append(("Deadline Changed",
                                    f"Deadline updated to {project.final_deadline or 'unset'}.", "deadline_changed"))

    if "support_duration_label" in data:
        project.support_duration_label = (data["support_duration_label"] or "").strip() or None
        project.support_status = "active" if project.support_duration_label else "not_started"

    if "revision_count" in data:
        try:
            revision_count = int(data["revision_count"])
            if revision_count < 0:
                raise ValueError
            project.revision_total = revision_count
        except (TypeError, ValueError):
            errors["revision_count"] = "Enter a valid revision count."

    if "revision_window_label" in data:
        project.revision_window_label = (data["revision_window_label"] or "").strip() or None

    if "approved_features" in data:
        if isinstance(data["approved_features"], list):
            project.approved_features = json.dumps(data["approved_features"])
        else:
            errors["approved_features"] = "Approved features must be a list."

    if "priority" in data:
        if data["priority"] in {"low", "normal", "high", "urgent"}:
            project.priority = data["priority"]
        else:
            errors["priority"] = "Choose a valid priority."

    if "admin_notes" in data:
        project.admin_notes = (data["admin_notes"] or "").strip() or None

    if "handover_url" in data:
        project.handover_url = (data["handover_url"] or "").strip()[:500] or None
    if "handover_notes" in data:
        # Operational pointers only (domain/hosting/where-things-live) —
        # never credentials. No secrets-vault exists in this app, so no
        # field for storing passwords has been added here or anywhere
        # else, per the explicit Phase 7T security requirement.
        project.handover_notes = (data["handover_notes"] or "").strip() or None

    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    for title, desc, event_type in timeline_notes:
        add_timeline_event(project.id, title, desc, event_type)
    db.session.commit()

    write_audit_log("project_updated", "project", project.order_id, before=before, after=data, actor=g.admin.admin_id)

    return jsonify({"message": "Project updated.", "project": serialize_project_workspace(project)})


@app.route("/api/admin/projects/<order_id>/delivery-readiness", methods=["GET"])
@admin_required
def admin_project_delivery_readiness(order_id):
    project = Project.query.filter_by(order_id=order_id.upper(), is_approved=True).first()
    if not project:
        return json_error("Project not found.", 404)
    return jsonify(compute_delivery_readiness(project))


@app.route("/api/admin/projects/<order_id>/deliver", methods=["POST"])
@admin_required
def admin_deliver_project(order_id):
    """Records the delivery EVENT (7R) — deliberately distinct from just
    setting status='Delivered' via the generic PATCH route above, so
    delivered_at/delivered_by/delivery_notes are always captured together
    and stay consistent. Does not hard-block on readiness (7P) — the
    factual blockers are returned either way so admin can make an informed
    call, but the decision stays with admin, not an invented business rule."""
    project = Project.query.filter_by(order_id=order_id.upper(), is_approved=True).first()
    if not project:
        return json_error("Project not found.", 404)
    if project.status in ("Delivered", "Completed", "Cancelled"):
        return json_error(f"This project is already {project.status.lower()}.", 409)

    data = request.get_json(silent=True) or {}
    readiness = compute_delivery_readiness(project)

    before = {"status": project.status}
    project.status = "Delivered"
    project.delivered_at = _now()
    project.delivered_by = g.admin.full_name
    project.delivery_notes = (data.get("delivery_notes") or "").strip() or None
    db.session.commit()

    add_timeline_event(project.id, "Project Delivered",
                       project.delivery_notes or "Your project has been delivered.", "project_delivered")
    write_audit_log("project_delivered", "project", project.order_id, before=before,
                     after={"status": "Delivered", "readiness_blockers": readiness["blockers"]}, actor=g.admin.admin_id)
    if project.customer:
        send_project_update_email(project.customer, f"Your project has been delivered — {project.order_id}",
                    "Your project has been delivered. Sign in to your Project Dashboard for details.")

    return jsonify({"message": "Project marked delivered.", "project": serialize_project_workspace(project)})


@app.route("/api/admin/projects/<order_id>/complete", methods=["POST"])
@admin_required
def admin_complete_project(order_id):
    """The true closure boundary (7S) — only reachable from Delivered, so
    'delivered' and 'fully closed out' can never be conflated."""
    project = Project.query.filter_by(order_id=order_id.upper(), is_approved=True).first()
    if not project:
        return json_error("Project not found.", 404)
    if project.status != "Delivered":
        return json_error("Only a delivered project can be marked completed.", 409)

    before = {"status": project.status}
    project.status = "Completed"
    db.session.commit()

    add_timeline_event(project.id, "Project Completed", "The project was marked complete.", "project_completed")
    write_audit_log("project_completed", "project", project.order_id, before=before,
                     after={"status": "Completed"}, actor=g.admin.admin_id)

    return jsonify({"message": "Project marked completed.", "project": serialize_project_workspace(project)})


@app.route("/api/admin/projects/<order_id>/payment", methods=["PATCH"])
@admin_required
def admin_update_project_payment(order_id):
    project = Project.query.filter_by(order_id=order_id.upper(), is_approved=True).first()
    if not project:
        return json_error("Project not found.", 404)

    payment = project.payment
    if not payment:
        payment = Payment(project_id=project.id)
        db.session.add(payment)
        db.session.flush()

    data = request.get_json(silent=True) or {}
    errors = {}
    has_transactions = project.payment_transactions.count() > 0
    numeric_fields = {
        "final_cost": "final_cost", "advance_paid": "advance_paid",
        "extra_charges": "extra_charges", "addon_charges": "addon_charges",
        "revision_charges": "revision_charges",
    }
    before = {f: getattr(payment, f) for f in numeric_fields}

    for field in numeric_fields:
        if field in data:
            if field == "advance_paid" and has_transactions:
                # Derived from verified transactions once any exist on this
                # project — see sync_payment_advance_from_transactions().
                # Silently ignored rather than erroring, so older admin UI
                # flows that still submit this field don't break.
                continue
            try:
                value = float(data[field])
                if value < 0:
                    raise ValueError
                setattr(payment, field, value)
            except (TypeError, ValueError):
                errors[field] = "Enter a valid non-negative amount."

    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    recompute_payment_balance(payment)
    if "status" in data and data["status"] in PAYMENT_STATUS_LABELS:
        payment.status = data["status"]  # explicit admin override (e.g. marking "overdue") wins over the derived status

    add_timeline_event(project.id, "Payment Updated", "Payment details were updated by the admin team.", "payment_updated")
    db.session.commit()

    write_audit_log("payment_updated", "project", project.order_id, before=before, after=data, actor=g.admin.admin_id)

    return jsonify({"message": "Payment updated.", "payment": serialize_payment(payment)})


# ==============================================================================
# SECTION 13EAB — ADMIN PANEL: PAYMENT TRANSACTIONS
# ==============================================================================
@app.route("/api/admin/projects/<order_id>/payment-transactions", methods=["GET"])
@admin_required
@admin_project_required
def admin_list_payment_transactions(order_id):
    txns = g.project.payment_transactions.order_by(PaymentTransaction.submitted_at.desc()).all()
    return jsonify([serialize_payment_transaction(t) for t in txns])


@app.route("/api/admin/projects/<order_id>/payment-transactions", methods=["POST"])
@admin_required
@admin_project_required
def admin_record_payment_transaction(order_id):
    """Admin records a payment on the customer's behalf (e.g. cash handed
    over in person, or a bank transfer confirmed by phone). Auto-verified —
    there's no one else who needs to review an admin's own entry — but
    still goes through the same balance recompute and audit trail as a
    customer-submitted-then-verified transaction."""
    project = g.project
    if not project.payment:
        return json_error("Set up financial terms for this project first.", 409)

    data = request.get_json(silent=True) or {}
    errors = {}
    try:
        amount = float(data.get("amount"))
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors["amount"] = "Enter a valid payment amount."
    method = (data.get("method") or "").strip()
    if method not in PAYMENT_TXN_METHOD_LABELS:
        errors["method"] = "Select a payment method."
    payment_date = None
    if data.get("payment_date"):
        payment_date = parse_deadline_input(data.get("payment_date"))
        if not payment_date:
            errors["payment_date"] = "Enter a valid date."
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    invoice_id = None
    if data.get("invoice_id"):
        invoice = Invoice.query.filter_by(id=data["invoice_id"], project_id=project.id).first()
        if not invoice:
            return json_error("That invoice doesn't belong to this project.", 400, errors={"invoice_id": "Not found."})
        invoice_id = invoice.id

    txn = PaymentTransaction(
        project_id=project.id, amount_minor=to_minor_units(amount),
        currency_code=project.payment.currency_code or "INR",
        method=method, reference=(data.get("reference") or "").strip() or None,
        payment_date=payment_date, note=(data.get("note") or "").strip() or None,
        status="verified", submitted_by="admin", invoice_id=invoice_id,
        reviewed_by=g.admin.full_name, reviewed_at=_now(),
        receipt_number=gen_receipt_number(),
    )
    db.session.add(txn)
    db.session.flush()
    sync_payment_advance_from_transactions(project)
    db.session.commit()

    log_activity("payment_txn_recorded", f"Payment recorded: {format_inr(amount)}",
                 f"Via {PAYMENT_TXN_METHOD_LABELS[method]}, recorded by admin.", "payment_verified",
                 project=project, entity_type="payment_transaction", entity_id=txn.id,
                 after={"amount": amount, "method": method}, actor=g.admin.admin_id)
    notify_customer(project.customer, "payment_verified", f"Payment recorded on {project.order_id}",
                    format_inr(amount), project=project)

    return jsonify(serialize_payment_transaction(txn)), 201


@app.route("/api/admin/payment-transactions/<int:txn_id>/verify", methods=["POST"])
@admin_required
def admin_verify_payment_transaction(txn_id):
    txn = PaymentTransaction.query.get_or_404(txn_id)
    if txn.status != "submitted":
        return json_error("Only a submitted transaction can be verified.", 409)
    project = txn.project

    data = request.get_json(silent=True) or {}
    if data.get("invoice_id"):
        invoice = Invoice.query.filter_by(id=data["invoice_id"], project_id=project.id).first()
        if not invoice:
            return json_error("That invoice doesn't belong to this project.", 400, errors={"invoice_id": "Not found."})
        txn.invoice_id = invoice.id
    txn.status = "verified"
    txn.reviewed_by = g.admin.full_name
    txn.reviewed_at = _now()
    txn.review_note = (data.get("note") or "").strip() or None
    txn.receipt_number = gen_receipt_number()

    sync_payment_advance_from_transactions(project)
    db.session.commit()

    amount = from_minor_units(txn.amount_minor)
    log_activity("payment_txn_verified", f"Payment verified: {format_inr(amount)}", txn.review_note or "",
                 "payment_verified", project=project, entity_type="payment_transaction", entity_id=txn.id,
                 before={"status": "submitted"}, after={"status": "verified"}, actor=g.admin.admin_id)
    notify_customer(project.customer, "payment_verified", f"Payment verified on {project.order_id}",
                    format_inr(amount), project=project)

    return jsonify(serialize_payment_transaction(txn))


@app.route("/api/admin/payment-transactions/<int:txn_id>/reject", methods=["POST"])
@admin_required
def admin_reject_payment_transaction(txn_id):
    txn = PaymentTransaction.query.get_or_404(txn_id)
    if txn.status != "submitted":
        return json_error("Only a submitted transaction can be rejected.", 409)
    project = txn.project

    data = request.get_json(silent=True) or {}
    note = (data.get("note") or "").strip()
    if not note:
        return json_error("Explain why this payment is being rejected.", 400, errors={"note": "This field is required."})

    txn.status = "rejected"
    txn.reviewed_by = g.admin.full_name
    txn.reviewed_at = _now()
    txn.review_note = note
    db.session.commit()  # no balance change — rejected transactions never counted toward advance_paid

    amount = from_minor_units(txn.amount_minor)
    log_activity("payment_txn_rejected", f"Payment rejected: {format_inr(amount)}", note,
                 "payment_rejected", project=project, entity_type="payment_transaction", entity_id=txn.id,
                 before={"status": "submitted"}, after={"status": "rejected"}, actor=g.admin.admin_id)
    notify_customer(project.customer, "payment_rejected", f"Payment needs attention on {project.order_id}",
                    note, project=project)

    return jsonify(serialize_payment_transaction(txn))


@app.route("/api/admin/payment-transactions/<int:txn_id>/receipt", methods=["GET"])
@admin_required
def admin_payment_receipt(txn_id):
    txn = PaymentTransaction.query.get_or_404(txn_id)
    if txn.status != "verified":
        return json_error("A receipt is only available once a payment has been verified.", 409)
    return render_payment_receipt_html(txn, txn.project)


@app.route("/api/admin/payment-transactions/<int:txn_id>/proof", methods=["GET"])
@admin_required
def admin_payment_proof_download(txn_id):
    txn = PaymentTransaction.query.get_or_404(txn_id)
    if not txn.proof_document_id:
        return json_error("No proof was attached to this transaction.", 404)
    doc = Document.query.get_or_404(txn.proof_document_id)
    return send_stored_file(doc.file_path, doc.original_name)


# ==============================================================================
# SECTION 13EA — ADMIN PANEL: MILESTONES / TASKS / REQUIREMENTS / APPROVALS
# (Admin.html — same models/serializers as the client side; single source
# of truth, no duplicate admin-only tables.)
# ==============================================================================
@app.route("/api/admin/projects/<order_id>/milestones", methods=["GET"])
@admin_required
@admin_project_required
def admin_list_milestones(order_id):
    milestones = g.project.milestones.order_by(Milestone.order_index.asc()).all()
    return jsonify([serialize_milestone(m) for m in milestones])


@app.route("/api/admin/projects/<order_id>/milestones", methods=["POST"])
@admin_required
@admin_project_required
def admin_create_milestone(order_id):
    data = request.get_json(silent=True) or {}
    errors = require_fields(data, "title")
    due_date = parse_deadline_input(data.get("due_date")) if data.get("due_date") else None
    if data.get("due_date") and not due_date:
        errors["due_date"] = "Enter a valid date."
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    max_order = db.session.query(db.func.max(Milestone.order_index)).filter_by(project_id=g.project.id).scalar() or 0
    m = Milestone(project_id=g.project.id, title=data["title"].strip(),
                  description=(data.get("description") or "").strip() or None,
                  status=data.get("status") if data.get("status") in MILESTONE_STATUS_LABELS else "upcoming",
                  due_date=due_date, order_index=max_order + 1)
    db.session.add(m)
    db.session.commit()

    log_activity("milestone_created", f"Milestone added: {m.title}", "", "milestone",
                 project=g.project, entity_type="milestone", entity_id=m.id,
                 after={"title": m.title, "status": m.status}, actor=g.admin.admin_id)
    return jsonify(serialize_milestone(m)), 201


@app.route("/api/admin/milestones/<int:milestone_id>", methods=["PATCH"])
@admin_required
def admin_update_milestone(milestone_id):
    m = Milestone.query.get_or_404(milestone_id)
    data = request.get_json(silent=True) or {}
    before = {"title": m.title, "status": m.status, "order_index": m.order_index}

    if "title" in data and data["title"]:
        m.title = data["title"].strip()
    if "description" in data:
        m.description = (data.get("description") or "").strip() or None
    if "status" in data:
        err = validate_choice(data["status"], MILESTONE_STATUS_LABELS.keys(), "status")
        if err:
            return json_error(err, 400, errors={"status": err})
        m.status = data["status"]
        m.completed_at = _now() if m.status == "completed" else None
    if "due_date" in data:
        due_date = parse_deadline_input(data["due_date"]) if data["due_date"] else None
        if data["due_date"] and not due_date:
            return json_error("Enter a valid date.", 400, errors={"due_date": "Enter a valid date."})
        m.due_date = due_date
    if "order_index" in data:
        try:
            m.order_index = int(data["order_index"])
        except (TypeError, ValueError):
            return json_error("Invalid order_index.", 400)

    db.session.commit()
    log_activity("milestone_updated", f"Milestone updated: {m.title}",
                 "Milestone completed." if m.status == "completed" and before["status"] != "completed" else "",
                 "milestone_completed" if m.status == "completed" else "milestone_updated",
                 project=m.project, entity_type="milestone", entity_id=m.id,
                 before=before, after=data, actor=g.admin.admin_id,
                 client_visible=(m.status == "completed"))  # routine edits stay internal; completion is client-visible
    return jsonify(serialize_milestone(m))


@app.route("/api/admin/milestones/<int:milestone_id>", methods=["DELETE"])
@admin_required
def admin_delete_milestone(milestone_id):
    m = Milestone.query.get_or_404(milestone_id)
    title, project = m.title, m.project
    Task.query.filter_by(milestone_id=m.id).update({"milestone_id": None})
    db.session.delete(m)
    db.session.commit()
    write_audit_log("milestone_deleted", "milestone", milestone_id, before={"title": title}, actor=g.admin.admin_id)
    return jsonify({"message": "Milestone deleted."})


@app.route("/api/admin/projects/<order_id>/tasks", methods=["GET"])
@admin_required
@admin_project_required
def admin_list_tasks(order_id):
    tasks = g.project.tasks.order_by(Task.order_index.asc()).all()
    return jsonify([serialize_task(t) for t in tasks])


@app.route("/api/admin/projects/<order_id>/tasks", methods=["POST"])
@admin_required
@admin_project_required
def admin_create_task(order_id):
    data = request.get_json(silent=True) or {}
    errors = require_fields(data, "title")
    milestone_id = data.get("milestone_id")
    if milestone_id and not Milestone.query.filter_by(id=milestone_id, project_id=g.project.id).first():
        errors["milestone_id"] = "Milestone not found on this project."
    due_date = parse_deadline_input(data.get("due_date")) if data.get("due_date") else None
    if data.get("due_date") and not due_date:
        errors["due_date"] = "Enter a valid date."
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    max_order = db.session.query(db.func.max(Task.order_index)).filter_by(project_id=g.project.id).scalar() or 0
    t = Task(project_id=g.project.id, milestone_id=milestone_id or None, title=data["title"].strip(),
             description=(data.get("description") or "").strip() or None,
             status=data.get("status") if data.get("status") in TASK_STATUS_LABELS else "todo",
             priority=data.get("priority") or "normal", assigned_to=(data.get("assigned_to") or "").strip() or None,
             due_date=due_date, order_index=max_order + 1)
    db.session.add(t)
    db.session.commit()

    write_audit_log("task_created", "task", t.id, after={"title": t.title}, actor=g.admin.admin_id)
    if t.assigned_to:
        notify_admins("task_assigned", f"Task assigned: {t.title}", t.assigned_to, project=g.project)
    return jsonify(serialize_task(t)), 201


@app.route("/api/admin/tasks/<int:task_id>", methods=["PATCH"])
@admin_required
def admin_update_task(task_id):
    t = Task.query.get_or_404(task_id)
    data = request.get_json(silent=True) or {}
    before = {"title": t.title, "status": t.status, "priority": t.priority}

    if "title" in data and data["title"]:
        t.title = data["title"].strip()
    if "description" in data:
        t.description = (data.get("description") or "").strip() or None
    if "status" in data:
        err = validate_choice(data["status"], TASK_STATUS_LABELS.keys(), "status")
        if err:
            return json_error(err, 400, errors={"status": err})
        t.status = data["status"]
        t.completed_at = _now() if t.status == "done" else None
    if "priority" in data and data["priority"]:
        t.priority = data["priority"]
    if "assigned_to" in data:
        t.assigned_to = (data.get("assigned_to") or "").strip() or None
    if "milestone_id" in data:
        t.milestone_id = data["milestone_id"] or None
    if "due_date" in data:
        due_date = parse_deadline_input(data["due_date"]) if data["due_date"] else None
        if data["due_date"] and not due_date:
            return json_error("Enter a valid date.", 400, errors={"due_date": "Enter a valid date."})
        t.due_date = due_date
    if "order_index" in data:
        try:
            t.order_index = int(data["order_index"])
        except (TypeError, ValueError):
            return json_error("Invalid order_index.", 400)

    db.session.commit()
    write_audit_log("task_updated", "task", t.id, before=before, after=data, actor=g.admin.admin_id)
    return jsonify(serialize_task(t))


@app.route("/api/admin/tasks/<int:task_id>", methods=["DELETE"])
@admin_required
def admin_delete_task(task_id):
    t = Task.query.get_or_404(task_id)
    title = t.title
    db.session.delete(t)
    db.session.commit()
    write_audit_log("task_deleted", "task", task_id, before={"title": title}, actor=g.admin.admin_id)
    return jsonify({"message": "Task deleted."})


@app.route("/api/admin/projects/<order_id>/requirements", methods=["GET"])
@admin_required
@admin_project_required
def admin_list_requirements(order_id):
    reqs = g.project.requirements.order_by(Requirement.requested_at.desc()).all()
    return jsonify([serialize_requirement(r) for r in reqs])


@app.route("/api/admin/projects/<order_id>/requirements", methods=["POST"])
@admin_required
@admin_project_required
def admin_create_requirement(order_id):
    data = request.get_json(silent=True) or {}
    errors = require_fields(data, "title")
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    due_date = None
    if data.get("due_date"):
        due_date = parse_deadline_input(data["due_date"])
        if not due_date:
            return json_error("Enter a valid due date.", 400, errors={"due_date": "Invalid date."})

    r = Requirement(project_id=g.project.id, title=data["title"].strip(),
                     description=(data.get("description") or "").strip() or None, status="pending", due_date=due_date)
    db.session.add(r)
    db.session.commit()

    log_activity("requirement_created", f"Requirement requested: {r.title}", r.description or "",
                 "requirement_requested", project=g.project, entity_type="requirement", entity_id=r.id,
                 after={"title": r.title}, actor=g.admin.admin_id)
    notify_customer(g.project.customer, "requirement_requested", f"New requirement on {g.project.order_id}",
                    r.title, project=g.project)
    return jsonify(serialize_requirement(r)), 201


@app.route("/api/admin/requirements/<int:requirement_id>", methods=["PATCH"])
@admin_required
def admin_review_requirement(requirement_id):
    """Reviews a client submission: accept it, or send it back with
    admin_notes explaining what needs to change (status -> revision_required,
    which re-opens it as actionable on the client side)."""
    r = Requirement.query.get_or_404(requirement_id)
    data = request.get_json(silent=True) or {}
    status = data.get("status")

    if status is not None:
        err = validate_choice(status, REQUIREMENT_STATUS_LABELS.keys(), "status")
        if err:
            return json_error(err, 400, errors={"status": err})
        if status in ("accepted", "revision_required") and r.status not in ("submitted", "under_review"):
            return json_error("Only a submitted requirement can be reviewed.", 409)

    before = {"status": r.status}
    if "title" in data and data["title"]:
        r.title = data["title"].strip()
    if "description" in data:
        r.description = (data.get("description") or "").strip() or None
    if "admin_notes" in data:
        r.admin_notes = (data.get("admin_notes") or "").strip() or None
    if "due_date" in data:
        if data["due_date"]:
            parsed = parse_deadline_input(data["due_date"])
            if not parsed:
                return json_error("Enter a valid due date.", 400, errors={"due_date": "Invalid date."})
            r.due_date = parsed
        else:
            r.due_date = None
    if status:
        r.status = status
        r.reviewed_at = _now()
    r.updated_at = _now()
    db.session.commit()

    if status == "accepted":
        log_activity("requirement_accepted", f"Requirement accepted: {r.title}", "", "requirement_accepted",
                     project=r.project, entity_type="requirement", entity_id=r.id,
                     before=before, after={"status": "accepted"}, actor=g.admin.admin_id)
        notify_customer(r.project.customer, "requirement_accepted", f"Requirement accepted on {r.project.order_id}",
                        r.title, project=r.project)
    elif status == "revision_required":
        log_activity("requirement_revision_requested", f"Revision needed: {r.title}", r.admin_notes or "",
                     "requirement_revision", project=r.project, entity_type="requirement", entity_id=r.id,
                     before=before, after={"status": "revision_required"}, actor=g.admin.admin_id)
        notify_customer(r.project.customer, "requirement_revision_required",
                        f"Revision needed on {r.project.order_id}", r.admin_notes or r.title, project=r.project)
    else:
        write_audit_log("requirement_updated", "requirement", r.id, before=before, after=data, actor=g.admin.admin_id)

    return jsonify(serialize_requirement(r))


@app.route("/api/admin/projects/<order_id>/approvals", methods=["GET"])
@admin_required
@admin_project_required
def admin_list_approvals(order_id):
    approvals = g.project.approvals.order_by(Approval.submitted_at.desc()).all()
    return jsonify([serialize_approval(a) for a in approvals])


@app.route("/api/admin/projects/<order_id>/approvals", methods=["POST"])
@admin_required
@admin_project_required
def admin_create_approval(order_id):
    """Requests client sign-off on a deliverable. The client then approves
    or requests changes via the existing client-side approval routes —
    admin does not decide its own approval requests."""
    data = request.get_json(silent=True) or {}
    errors = require_fields(data, "item_title")
    requirement_id = data.get("requirement_id")
    if requirement_id and not Requirement.query.filter_by(id=requirement_id, project_id=g.project.id).first():
        errors["requirement_id"] = "Requirement not found on this project."
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    a = Approval(project_id=g.project.id, requirement_id=requirement_id or None,
                 item_title=data["item_title"].strip(), item_description=(data.get("item_description") or "").strip() or None,
                 status="pending", requested_by=g.admin.full_name)
    db.session.add(a)
    db.session.commit()

    log_activity("approval_requested", f"Approval requested: {a.item_title}", a.item_description or "",
                 "approval_requested", project=g.project, entity_type="approval", entity_id=a.id,
                 after={"item_title": a.item_title}, actor=g.admin.admin_id)
    notify_customer(g.project.customer, "approval_requested", f"Approval needed on {g.project.order_id}",
                    a.item_title, project=g.project)
    return jsonify(serialize_approval(a)), 201


# ==============================================================================
# SECTION 13EC — ADMIN PANEL: PROPOSALS / QUOTATIONS (Phase 5)
# ==============================================================================
@app.route("/api/admin/projects/<order_id>/proposals", methods=["GET"])
@admin_required
@admin_project_required
def admin_list_proposals(order_id):
    proposals = Proposal.query.filter_by(project_id=g.project.id).order_by(
        Proposal.proposal_number, Proposal.version.desc()
    ).all()
    return jsonify([serialize_proposal(p, include_lines=False) for p in proposals])


@app.route("/api/admin/projects/<order_id>/proposals", methods=["POST"])
@admin_required
@admin_project_required
def admin_create_proposal(order_id):
    """Creates a fresh proposal_number at version 1. To revise an existing
    proposal use POST .../new-version instead — this route never reuses an
    existing number."""
    data = request.get_json(silent=True) or {}
    proposal, error_response = _build_proposal_from_payload(g.project, data)
    if error_response:
        return error_response

    proposal.proposal_number = gen_proposal_number()
    proposal.version = 1
    proposal.created_by = g.admin.full_name
    db.session.add(proposal)
    db.session.commit()

    write_audit_log("proposal_created", "proposal", proposal.id, after={"proposal_number": proposal.proposal_number})
    return jsonify(serialize_proposal(proposal)), 201


def _build_proposal_from_payload(project, data, existing=None):
    """Shared validation/build logic for create and new-version. Returns
    (proposal, None) on success or (None, error_response) on failure.
    Server always recomputes totals from lines — client-submitted totals,
    including line_total, are ignored."""
    title = (data.get("title") or "").strip()[:200]
    errors = {}
    if not title:
        errors["title"] = "Enter a proposal title."

    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        errors["lines"] = "Add at least one pricing line."

    validity_date = None
    if data.get("validity_date"):
        validity_date = parse_deadline_input(data["validity_date"])
        if not validity_date:
            errors["validity_date"] = "Enter a valid date."

    revision_count = data.get("revision_count")
    if revision_count not in (None, ""):
        try:
            revision_count = int(revision_count)
            if revision_count < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors["revision_count"] = "Enter a valid revision count."
    else:
        revision_count = None

    if errors:
        return None, json_error("Please correct the highlighted fields.", 400, errors=errors)

    proposal = existing or Proposal(project_id=project.id)
    proposal.title = title
    proposal.scope_summary = (data.get("scope_summary") or "").strip() or None
    proposal.deliverables = (data.get("deliverables") or "").strip() or None
    proposal.exclusions = (data.get("exclusions") or "").strip() or None
    proposal.timeline_label = (data.get("timeline_label") or "").strip()[:100] or None
    proposal.revision_count = revision_count
    proposal.revision_window_label = (data.get("revision_window_label") or "").strip()[:100] or None
    proposal.additional_revision_note = (data.get("additional_revision_note") or "").strip()[:200] or None
    proposal.payment_terms_summary = (data.get("payment_terms_summary") or "").strip() or None
    proposal.support_duration_label = (data.get("support_duration_label") or "").strip()[:100] or None
    proposal.validity_date = validity_date
    proposal.status = "draft"

    # Replace lines wholesale — simplest correct approach for a draft-only edit surface.
    for old_line in list(proposal.lines):
        db.session.delete(old_line)
    db.session.flush()

    line_errors = []
    for i, raw in enumerate(raw_lines):
        line_title = (raw.get("title") or "").strip()[:150]
        try:
            quantity = float(raw.get("quantity", 1) or 1)
            unit_price = float(raw.get("unit_price"))
            if quantity <= 0 or unit_price < 0:
                raise ValueError
        except (TypeError, ValueError):
            line_errors.append(f"Line {i + 1}: enter a valid quantity and price.")
            continue
        if not line_title:
            line_errors.append(f"Line {i + 1}: enter a title.")
            continue
        db.session.add(ProposalLine(
            proposal=proposal, title=line_title, description=(raw.get("description") or "").strip() or None,
            quantity=quantity, unit_price_minor=to_minor_units(unit_price), sort_order=i,
        ))

    if line_errors:
        db.session.rollback()
        return None, json_error("Please correct the pricing lines.", 400, errors={"lines": line_errors})

    db.session.flush()
    recompute_proposal_totals(proposal)
    return proposal, None


@app.route("/api/admin/proposals/<proposal_number>", methods=["GET"])
@admin_required
def admin_get_proposal(proposal_number):
    version = request.args.get("version")
    query = Proposal.query.filter_by(proposal_number=proposal_number.upper())
    proposal = (query.filter_by(version=int(version)).first() if version and version.isdigit()
                else query.order_by(Proposal.version.desc()).first())
    if not proposal:
        return json_error("Proposal not found.", 404)
    return jsonify(serialize_proposal(proposal))


@app.route("/api/admin/proposals/<proposal_number>/versions", methods=["GET"])
@admin_required
def admin_list_proposal_versions(proposal_number):
    versions = Proposal.query.filter_by(proposal_number=proposal_number.upper()).order_by(Proposal.version.desc()).all()
    if not versions:
        return json_error("Proposal not found.", 404)
    return jsonify([serialize_proposal(p, include_lines=False) for p in versions])


@app.route("/api/admin/proposals/<int:proposal_id>", methods=["PATCH"])
@admin_required
def admin_update_proposal(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.status not in PROPOSAL_EDITABLE_STATUSES:
        return json_error("Only a draft proposal can be edited directly — create a new version instead.", 409)
    data = request.get_json(silent=True) or {}
    updated, error_response = _build_proposal_from_payload(proposal.project, data, existing=proposal)
    if error_response:
        return error_response
    db.session.commit()
    write_audit_log("proposal_updated", "proposal", proposal.id)
    return jsonify(serialize_proposal(updated))


@app.route("/api/admin/proposals/<int:proposal_id>/send", methods=["POST"])
@admin_required
def admin_send_proposal(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    if proposal.status not in ("draft", "changes_requested"):
        return json_error("This proposal can't be sent from its current status.", 409)
    project = proposal.project

    proposal.status = "sent"
    proposal.sent_at = _now()
    db.session.commit()

    log_activity("proposal_sent", f"Proposal {proposal.proposal_number} sent",
                 f"{proposal.title} — {format_inr(from_minor_units(proposal.total_minor))}", "proposal_sent",
                 project=project, entity_type="proposal", entity_id=proposal.id, actor=g.admin.admin_id)
    return jsonify(serialize_proposal(proposal))


@app.route("/api/admin/proposals/<int:proposal_id>/new-version", methods=["POST"])
@admin_required
def admin_new_proposal_version(proposal_id):
    """Never edits the old version in place — copies it into a fresh draft
    at version+1 and marks the old one 'superseded' immediately, so exactly
    one version is ever the 'current' one for a given proposal_number."""
    old = Proposal.query.get_or_404(proposal_id)
    if old.status not in PROPOSAL_VERSIONABLE_STATUSES:
        return json_error("A new version can't be created from this status.", 409)

    new = Proposal(
        project_id=old.project_id, proposal_number=old.proposal_number, version=old.version + 1,
        supersedes_id=old.id, title=old.title, status="draft", currency_code=old.currency_code,
        scope_summary=old.scope_summary, deliverables=old.deliverables, exclusions=old.exclusions,
        timeline_label=old.timeline_label, revision_count=old.revision_count,
        revision_window_label=old.revision_window_label, additional_revision_note=old.additional_revision_note,
        payment_terms_summary=old.payment_terms_summary, support_duration_label=old.support_duration_label,
        validity_date=old.validity_date, created_by=g.admin.full_name,
    )
    db.session.add(new)
    db.session.flush()
    for old_line in old.lines:
        db.session.add(ProposalLine(
            proposal=new, title=old_line.title, description=old_line.description,
            quantity=old_line.quantity, unit_price_minor=old_line.unit_price_minor, sort_order=old_line.sort_order,
        ))
    db.session.flush()
    recompute_proposal_totals(new)
    old.status = "superseded"
    db.session.commit()

    write_audit_log("proposal_version_created", "proposal", new.id,
                     before={"supersedes": old.id}, after={"version": new.version})
    return jsonify(serialize_proposal(new)), 201


@app.route("/api/admin/proposals/<int:proposal_id>/view", methods=["GET"])
@admin_required
def admin_view_proposal(proposal_id):
    proposal = Proposal.query.get_or_404(proposal_id)
    return render_proposal_html(proposal)


# ==============================================================================
# SECTION 13EDA — ADMIN PANEL: INVOICES (Phase 8)
# ==============================================================================
@app.route("/api/admin/projects/<order_id>/invoices", methods=["GET"])
@admin_required
@admin_project_required
def admin_list_invoices(order_id):
    invoices = Invoice.query.filter_by(project_id=g.project.id).order_by(Invoice.created_at.desc()).all()
    return jsonify([serialize_invoice(i) for i in invoices])


@app.route("/api/admin/projects/<order_id>/invoices", methods=["POST"])
@admin_required
@admin_project_required
def admin_create_invoice(order_id):
    project = g.project
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:200]
    errors = {}
    if not title:
        errors["title"] = "Enter a title."
    try:
        amount = float(data.get("amount"))
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors["amount"] = "Enter a valid amount."

    proposal_id = None
    if data.get("proposal_id"):
        proposal = Proposal.query.filter_by(id=data["proposal_id"], project_id=project.id).first()
        if not proposal:
            errors["proposal_id"] = "That proposal doesn't belong to this project."
        else:
            proposal_id = proposal.id

    issue_date, due_date = None, None
    if data.get("issue_date"):
        issue_date = parse_deadline_input(data["issue_date"])
        if not issue_date:
            errors["issue_date"] = "Enter a valid date."
    if data.get("due_date"):
        due_date = parse_deadline_input(data["due_date"])
        if not due_date:
            errors["due_date"] = "Enter a valid date."

    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    inv = Invoice(
        invoice_number=gen_invoice_number(), project_id=project.id, proposal_id=proposal_id, title=title,
        notes=(data.get("notes") or "").strip() or None, amount_minor=to_minor_units(amount),
        currency_code=(project.payment.currency_code if project.payment else "INR") or "INR",
        status="draft", issue_date=issue_date, due_date=due_date, created_by=g.admin.full_name,
    )
    db.session.add(inv)
    db.session.commit()

    write_audit_log("invoice_created", "invoice", inv.id, after={"invoice_number": inv.invoice_number, "amount": amount})
    return jsonify(serialize_invoice(inv)), 201


@app.route("/api/admin/invoices/<int:invoice_id>", methods=["PATCH"])
@admin_required
def admin_update_invoice(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    if inv.status != "draft":
        return json_error("Only a draft invoice can be edited — issue or cancel it instead.", 409)
    data = request.get_json(silent=True) or {}
    if "title" in data:
        title = (data["title"] or "").strip()[:200]
        if not title:
            return json_error("Title can't be empty.", 400, errors={"title": "Required."})
        inv.title = title
    if "amount" in data:
        try:
            amount = float(data["amount"])
            if amount <= 0:
                raise ValueError
            inv.amount_minor = to_minor_units(amount)
        except (TypeError, ValueError):
            return json_error("Enter a valid amount.", 400, errors={"amount": "Invalid."})
    if "notes" in data:
        inv.notes = (data["notes"] or "").strip() or None
    if "due_date" in data:
        if data["due_date"]:
            parsed = parse_deadline_input(data["due_date"])
            if not parsed:
                return json_error("Enter a valid due date.", 400, errors={"due_date": "Invalid."})
            inv.due_date = parsed
        else:
            inv.due_date = None
    db.session.commit()
    write_audit_log("invoice_updated", "invoice", inv.id)
    return jsonify(serialize_invoice(inv))


@app.route("/api/admin/invoices/<int:invoice_id>/issue", methods=["POST"])
@admin_required
def admin_issue_invoice(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    if inv.status != "draft":
        return json_error("Only a draft invoice can be issued.", 409)
    inv.status = "issued"
    if not inv.issue_date:
        inv.issue_date = date.today()
    db.session.commit()

    add_timeline_event(inv.project_id, "Invoice Issued", f"{inv.invoice_number} — {format_inr(from_minor_units(inv.amount_minor))}", "invoice_issued")
    write_audit_log("invoice_issued", "invoice", inv.id)
    if inv.project.customer:
        notify_customer(inv.project.customer, "invoice_issued", f"New invoice on {inv.project.order_id}",
                        f"{inv.invoice_number} — {format_inr(from_minor_units(inv.amount_minor))}", project=inv.project)
    return jsonify(serialize_invoice(inv))


@app.route("/api/admin/invoices/<int:invoice_id>/cancel", methods=["POST"])
@admin_required
def admin_cancel_invoice(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    if inv.status == "cancelled":
        return json_error("This invoice is already cancelled.", 409)
    inv.status = "cancelled"
    db.session.commit()
    write_audit_log("invoice_cancelled", "invoice", inv.id)
    return jsonify(serialize_invoice(inv))


@app.route("/api/admin/invoices/<int:invoice_id>/view", methods=["GET"])
@admin_required
def admin_view_invoice(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    return render_invoice_html(inv)


# ==============================================================================
# SECTION 13ED — ADMIN PANEL: CHANGE REQUESTS (Phase 6)
# ==============================================================================
@app.route("/api/admin/projects/<order_id>/change-requests", methods=["GET"])
@admin_required
@admin_project_required
def admin_list_change_requests(order_id):
    items = ChangeRequest.query.filter_by(project_id=g.project.id).order_by(ChangeRequest.created_at.desc()).all()
    return jsonify([serialize_change_request(c) for c in items])


@app.route("/api/admin/projects/<order_id>/change-requests", methods=["POST"])
@admin_required
@admin_project_required
def admin_create_change_request(order_id):
    """Admin can also log a change request proactively (e.g. a scope note
    from a phone call) — sender identity is still server-derived, never
    trusted from the body."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:200]
    description = (data.get("description") or "").strip()
    cr_type = (data.get("type") or "other").strip()
    errors = {}
    if not title:
        errors["title"] = "Enter a title."
    if not description:
        errors["description"] = "Describe what's being requested."
    if cr_type not in CHANGE_REQUEST_TYPE_LABELS:
        cr_type = "other"
    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    cr = ChangeRequest(
        change_request_id=gen_change_request_id(), project_id=g.project.id, title=title,
        description=description, type=cr_type, status="under_review", submitted_by="admin",
    )
    db.session.add(cr)
    db.session.commit()

    write_audit_log("change_request_created", "change_request", cr.id, after={"type": cr_type, "submitted_by": "admin"})
    return jsonify(serialize_change_request(cr)), 201


@app.route("/api/admin/change-requests/<change_request_id>", methods=["GET"])
@admin_required
def admin_get_change_request(change_request_id):
    cr = ChangeRequest.query.filter_by(change_request_id=change_request_id.upper()).first()
    if not cr:
        return json_error("Change request not found.", 404)
    return jsonify(serialize_change_request(cr))


@app.route("/api/admin/change-requests/<change_request_id>", methods=["PATCH"])
@admin_required
def admin_update_change_request(change_request_id):
    """One flexible endpoint for classification + status, mirroring the
    Requirement PATCH pattern — a change request moves through its
    lifecycle via normal field edits, not a proliferation of action verbs.
    Financial impact (estimated_cost) is informational only here; the
    authoritative commercial amount, once a change is chargeable, lives in
    the linked change-order Proposal (see proposal_id), never duplicated
    into a second total on this row."""
    cr = ChangeRequest.query.filter_by(change_request_id=change_request_id.upper()).first()
    if not cr:
        return json_error("Change request not found.", 404)
    data = request.get_json(silent=True) or {}
    before = {"status": cr.status, "impact": cr.impact}
    errors = {}

    if "status" in data:
        if data["status"] not in CHANGE_REQUEST_STATUS_LABELS:
            errors["status"] = "Not a valid status."
        else:
            cr.status = data["status"]
    if "impact" in data:
        if data["impact"] not in (None, "", "included", "chargeable"):
            errors["impact"] = "Not a valid classification."
        else:
            cr.impact = data["impact"] or None
    if "estimated_cost" in data:
        if data["estimated_cost"] in (None, ""):
            cr.estimated_cost_minor = None
        else:
            try:
                cr.estimated_cost_minor = to_minor_units(float(data["estimated_cost"]))
            except (TypeError, ValueError):
                errors["estimated_cost"] = "Enter a valid amount."
    if "estimated_timeline_label" in data:
        cr.estimated_timeline_label = (data["estimated_timeline_label"] or "").strip()[:100] or None
    if "admin_response" in data:
        cr.admin_response = (data["admin_response"] or "").strip() or None
    if "proposal_id" in data:
        if data["proposal_id"]:
            proposal = Proposal.query.filter_by(id=data["proposal_id"], project_id=cr.project_id).first()
            if not proposal:
                errors["proposal_id"] = "That proposal doesn't belong to this project."
            else:
                cr.proposal_id = proposal.id
        else:
            cr.proposal_id = None

    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    if cr.status in CHANGE_REQUEST_RESOLVED_STATUSES and not cr.resolved_at:
        cr.resolved_at = _now()
    elif cr.status not in CHANGE_REQUEST_RESOLVED_STATUSES:
        cr.resolved_at = None

    db.session.commit()
    write_audit_log("change_request_updated", "change_request", cr.id, before=before,
                     after={"status": cr.status, "impact": cr.impact})

    # Only notify the customer on changes they'd actually want to know
    # about — status movement or a response — not every internal tweak
    # (e.g. an admin adjusting the estimated-timeline label alone stays
    # silent, avoiding notification spam).
    if "status" in data or "admin_response" in data:
        notify_customer(cr.project.customer, "change_request_updated",
                        f"Update on your request: {cr.title}",
                        CHANGE_REQUEST_STATUS_LABELS.get(cr.status, cr.status), project=cr.project)

    return jsonify(serialize_change_request(cr))


# ==============================================================================
# SECTION 13EDB — ADMIN PANEL: SUPPORT REQUESTS (Phase 9)
# Covers both legacy generic reports (project_id NULL, from the original
# My Account "Report a Problem" form) and new project-scoped requests in
# one list — filterable by either.
# ==============================================================================
@app.route("/api/admin/support", methods=["GET"])
@admin_required
def admin_list_support_tickets():
    status = (request.args.get("status") or "").strip()
    priority = (request.args.get("priority") or "").strip()
    order_id = (request.args.get("order_id") or "").strip()
    query = SupportTicket.query
    if status:
        query = query.filter_by(status=status)
    if priority:
        query = query.filter_by(priority=priority)
    if order_id:
        project = Project.query.filter_by(order_id=order_id.upper()).first()
        query = query.filter_by(project_id=project.id if project else -1)
    items = query.order_by(SupportTicket.created_at.desc()).all()
    return jsonify([serialize_support_ticket(t) for t in items])


@app.route("/api/admin/support/summary", methods=["GET"])
@admin_required
def admin_support_summary():
    """Real counts only — no fabricated score (9AA)."""
    open_statuses = ("submitted", "acknowledged", "in_progress", "waiting_for_customer", "reopened", "open")
    return jsonify({
        "open": SupportTicket.query.filter(SupportTicket.status.in_(open_statuses)).count(),
        "high_priority_open": SupportTicket.query.filter(
            SupportTicket.status.in_(open_statuses), SupportTicket.priority.in_(["high", "urgent"])
        ).count(),
        "waiting_for_customer": SupportTicket.query.filter_by(status="waiting_for_customer").count(),
        "reopened": SupportTicket.query.filter_by(status="reopened").count(),
    })


@app.route("/api/admin/support/<int:ticket_id>", methods=["GET"])
@admin_required
def admin_get_support_ticket(ticket_id):
    ticket = SupportTicket.query.get_or_404(ticket_id)
    return jsonify(serialize_support_ticket(ticket))


@app.route("/api/admin/support/<int:ticket_id>", methods=["PATCH"])
@admin_required
def admin_update_support_ticket(ticket_id):
    """One flexible endpoint for classification + status + response,
    mirroring the ChangeRequest/Requirement PATCH pattern rather than a
    proliferation of action verbs. Frontend-supplied status/priority/
    category are validated against controlled sets — never arbitrary
    strings (9F/9G/9H)."""
    ticket = SupportTicket.query.get_or_404(ticket_id)
    data = request.get_json(silent=True) or {}
    before = {"status": ticket.status, "priority": ticket.priority, "category": ticket.category}
    errors = {}

    if "status" in data:
        if data["status"] not in SUPPORT_STATUS_LABELS:
            errors["status"] = "Not a valid status."
        else:
            ticket.status = data["status"]
    if "priority" in data:
        if data["priority"] not in {"low", "normal", "high", "urgent"}:
            errors["priority"] = "Not a valid priority."
        else:
            ticket.priority = data["priority"]
    if "category" in data:
        if data["category"] and data["category"] not in SUPPORT_CATEGORY_LABELS:
            errors["category"] = "Not a valid category."
        else:
            ticket.category = data["category"] or None
    if "admin_response" in data:
        ticket.admin_response = (data["admin_response"] or "").strip() or None
        if not ticket.first_response_at:
            ticket.first_response_at = _now()

    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    if ticket.status in SUPPORT_RESOLVED_STATUSES and not ticket.resolved_at:
        ticket.resolved_at = _now()
        ticket.resolved_by = g.admin.full_name
    elif ticket.status not in SUPPORT_RESOLVED_STATUSES:
        ticket.resolved_at = None
        ticket.resolved_by = None

    db.session.commit()
    write_audit_log("support_ticket_updated", "support_ticket", ticket.id, before=before,
                     after={"status": ticket.status, "priority": ticket.priority, "category": ticket.category})

    if ("status" in data or "admin_response" in data) and ticket.customer:
        notify_customer(ticket.customer, "support_ticket_updated",
                        f"Update on your support request: {ticket.title or gen_support_ticket_ref(ticket)}",
                        SUPPORT_STATUS_LABELS.get(ticket.status, ticket.status), project=ticket.project)

    return jsonify(serialize_support_ticket(ticket))


@app.route("/api/admin/support/<int:ticket_id>/convert-to-change-request", methods=["POST"])
@admin_required
def admin_convert_support_to_change_request(ticket_id):
    """The explicit, audited bridge from 'bug report' to 'new/chargeable
    work' (9Q). Only possible for a project-linked ticket — a general
    portal-wide report has no project to attach the change request to.
    Creates the ChangeRequest as a normal 'submitted' item that then goes
    through the exact same admin classification/proposal flow as any
    other change request — this route does not itself decide chargeable
    vs included, and never touches payment/proposal state directly."""
    ticket = SupportTicket.query.get_or_404(ticket_id)
    if not ticket.project_id:
        return json_error("This report isn't linked to a project, so it can't become a change request.", 409)
    if ticket.converted_change_request_id:
        return json_error("This request has already been converted.", 409)

    data = request.get_json(silent=True) or {}
    cr = ChangeRequest(
        change_request_id=gen_change_request_id(), project_id=ticket.project_id,
        title=(data.get("title") or ticket.title or "Change request from support ticket")[:200],
        description=(data.get("description") or ticket.message), type=(data.get("type") or "other"),
        status="under_review", submitted_by="admin",
    )
    db.session.add(cr)
    db.session.flush()
    ticket.converted_change_request_id = cr.id
    if ticket.status not in SUPPORT_RESOLVED_STATUSES:
        ticket.status = "closed"
        ticket.resolved_at = _now()
        ticket.resolved_by = g.admin.full_name
    db.session.commit()

    write_audit_log("support_ticket_converted", "support_ticket", ticket.id, after={"change_request_id": cr.change_request_id})
    if ticket.customer:
        notify_customer(ticket.customer, "support_ticket_updated",
                        f"Your request has become a change request: {cr.title}",
                        "Kytron is treating this as new work — see Change Requests for details.", project=ticket.project)
    return jsonify({"ticket": serialize_support_ticket(ticket), "change_request": serialize_change_request(cr)}), 201


# ==============================================================================
# SECTION 13EE — ADMIN PANEL: PROJECT NOTES (internal-only, Phase 6H)
# ==============================================================================
@app.route("/api/admin/projects/<order_id>/notes", methods=["GET"])
@admin_required
@admin_project_required
def admin_list_project_notes(order_id):
    notes = g.project.notes.order_by(ProjectNote.created_at.desc()).all()
    return jsonify([serialize_project_note(n) for n in notes])


@app.route("/api/admin/projects/<order_id>/notes", methods=["POST"])
@admin_required
@admin_project_required
def admin_add_project_note(order_id):
    data = request.get_json(silent=True) or {}
    note_text = (data.get("note") or "").strip()
    if not note_text:
        return json_error("Enter a note.", 400, errors={"note": "This field is required."})
    note = ProjectNote(project_id=g.project.id, note=note_text, created_by=g.admin.full_name)
    db.session.add(note)
    db.session.commit()
    write_audit_log("project_note_added", "project", g.project.order_id)
    return jsonify(serialize_project_note(note)), 201


# ==============================================================================
# SECTION 13EF — ADMIN PANEL: PROJECT WORKSPACE MESSAGES (Phase 6C)
# Thin order_id-keyed wrapper around the existing Conversation/Message
# system, matching the workspace's established per-project route shape —
# reuses get_project_conversation/send_message/mark_messages_read exactly
# as the cross-project /api/admin/communication/... routes already do.
# ==============================================================================
@app.route("/api/admin/projects/<order_id>/messages", methods=["GET"])
@admin_required
@admin_project_required
def admin_project_messages(order_id):
    convo = get_project_conversation(g.project)
    msgs = convo.messages.order_by(Message.created_at.asc()).all()
    mark_messages_read(convo, "admin")
    return jsonify([serialize_message(m, "admin") for m in msgs])


@app.route("/api/admin/projects/<order_id>/messages", methods=["POST"])
@admin_required
@admin_project_required
def admin_send_project_message(order_id):
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return json_error("Message can't be empty.", 400, errors={"body": "This field is required."})
    msg = send_message(g.project, "admin", g.admin.full_name, body)
    return jsonify(serialize_message(msg, "admin")), 201


# ==============================================================================
# SECTION 13F — ADMIN PANEL: REVISION REQUESTS (cross-project, Requests Tab 2)
# ==============================================================================
@app.route("/api/admin/revisions", methods=["GET"])
@admin_required
def admin_list_revisions():
    query = Revision.query.join(Project, Revision.project_id == Project.id)
    status_filter = (request.args.get("status") or "").strip()
    if status_filter in REVISION_STATUS_LABELS:
        query = query.filter(Revision.status == status_filter)
    revisions = query.order_by(Revision.requested_at.desc()).all()
    return jsonify([serialize_revision_admin_row(r) for r in revisions])


@app.route("/api/admin/revisions/<int:revision_id>/accept", methods=["POST"])
@admin_required
def admin_accept_revision(revision_id):
    revision = Revision.query.get(revision_id)
    if not revision:
        return json_error("Revision request not found.", 404)
    if revision.status != "pending":
        return json_error("This revision request has already been decided.", 409)

    data = request.get_json(silent=True) or {}
    before = {"status": revision.status}

    revision.status = "accepted"
    revision.upload_enabled = True
    revision.decided_at = _now()
    if data.get("reply"):
        revision.admin_response = data["reply"].strip()

    add_timeline_event(revision.project_id, "Revision Accepted",
                        "Your revision request was accepted. You can now upload related files.",
                        "revision_accepted")
    db.session.commit()

    write_audit_log("revision_accepted", "revision", revision.id, before=before,
                     after={"status": "accepted"}, actor=g.admin.admin_id)
    if revision.project and revision.project.customer:
        send_project_update_email(revision.project.customer, f"Revision request accepted — {revision.project.order_id}",
                    "Your revision request was accepted. You can now upload related files from your dashboard.")

    return jsonify({"message": "Revision accepted.", "revision": serialize_revision_admin_row(revision)})


@app.route("/api/admin/revisions/<int:revision_id>/reject", methods=["POST"])
@admin_required
def admin_reject_revision(revision_id):
    revision = Revision.query.get(revision_id)
    if not revision:
        return json_error("Revision request not found.", 404)
    if revision.status != "pending":
        return json_error("This revision request has already been decided.", 409)

    data = request.get_json(silent=True) or {}
    before = {"status": revision.status}

    revision.status = "rejected"
    revision.decided_at = _now()
    if data.get("reply"):
        revision.admin_response = data["reply"].strip()
    db.session.commit()

    write_audit_log("revision_rejected", "revision", revision.id, before=before,
                     after={"status": "rejected"}, actor=g.admin.admin_id)
    if revision.project and revision.project.customer:
        send_project_update_email(revision.project.customer, f"Update on your revision request — {revision.project.order_id}",
                    "After review, we're unable to move forward with this revision request.")

    return jsonify({"message": "Revision rejected.", "revision": serialize_revision_admin_row(revision)})


@app.route("/api/admin/revisions/<int:revision_id>/reply", methods=["POST"])
@admin_required
def admin_reply_revision(revision_id):
    revision = Revision.query.get(revision_id)
    if not revision:
        return json_error("Revision request not found.", 404)

    data = request.get_json(silent=True) or {}
    reply = (data.get("reply") or "").strip()
    if not reply:
        return json_error("Enter a reply message.", 400, errors={"reply": "Enter a reply message."})

    revision.admin_response = reply
    db.session.commit()
    write_audit_log("revision_replied", "revision", revision.id, after={"reply": reply}, actor=g.admin.admin_id)

    return jsonify({"message": "Reply saved.", "revision": serialize_revision_admin_row(revision)})


@app.route("/api/admin/revisions/<int:revision_id>/close", methods=["POST"])
@admin_required
def admin_close_revision(revision_id):
    revision = Revision.query.get(revision_id)
    if not revision:
        return json_error("Revision request not found.", 404)
    if revision.status != "accepted":
        return json_error("Only an accepted revision can be closed.", 409)

    revision.status = "completed"
    revision.upload_enabled = False
    db.session.commit()
    write_audit_log("revision_closed", "revision", revision.id, actor=g.admin.admin_id)

    return jsonify({"message": "Revision closed.", "revision": serialize_revision_admin_row(revision)})


@app.route("/api/admin/revision-files/<int:file_id>/download", methods=["GET"])
@admin_required
def admin_download_revision_file(file_id):
    rf = RevisionFile.query.get_or_404(file_id)
    return send_stored_file(rf.file_path, rf.original_name)


@app.route("/api/admin/requirement-files/<int:file_id>/download", methods=["GET"])
@admin_required
def admin_download_requirement_file(file_id):
    rf = RequirementFile.query.get_or_404(file_id)
    return send_stored_file(rf.file_path, rf.original_name)


# ==============================================================================
# SECTION 13G — ADMIN PANEL: DOCUMENTS (per-project, Projects Workspace tab)
# ==============================================================================
@app.route("/api/admin/projects/<order_id>/documents", methods=["POST"])
@admin_required
def admin_upload_document(order_id):
    project = Project.query.filter_by(order_id=order_id.upper(), is_approved=True).first()
    if not project:
        return json_error("Project not found.", 404)
    if "file" not in request.files:
        return json_error("No file provided.", 400)

    category = request.form.get("category") or "Other"
    if category not in DOCUMENT_CATEGORIES:
        category = "Other"
    visibility = request.form.get("visibility") or "client"
    if visibility not in {"client", "internal"}:
        visibility = "client"

    saved, error = save_upload(request.files["file"], "projects")
    if error:
        return json_error(error, 400)

    doc = Document(
        project_id=project.id, original_name=saved["original_name"], stored_name=saved["stored_name"],
        category=category, uploader=g.admin.full_name, size=saved["size"], mime_type=saved["mime_type"],
        visibility=visibility, file_path=saved["file_path"],
    )
    db.session.add(doc)
    add_timeline_event(project.id, "Document Uploaded", f"{saved['original_name']} ({category}) was uploaded.", "document_uploaded")
    db.session.commit()

    write_audit_log("document_uploaded", "project", project.order_id, after={"file_name": saved["original_name"]}, actor=g.admin.admin_id)
    return jsonify({"message": "Document uploaded.", "document": serialize_document_admin(doc)}), 201


@app.route("/api/admin/documents/<int:document_id>", methods=["PATCH"])
@admin_required
def admin_update_document(document_id):
    doc = Document.query.get(document_id)
    if not doc:
        return json_error("Document not found.", 404)

    data = request.get_json(silent=True) or {}
    if "category" in data:
        if data["category"] not in DOCUMENT_CATEGORIES:
            return json_error("Choose a valid category.", 400, errors={"category": "Choose a valid category."})
        doc.category = data["category"]
    if "visibility" in data:
        if data["visibility"] not in {"client", "internal"}:
            return json_error("Choose a valid visibility.", 400, errors={"visibility": "Choose a valid visibility."})
        doc.visibility = data["visibility"]

    db.session.commit()
    write_audit_log("document_updated", "document", doc.id, after=data, actor=g.admin.admin_id)
    return jsonify({"message": "Document updated.", "document": serialize_document_admin(doc)})


@app.route("/api/admin/documents/<int:document_id>/replace", methods=["POST"])
@admin_required
def admin_replace_document(document_id):
    doc = Document.query.get(document_id)
    if not doc:
        return json_error("Document not found.", 404)
    if "file" not in request.files:
        return json_error("No file provided.", 400)

    saved, error = save_upload(request.files["file"], "projects")
    if error:
        return json_error(error, 400)

    old_path = os.path.join(UPLOAD_DIR, doc.file_path)
    doc.original_name = saved["original_name"]
    doc.stored_name = saved["stored_name"]
    doc.size = saved["size"]
    doc.mime_type = saved["mime_type"]
    doc.file_path = saved["file_path"]
    doc.upload_date = _now()
    doc.uploader = g.admin.full_name
    db.session.commit()

    if os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            logger.warning("Could not remove replaced file at %s", old_path)

    add_timeline_event(doc.project_id, "Document Uploaded", f"{saved['original_name']} replaced a previous file.", "document_uploaded")
    db.session.commit()
    write_audit_log("document_replaced", "document", doc.id, after={"file_name": saved["original_name"]}, actor=g.admin.admin_id)
    return jsonify({"message": "Document replaced.", "document": serialize_document_admin(doc)})


@app.route("/api/admin/documents/<int:document_id>", methods=["DELETE"])
@admin_required
def admin_delete_document(document_id):
    doc = Document.query.get(document_id)
    if not doc:
        return json_error("Document not found.", 404)

    file_path = os.path.join(UPLOAD_DIR, doc.file_path)
    project_id, file_name = doc.project_id, doc.original_name
    db.session.delete(doc)
    db.session.commit()

    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            logger.warning("Could not remove deleted file at %s", file_path)

    write_audit_log("document_deleted", "document", document_id, before={"file_name": file_name}, actor=g.admin.admin_id)
    return jsonify({"message": "Document deleted."})


@app.route("/api/admin/documents/<int:document_id>/download", methods=["GET"])
@admin_required
def admin_download_document(document_id):
    doc = Document.query.get_or_404(document_id)
    directory = os.path.join(UPLOAD_DIR, os.path.dirname(doc.file_path))
    filename = os.path.basename(doc.file_path)
    return send_from_directory(directory, filename, as_attachment=True, download_name=doc.original_name)


# ==============================================================================
# SECTION 13GA — ADMIN PANEL: LEADS / ENQUIRIES (Phase 4)
# Minimum useful operational layer, not a CRM: list/filter/search, status
# pipeline, follow-up notes, convert-to-customer. Deliberately does NOT
# create Projects — that stays behind the existing Project Request → Admin
# Review → Accept boundary, unchanged.
# ==============================================================================
@app.route("/api/admin/leads", methods=["GET"])
@admin_required
def admin_list_leads():
    status = (request.args.get("status") or "").strip()
    search = (request.args.get("search") or "").strip()
    query = Lead.query
    if status and status in LEAD_STATUS_LABELS:
        query = query.filter_by(status=status)
    if search:
        like = f"%{search}%"
        query = query.filter(db.or_(
            Lead.name.ilike(like), Lead.email.ilike(like), Lead.phone.ilike(like),
            Lead.business_name.ilike(like), Lead.lead_id.ilike(like),
        ))
    leads = query.order_by(Lead.updated_at.desc()).all()
    return jsonify([serialize_lead(l) for l in leads])


@app.route("/api/admin/leads/summary", methods=["GET"])
@admin_required
def admin_leads_summary():
    """Real counts only, straight off the status column — no scoring, no
    fabricated analytics (Phase-4 spec Part Q)."""
    counts = dict(db.session.query(Lead.status, db.func.count(Lead.id)).group_by(Lead.status).all())
    return jsonify({status: counts.get(status, 0) for status in LEAD_STATUS_LABELS})


@app.route("/api/admin/leads/<lead_id>", methods=["GET"])
@admin_required
def admin_get_lead(lead_id):
    lead = Lead.query.filter_by(lead_id=lead_id.upper()).first()
    if not lead:
        return json_error("Lead not found.", 404)
    return jsonify(serialize_lead(lead, include_notes=True))


@app.route("/api/admin/leads/<lead_id>", methods=["PATCH"])
@admin_required
def admin_update_lead(lead_id):
    lead = Lead.query.filter_by(lead_id=lead_id.upper()).first()
    if not lead:
        return json_error("Lead not found.", 404)
    data = request.get_json(silent=True) or {}
    before = {"status": lead.status, "priority": lead.priority}
    errors = {}

    if "status" in data:
        if data["status"] not in LEAD_STATUS_LABELS:
            errors["status"] = "Not a valid status."
        else:
            lead.status = data["status"]
    if "priority" in data:
        if data["priority"] not in {"low", "normal", "high", "urgent"}:
            errors["priority"] = "Not a valid priority."
        else:
            lead.priority = data["priority"]
    if "next_follow_up_at" in data:
        if data["next_follow_up_at"]:
            parsed = parse_deadline_input(data["next_follow_up_at"])
            if not parsed:
                errors["next_follow_up_at"] = "Enter a valid date."
            else:
                lead.next_follow_up_at = parsed
        else:
            lead.next_follow_up_at = None
    for field in ("business_name", "business_category", "budget_range", "preferred_timeline"):
        if field in data:
            setattr(lead, field, (data[field] or "").strip()[:150] or None)

    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    db.session.commit()
    write_audit_log("lead_updated", "lead", lead.id, before=before, after={"status": lead.status, "priority": lead.priority})
    return jsonify(serialize_lead(lead))


@app.route("/api/admin/leads/<lead_id>/notes", methods=["POST"])
@admin_required
def admin_add_lead_note(lead_id):
    lead = Lead.query.filter_by(lead_id=lead_id.upper()).first()
    if not lead:
        return json_error("Lead not found.", 404)
    data = request.get_json(silent=True) or {}
    note_text = (data.get("note") or "").strip()
    if not note_text:
        return json_error("Enter a note.", 400, errors={"note": "This field is required."})
    contact_method = (data.get("contact_method") or "").strip() or None
    if contact_method and contact_method not in LEAD_CONTACT_METHOD_LABELS:
        contact_method = "other"

    note = LeadNote(lead_id=lead.id, note=note_text, contact_method=contact_method, created_by=g.admin.full_name)
    db.session.add(note)
    if contact_method:
        lead.last_contacted_at = _now()
    if data.get("next_follow_up_at"):
        parsed = parse_deadline_input(data["next_follow_up_at"])
        if parsed:
            lead.next_follow_up_at = parsed
    if lead.status == "new":
        lead.status = "contacted"  # a logged contact is the one status transition safe to infer automatically
    db.session.commit()

    write_audit_log("lead_note_added", "lead", lead.id, after={"contact_method": contact_method})
    return jsonify(serialize_lead(lead, include_notes=True)), 201


@app.route("/api/admin/leads/<lead_id>/convert", methods=["POST"])
@admin_required
def admin_convert_lead(lead_id):
    """Links or creates a Customer — never creates a Project. Starting an
    actual project still goes through the existing Start-a-Project flow
    (customer-initiated); preserving that boundary is explicit in the
    Phase-4 spec."""
    lead = Lead.query.filter_by(lead_id=lead_id.upper()).first()
    if not lead:
        return json_error("Lead not found.", 404)
    if lead.status == "converted":
        return json_error("This lead has already been converted.", 409)

    customer = lead.customer or Customer.query.filter_by(email=lead.email).first()
    created_new_customer = False
    if not customer:
        temp_password = gen_temp_password()
        customer = Customer(
            customer_id=gen_customer_id(), full_name=lead.name, email=lead.email,
            phone=lead.phone, company=lead.business_name,
            password_hash=generate_password_hash(temp_password),
            must_change_password=True,
        )
        db.session.add(customer)
        db.session.flush()
        created_new_customer = True

    lead.customer_id = customer.id
    lead.status = "converted"
    lead.converted_at = _now()
    db.session.commit()

    credential_email_sent = None
    if created_new_customer:
        credential_email_sent = send_email(customer.email, "Your Kytron Client Portal account",
                   f"Welcome to Kytron. Your Customer ID is {customer.customer_id}. "
                   f"Temporary password: {temp_password}. Please change it after your first login.")
        write_audit_log(
            "credential_email_sent" if credential_email_sent else "credential_email_failed",
            "customer", customer.customer_id, after={"recipient": customer.email, "source": "lead_conversion"},
        )

    write_audit_log("lead_converted", "lead", lead.id, after={"customer_id": customer.customer_id})
    result = serialize_lead(lead)
    result["new_customer_account"] = created_new_customer
    result["credential_email_sent"] = credential_email_sent if created_new_customer else None
    return jsonify(result)


# ==============================================================================
# SECTION 13H — ADMIN PANEL: CUSTOMERS
# ==============================================================================
@app.route("/api/admin/customers", methods=["GET"])
@admin_required
def admin_list_customers():
    search = (request.args.get("search") or "").strip()
    query = Customer.query
    if search:
        like = f"%{search}%"
        query = query.filter(db.or_(
            Customer.full_name.ilike(like), Customer.email.ilike(like),
            Customer.customer_id.ilike(like), Customer.company.ilike(like),
        ))
    customers = query.order_by(Customer.created_at.desc()).all()
    return jsonify([{
        "customer_id": c.customer_id,
        "name": c.full_name,
        "email": c.email,
        "phone": c.phone,
        "company": c.company,
        "project_count": c.projects.count(),
        "joined": fmt_dt(c.created_at),
    } for c in customers])


@app.route("/api/admin/customers/<customer_id>", methods=["GET"])
@admin_required
def admin_get_customer(customer_id):
    customer = Customer.query.filter_by(customer_id=customer_id.upper()).first()
    if not customer:
        return json_error("Customer not found.", 404)
    return jsonify({
        "customer_id": customer.customer_id,
        "name": customer.full_name,
        "email": customer.email,
        "phone": customer.phone,
        "company": customer.company,
        "project_update_email": bool(customer.project_update_email),
        "account_disabled": bool(customer.account_disabled),
        "joined": fmt_dt(customer.created_at),
        "projects": [serialize_project_summary(p) for p in customer.projects.order_by(Project.created_at.desc()).all()],
    })


@app.route("/api/admin/customers/<customer_id>/disable", methods=["POST"])
@admin_required
def admin_disable_customer(customer_id):
    customer = Customer.query.filter_by(customer_id=customer_id.upper()).first()
    if not customer:
        return json_error("Customer not found.", 404)
    customer.account_disabled = True
    # Also invalidates any session currently open for this customer.
    customer.session_version = (customer.session_version or 1) + 1
    db.session.commit()
    write_audit_log("customer_disabled", "customer", customer.customer_id, actor=g.admin.admin_id)
    return jsonify({"message": "Account disabled."})


@app.route("/api/admin/customers/<customer_id>/enable", methods=["POST"])
@admin_required
def admin_enable_customer(customer_id):
    customer = Customer.query.filter_by(customer_id=customer_id.upper()).first()
    if not customer:
        return json_error("Customer not found.", 404)
    customer.account_disabled = False
    db.session.commit()
    write_audit_log("customer_enabled", "customer", customer.customer_id, actor=g.admin.admin_id)
    return jsonify({"message": "Account enabled."})


# ==============================================================================
# SECTION 13HA — ADMIN PANEL: CROSS-PROJECT DOCUMENTS, COMMUNICATION, ACTIVITY,
# DASHBOARD ACTIONS
# ==============================================================================
@app.route("/api/admin/documents", methods=["GET"])
@admin_required
def admin_documents_index():
    search = (request.args.get("search") or "").strip()
    category = (request.args.get("category") or "").strip()

    query = Document.query.join(Project, isouter=True)
    if category:
        query = query.filter(Document.category == category)
    if search:
        like = f"%{search}%"
        query = query.filter(db.or_(Document.original_name.ilike(like), Project.order_id.ilike(like)))

    docs, meta = paginate_query(query.order_by(Document.upload_date.desc()), request.args, default_per_page=30)
    items = []
    for d in docs:
        item = serialize_document_admin(d)
        item["project_order_id"] = d.project.order_id if d.project else None
        items.append(item)
    return jsonify({"documents": items, "pagination": meta})


@app.route("/api/admin/communication/conversations", methods=["GET"])
@admin_required
def admin_conversations():
    convos = Conversation.query.order_by(Conversation.updated_at.desc()).all()
    return jsonify([serialize_conversation_summary(c, "admin") for c in convos if c.project])


@app.route("/api/admin/communication/conversations/<int:conv_id>/messages", methods=["GET"])
@admin_required
def admin_conversation_messages(conv_id):
    convo = Conversation.query.get_or_404(conv_id)
    msgs = convo.messages.order_by(Message.created_at.asc()).all()
    mark_messages_read(convo, "admin")
    return jsonify([serialize_message(m, "admin") for m in msgs])


@app.route("/api/admin/communication/conversations/<int:conv_id>/messages", methods=["POST"])
@admin_required
def admin_reply_conversation(conv_id):
    convo = Conversation.query.get_or_404(conv_id)
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return json_error("Message can't be empty.", 400, errors={"body": "This field is required."})

    msg = send_message(convo.project, "admin", g.admin.full_name, body)
    return jsonify(serialize_message(msg, "admin")), 201


@app.route("/api/admin/activity-logs", methods=["GET"])
@admin_required
def admin_activity_logs():
    """Exposes the existing internal AuditLog — no separate admin activity
    table. Filterable by action/entity_type and paginated."""
    query = AuditLog.query
    action = (request.args.get("action") or "").strip()
    if action:
        query = query.filter(AuditLog.action == action)
    entity_type = (request.args.get("entity_type") or "").strip()
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)

    logs, meta = paginate_query(query.order_by(AuditLog.created_at.desc()), request.args, default_per_page=30)
    return jsonify({
        "logs": [{
            "id": l.id, "actor": l.actor, "action": l.action,
            "entity_type": l.entity_type, "entity_id": l.entity_id,
            "timestamp": fmt_dt(l.created_at, "%b %d, %Y — %I:%M %p"),
        } for l in logs],
        "pagination": meta,
    })


@app.route("/api/admin/dashboard/actions", methods=["GET"])
@admin_required
def admin_dashboard_actions():
    """Pending-client-action / at-risk detail feed backing the Action
    Center — every item here is a real pending Requirement/Approval/
    Revision/overdue Project row, never invented."""
    items = []

    pending_reqs = Requirement.query.filter(Requirement.status.in_(["submitted", "under_review"])).all()
    for r in pending_reqs:
        items.append({
            "id": f"requirement-{r.id}", "type": "requirement_review",
            "title": f"Review requirement: {r.title}",
            "project_order_id": r.project.order_id, "project_name": r.project.project_name or r.project.project_type,
            "timestamp": fmt_dt(r.submitted_at) if r.submitted_at else fmt_dt(r.requested_at),
        })

    overdue_projects = [p for p in Project.query.filter_by(is_approved=True).all()
                        if compute_deadline_bucket(p.deadline_date)[0] == "overdue"]
    for p in overdue_projects:
        items.append({
            "id": f"overdue-{p.order_id}", "type": "overdue_project",
            "title": f"Overdue: {p.project_name or p.project_type}",
            "project_order_id": p.order_id, "project_name": p.project_name or p.project_type,
            "timestamp": fmt_dt(p.updated_at),
        })

    pending_payments = Payment.query.filter(Payment.status.in_(["pending", "partial"])).all()
    for pay in pending_payments:
        if pay.project:
            items.append({
                "id": f"payment-{pay.id}", "type": "outstanding_payment",
                "title": f"Outstanding payment: {pay.project.order_id}",
                "project_order_id": pay.project.order_id, "project_name": pay.project.project_name or pay.project.project_type,
                "timestamp": fmt_dt(pay.updated_at),
            })

    pending_txns = PaymentTransaction.query.filter_by(status="submitted").all()
    for t in pending_txns:
        if t.project:
            items.append({
                "id": f"txn-{t.id}", "type": "payment_verification",
                "title": f"Verify payment: {format_inr(from_minor_units(t.amount_minor))} on {t.project.order_id}",
                "project_order_id": t.project.order_id, "project_name": t.project.project_name or t.project.project_type,
                "timestamp": fmt_dt(t.submitted_at),
            })

    pending_approvals = Approval.query.filter_by(status="pending").all()
    for a in pending_approvals:
        if a.project:
            items.append({
                "id": f"approval-{a.id}", "type": "approval_pending",
                "title": f"Awaiting customer approval: {a.item_title}",
                "project_order_id": a.project.order_id, "project_name": a.project.project_name or a.project.project_type,
                "timestamp": fmt_dt(a.submitted_at),
            })

    pending_crs = ChangeRequest.query.filter_by(status="submitted").all()
    for cr in pending_crs:
        if cr.project:
            items.append({
                "id": f"changerequest-{cr.id}", "type": "change_request_review",
                "title": f"Review change request: {cr.title}",
                "project_order_id": cr.project.order_id, "project_name": cr.project.project_name or cr.project.project_type,
                "timestamp": fmt_dt(cr.created_at),
            })

    ready_projects = Project.query.filter(Project.status.notin_(["Delivered", "Completed", "Cancelled", "Registered"])).all()
    for p in ready_projects:
        readiness = compute_delivery_readiness(p)
        if readiness["ready"]:
            items.append({
                "id": f"ready-{p.order_id}", "type": "ready_for_delivery",
                "title": f"Ready for delivery: {p.project_name or p.project_type}",
                "project_order_id": p.order_id, "project_name": p.project_name or p.project_type,
                "timestamp": fmt_dt(p.updated_at),
            })

    open_support_statuses = ("submitted", "acknowledged", "open")
    open_support = SupportTicket.query.filter(SupportTicket.status.in_(open_support_statuses)).all()
    for t in open_support:
        items.append({
            "id": f"support-{t.id}", "type": "support_needs_response",
            "title": f"Support request: {t.title or 'General report'}" + (" (urgent)" if t.priority in ("high", "urgent") else ""),
            "project_order_id": t.project.order_id if t.project else None,
            "project_name": (t.project.project_name or t.project.project_type) if t.project else "General (no project)",
            "timestamp": fmt_dt(t.created_at),
        })
    reopened_support = SupportTicket.query.filter_by(status="reopened").all()
    for t in reopened_support:
        items.append({
            "id": f"support-reopened-{t.id}", "type": "support_reopened",
            "title": f"Reopened: {t.title or 'General report'}",
            "project_order_id": t.project.order_id if t.project else None,
            "project_name": (t.project.project_name or t.project.project_type) if t.project else "General (no project)",
            "timestamp": fmt_dt(t.updated_at),
        })

    return jsonify(items)


# ==============================================================================
# SECTION 13I — ADMIN PANEL: SETTINGS
# ==============================================================================
@app.route("/api/admin/settings", methods=["GET"])
@admin_required
def admin_get_settings():
    return jsonify({
        "deadline_due_soon_days": get_setting_int("deadline_due_soon_days"),
        "deadline_critical_days": get_setting_int("deadline_critical_days"),
    })


@app.route("/api/admin/settings", methods=["PATCH"])
@admin_required
def admin_update_settings():
    data = request.get_json(silent=True) or {}
    errors = {}

    for key in ("deadline_due_soon_days", "deadline_critical_days"):
        if key in data:
            try:
                value = int(data[key])
                if value < 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors[key] = "Enter a valid non-negative number of days."
                continue
            set_setting(key, value)

    if errors:
        return json_error("Please correct the highlighted fields.", 400, errors=errors)

    db.session.commit()
    write_audit_log("settings_updated", "settings", None, after=data, actor=g.admin.admin_id)
    return jsonify({
        "message": "Settings saved.",
        "deadline_due_soon_days": get_setting_int("deadline_due_soon_days"),
        "deadline_critical_days": get_setting_int("deadline_critical_days"),
    })


# ==============================================================================
# SECTION 14 — ERROR HANDLERS
# ==============================================================================
@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith("/api/"):
        return json_error("Not found.", 404)
    return e


@app.errorhandler(413)
def handle_413(e):
    return json_error("File exceeds the 50 MB limit.", 413)


@app.errorhandler(500)
def handle_500(e):
    logger.exception("Unhandled server error")
    return json_error("Something went wrong on our end. Please try again.", 500)


# ==============================================================================
# SECTION 15 — STATIC FRONTEND SERVING
# ==============================================================================
# Public marketing site — was entirely unrouted before this pass (only /,
# /project, /admin existed, and / incorrectly served client.html instead of
# the homepage). Every route below just serves its matching static file
# from BASE_DIR, the same pattern already used for client.html/admin.html.
@app.route("/")
def serve_home():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/about")
def serve_about():
    return send_from_directory(BASE_DIR, "about.html")


@app.route("/services")
def serve_services():
    return send_from_directory(BASE_DIR, "services.html")


@app.route("/portfolio")
def serve_portfolio():
    return send_from_directory(BASE_DIR, "portfolio.html")


@app.route("/contact")
def serve_contact():
    return send_from_directory(BASE_DIR, "contact.html")


@app.route("/privacy")
def serve_privacy():
    return send_from_directory(BASE_DIR, "privacy.html")


@app.route("/terms")
def serve_terms():
    return send_from_directory(BASE_DIR, "terms.html")


# Application — client.html was previously served at "/"; it now lives at
# its own finalized route.
@app.route("/client")
def serve_client_portal():
    return send_from_directory(BASE_DIR, "client.html")


# Dedicated AI Consultant page — its own file, its own route. Not embedded
# in client.html; client.html only links to it (see Phase H).
@app.route("/consultant")
def serve_consultant():
    return send_from_directory(BASE_DIR, "consultant.html")


@app.route("/consultant/admin")
def serve_consultant_admin():
    # Page itself has no server-side gate — every API call it makes is
    # already behind @admin_required, same as every other admin.html
    # fetch; this route only serves the static shell.
    return send_from_directory(BASE_DIR, "consultant-admin.html")


@app.route("/project/<order_id>")
def serve_project_details(order_id):
    # order_id in the path is a routing/display convenience only — it
    # grants no access by itself. Actual authorization still happens via
    # POST /api/client/projects/authenticate (order_id + password), exactly
    # as before. project_details.html is expected to read the id from
    # window.location.pathname; unconfirmed for the same reason as above.
    return send_from_directory(BASE_DIR, "project_details.html")


@app.route("/project")
def serve_project_details_legacy():
    """Backward-compat for the old `/project?order=<id>` links Part 1
    flagged client.html as still generating in some places. Redirects into
    the finalized /project/<id> route so no bookmarked/old link 404s;
    remove once client.html is confirmed to emit /project/<id> everywhere."""
    order_id = request.args.get("order")
    if order_id:
        from flask import redirect
        return redirect(f"/project/{order_id}", code=302)
    return send_from_directory(BASE_DIR, "project_details.html")


@app.route("/admin")
def serve_admin_panel():
    return send_from_directory(BASE_DIR, "admin.html")


# ==============================================================================
# SECTION 16 — STARTUP (self-init: create DB, migrate, seed catalog)
# ==============================================================================
def initialize_application():
    with app.app_context():
        run_self_migration()
        seed_catalog_if_empty()
        backfill_payment_currency()
        bootstrap_admin_if_empty()

        from ai_models import get_ai_setting, set_ai_setting
        if get_ai_setting("consultant_enabled") is None:
            set_ai_setting("consultant_enabled", "true")


initialize_application()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
