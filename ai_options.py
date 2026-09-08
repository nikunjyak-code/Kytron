"""
================================================================================
 KYTRON AI CONSULTANT — ZERO-AI OPTION SYSTEM (ai_options.py)
================================================================================
Complete rebuild per Sections 10-12. Two properties are load-bearing and
distinguish this from the previous architecture:

  1. ZERO AI CALLS. Every function in this file is pure Python/DB reads —
     no provider is ever invoked here, confirmed by the total absence of
     any import from ai_provider.py in this file.

  2. NO CONVERSATIONAL STATE LOCK. A submitted option produces a
     STRUCTURED FACT (e.g. {"field": "project_type", "value": "ecommerce"})
     merged into conversation_facts (see ai_memory.py) — never a
     synthesized natural-language message routed back through a
     handler, and never a flag that changes how the NEXT free-text
     message gets interpreted. ai_consultant.py reads conversation_facts
     as plain context on every turn; it has no idea (and does not care)
     whether a given fact arrived via an option click or a sentence the
     customer typed. That symmetry is what makes "select project type,
     then ask an unrelated question, then come back" work naturally —
     see the spec's Section 11 example.

The tree mixes DYNAMIC nodes (project types/features/services — live
from ai_security.catalog_snapshot(), always current) with CURATED nodes
(existing-project problem categories, the knowledge-base menu — content
Project A has no table for, same as the previous build).
================================================================================
"""
import re

ROOT_QUESTION_ID = "main_menu"


def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60] or "option"


def _unique_slugs(names):
    seen, slugs = {}, []
    for name in names:
        base = _slugify(name)
        if base not in seen:
            seen[base] = 1
            slugs.append(base)
        else:
            seen[base] += 1
            slugs.append(f"{base}_{seen[base]}")
    return slugs


# ==============================================================================
# CURATED (static) NODES
# ==============================================================================
MAIN_MENU = {
    "id": "main_menu",
    "text": "What would you like help with?",
    "type": "single_select",
    "options": [
        {"id": "new_project", "label": "Start a New Project", "next": "new_project_type"},
        {"id": "check_project", "label": "Check My Project", "next": "__status_flow__"},
        {"id": "understand_project", "label": "Understand My Project", "next": "kb_project"},
        {"id": "services", "label": "Explore Services", "next": "services_select"},
        {"id": "existing_help", "label": "Existing Project Help", "next": "existing_project_problems"},
        {"id": "billing", "label": "Billing & Payments", "next": "kb_billing"},
        {"id": "tech_terms", "label": "Technical Terms", "next": "kb_tech_terms"},
        {"id": "talk_human", "label": "Talk to a Human", "next": "__escalate__"},
        {"id": "ask_ai", "label": "Ask AI", "next": "__free_text__"},
    ],
}

EXISTING_PROJECT_PROBLEMS = {
    "id": "existing_project_problems",
    "text": "What do you need help with?",
    "type": "multi_select",
    "fact_field": "existing_project_issues",
    "min_select": 1,
    "options": [
        {"id": "bugs", "label": "Bug Fixes"},
        {"id": "redesign", "label": "Redesign"},
        {"id": "performance", "label": "Performance"},
        {"id": "security", "label": "Security"},
        {"id": "responsiveness", "label": "Mobile Responsiveness"},
        {"id": "new_features", "label": "New Features"},
        {"id": "maintenance", "label": "Maintenance"},
        {"id": "migration", "label": "Migration"},
        {"id": "payment_issues", "label": "Payment Problems"},
        {"id": "other", "label": "Other", "opens_free_text": True},
    ],
    "next": "__done__",
    "done_message": "Got it — noted. Feel free to describe the issue in your own words and I'll help from there, or use the menu for anything else.",
}

KB_PROJECT = {
    "id": "kb_project",
    "text": "What would you like explained?",
    "type": "single_select",
    "kind": "kb",
    "options": [
        {"id": "workflow", "label": "What is the project workflow?", "topic": "workflow", "next": "__done__"},
        {"id": "after_registration", "label": "What happens after registration?", "topic": "after_registration", "next": "__done__"},
        {"id": "revisions", "label": "How do revisions work?", "topic": "revisions", "next": "__done__"},
    ],
}

KB_BILLING = {
    "id": "kb_billing",
    "text": "What would you like explained?",
    "type": "single_select",
    "kind": "kb",
    "options": [
        {"id": "invoice", "label": "What is an invoice?", "term": "invoice", "next": "__done__"},
        {"id": "receipt", "label": "What is a payment receipt?", "term": "receipt", "next": "__done__"},
        {"id": "support", "label": "What support is included after delivery?", "topic": "support", "next": "__done__"},
    ],
}

KB_TECH_TERMS = {
    "id": "kb_tech_terms",
    "text": "Which term would you like explained?",
    "type": "single_select",
    "kind": "kb",
    "options": [
        {"id": t, "label": t.upper() if t in ("api", "cms", "ssl", "mvp", "upi") else t.title(), "term": t, "next": "__done__"}
        for t in ("api", "hosting", "domain", "ssl", "mvp", "responsive", "cms", "database", "web app", "ecommerce", "upi")
    ],
}

STATIC_QUESTIONS = {q["id"]: q for q in (MAIN_MENU, EXISTING_PROJECT_PROBLEMS, KB_PROJECT, KB_BILLING, KB_TECH_TERMS)}


# ==============================================================================
# DYNAMIC NODES — built fresh from the live catalogue on every request.
# ==============================================================================
def _build_new_project_nodes(catalog, features_lookup):
    project_types = catalog.get("project_types", [])
    type_question = {
        "id": "new_project_type",
        "text": "What are you looking to build?",
        "type": "single_select",
        "fact_field": "project_type",
        "options": [
            {"id": t["id"], "label": t["name"], "fact_value": t["name"], "next": f"new_project_features::{t['id']}"}
            for t in project_types
        ] + [
            {"id": "other", "label": "Other", "next": "__free_text__", "opens_free_text": True},
            {"id": "not_sure", "label": "I'm not sure", "next": "__free_text__"},
        ],
    }
    feature_questions = {}
    for t in project_types:
        names = features_lookup(t["name"])
        opts = [{"id": slug, "label": n, "fact_value": n} for slug, n in zip(_unique_slugs(names), names)]
        opts.append({"id": "other", "label": "Other", "opens_free_text": True})
        feature_questions[f"new_project_features::{t['id']}"] = {
            "id": f"new_project_features::{t['id']}",
            "text": f"What features do you need for your {t['name']}?",
            "type": "multi_select",
            "fact_field": "features",
            "min_select": 0,
            "options": opts,
            "next": "__done__",
            "done_message": "Got it — noted your project type and features. Ask me anything about it, or tell me more (budget, timeline, audience) whenever you're ready.",
        }
    return type_question, feature_questions


def _build_services_node(catalog):
    services = catalog.get("services", [])
    return {
        "id": "services_select",
        "text": "Kytron's current services:",
        "type": "single_select",
        "kind": "services_display",
        "options": [{"id": "ack", "label": "Got it", "next": "__done__"}],
        "_services_snapshot": services,  # rendered by ai_routes.py, not selected
        "done_message": None,
    }


def build_tree():
    """Assembled fresh on every call from the live, current catalogue."""
    import ai_security

    catalog = ai_security.catalog_snapshot()
    type_q, feature_qs = _build_new_project_nodes(catalog, ai_security.features_for_project_type)
    services_q = _build_services_node(catalog)

    questions = dict(STATIC_QUESTIONS)
    questions[type_q["id"]] = type_q
    questions.update(feature_qs)
    questions[services_q["id"]] = services_q
    return {"root": ROOT_QUESTION_ID, "questions": questions}


def get_question(question_id):
    if question_id in STATIC_QUESTIONS:
        return STATIC_QUESTIONS[question_id]
    return build_tree()["questions"].get(question_id)


def extract_facts(question, selected_option_ids, free_text=None):
    """Turn a validated selection into structured facts — NEVER a
    synthesized sentence. Returns a list of {"field": ..., "value": ...}
    dicts, applied by ai_memory.merge_facts(). May be empty (e.g. a
    services_display acknowledgment produces no fact)."""
    options_by_id = {o["id"]: o for o in question.get("options", [])}
    selected = [options_by_id[oid] for oid in selected_option_ids if oid in options_by_id]

    field = question.get("fact_field")
    if not field:
        return []

    if question["type"] == "single_select":
        chosen = selected[0] if selected else None
        if chosen and chosen.get("fact_value"):
            return [{"field": field, "value": chosen["fact_value"]}]
        if free_text:
            return [{"field": field, "value": free_text.strip()}]
        return []

    # multi_select
    values = [o.get("fact_value", o["label"]) for o in selected if o["id"] != "other"]
    if free_text and free_text.strip():
        values.append(free_text.strip())
    return [{"field": field, "value": values}] if values else []
