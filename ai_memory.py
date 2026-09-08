"""
================================================================================
 KYTRON AI CONSULTANT — DETERMINISTIC MEMORY (ai_memory.py)
================================================================================
Section 13: compact conversation memory, deterministic where possible —
no extra AI call merely to summarize. Two things happen here, both plain
Python:

  1. merge_facts() — folds a facts dict (from an option selection via
     ai_options.extract_facts(), OR from the model's own
     record_project_facts tool call when it understands something from
     free text — see ai_tools.py) into conversation.conversation_facts.
     List-valued fields (features, payment methods, ...) are unioned;
     everything else is overwritten, so a correction ("actually it's for
     my company") naturally replaces the old value the next time the
     model calls record_project_facts with the update it inferred.

  2. update_rolling_summary() — once the recent-message window is
     exceeded, folds the overflow into a short deterministic summary
     (truncation + join, exactly like the facts themselves — no
     "summarize this conversation" AI call).

conversation_facts is DATA, never an instruction. See ai_prompts.py's
system prompt for the explicit rule that context (including this) must
never be treated as something to obey.
================================================================================
"""
from app import db

LIST_FIELDS = {"features", "payment_methods", "integrations", "constraints", "open_questions", "existing_project_issues"}

MAX_RECENT_MESSAGES = 10
SUMMARY_TRIGGER_MESSAGE_COUNT = MAX_RECENT_MESSAGES * 2


def merge_facts(conversation, updates: dict):
    if not updates:
        return
    facts = conversation.get_facts()
    for field, value in updates.items():
        if field in LIST_FIELDS:
            existing = facts.get(field) or []
            if not isinstance(existing, list):
                existing = [existing]
            incoming = value if isinstance(value, list) else [value]
            merged = existing + [v for v in incoming if v not in existing]
            facts[field] = merged
        else:
            facts[field] = value
    conversation.set_facts(facts)
    db.session.commit()


def facts_summary_text(facts: dict) -> str:
    """Compact, human-readable rendering for the system prompt — not a
    transcript, just what's currently known."""
    if not facts:
        return ""
    parts = []
    for field, value in facts.items():
        if isinstance(value, list):
            if value:
                parts.append(f"{field}: {', '.join(str(v) for v in value)}")
        elif value:
            parts.append(f"{field}: {value}")
    return "; ".join(parts)


def maybe_update_rolling_summary(conversation):
    from ai_models import AIMessage

    total = conversation.messages.count()
    if total < SUMMARY_TRIGGER_MESSAGE_COUNT:
        return
    overflow = (
        conversation.messages.order_by(AIMessage.created_at.asc())
        .limit(total - MAX_RECENT_MESSAGES).all()
    )
    highlights = [m.content[:120] for m in overflow if m.role == "user"][-8:]
    if highlights:
        conversation.rolling_summary = "Earlier in this conversation: " + " | ".join(highlights)
        db.session.commit()


def recent_messages(conversation, limit=MAX_RECENT_MESSAGES):
    from ai_models import AIMessage
    return list(reversed(
        conversation.messages.order_by(AIMessage.created_at.desc()).limit(limit).all()
    ))
