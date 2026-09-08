"""
================================================================================
 KYTRON AI CONSULTANT — PROVIDER USAGE TRACKING (ai_usage.py)
================================================================================
Records every provider call attempt and decides, per provider
independently, whether that provider is in cooldown. ai_provider.py's
generate_with_fallback() calls should_attempt_provider(name) before
trying each provider in the chain — a Groq failure never puts OpenRouter
or Gemini in cooldown, confirmed by every query below being filtered by
the `provider` column.
================================================================================
"""
from datetime import datetime, timedelta

from app import db
from ai_models import AIUsageEvent

COOLDOWN_SECONDS = 30
EXTENDED_COOLDOWN_SECONDS = 300
COOLDOWN_ERROR_THRESHOLD = 3
ERROR_EVENT_TYPES = ("call_error", "quota_exceeded")


def record_success(provider, tokens_used=None, latency_ms=None, tool_calls_count=None):
    db.session.add(AIUsageEvent(
        event_type="call_success", provider=provider, tokens_used=tokens_used,
        latency_ms=latency_ms, tool_calls_count=tool_calls_count,
    ))
    db.session.commit()


def record_error(provider, error_message, quota=False):
    db.session.add(AIUsageEvent(
        event_type="quota_exceeded" if quota else "call_error",
        provider=provider, error_message=(error_message or "")[:500],
    ))
    db.session.commit()


def record_skip(provider):
    db.session.add(AIUsageEvent(event_type="cooldown_skip", provider=provider))
    db.session.commit()


def record_fallback(from_provider, to_provider):
    db.session.add(AIUsageEvent(
        event_type="provider_fallback", provider=to_provider,
        error_message=f"{from_provider} -> {to_provider}"[:500],
    ))
    db.session.commit()


def _recent_events(seconds, provider=None):
    since = datetime.utcnow() - timedelta(seconds=seconds)
    query = AIUsageEvent.query.filter(AIUsageEvent.created_at >= since)
    if provider:
        query = query.filter(AIUsageEvent.provider == provider)
    return query


def should_attempt_provider(provider):
    recent_errors = _recent_events(COOLDOWN_SECONDS, provider).filter(
        AIUsageEvent.event_type.in_(ERROR_EVENT_TYPES)
    ).count()
    if recent_errors == 0:
        return True

    last_success = (
        AIUsageEvent.query.filter_by(event_type="call_success", provider=provider)
        .order_by(AIUsageEvent.created_at.desc()).first()
    )
    since = last_success.created_at if last_success else datetime.utcnow() - timedelta(days=1)
    consecutive_errors = AIUsageEvent.query.filter(
        AIUsageEvent.event_type.in_(ERROR_EVENT_TYPES),
        AIUsageEvent.provider == provider,
        AIUsageEvent.created_at > since,
    ).count()

    window = EXTENDED_COOLDOWN_SECONDS if consecutive_errors >= COOLDOWN_ERROR_THRESHOLD else COOLDOWN_SECONDS
    return _recent_events(window, provider).filter(AIUsageEvent.event_type.in_(ERROR_EVENT_TYPES)).count() == 0


def provider_state(provider_instance):
    from ai_provider import provider_config_status

    name = provider_instance.name
    last_24h = _recent_events(24 * 3600, name)
    config = provider_config_status(provider_instance)
    return {
        "name": name,
        "configured": config["key_present"],
        "model_configured": config["model_configured"],
        "model_issue": config["model_issue"],
        "in_cooldown": config["key_present"] and not should_attempt_provider(name),
        "calls_last_24h": last_24h.filter_by(event_type="call_success").count(),
        "errors_last_24h": last_24h.filter(AIUsageEvent.event_type.in_(ERROR_EVENT_TYPES)).count(),
    }


def availability_state():
    from ai_provider import PROVIDER_CHAIN

    providers = [provider_state(cls()) for cls in PROVIDER_CHAIN]
    active = next(
        (p["name"] for p in providers if p["configured"] and p["model_configured"] is not False and not p["in_cooldown"]),
        None,
    )
    last_24h = _recent_events(24 * 3600)
    return {
        "active_provider": active,
        "any_provider_available": active is not None,
        "providers": providers,
        "calls_last_24h": last_24h.filter_by(event_type="call_success").count(),
        "errors_last_24h": last_24h.filter(AIUsageEvent.event_type.in_(ERROR_EVENT_TYPES)).count(),
        "fallback_skips_last_24h": last_24h.filter_by(event_type="cooldown_skip").count(),
        "provider_fallbacks_last_24h": last_24h.filter_by(event_type="provider_fallback").count(),
    }
