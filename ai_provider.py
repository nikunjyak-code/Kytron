"""
================================================================================
 KYTRON AI CONSULTANT — PROVIDER ROUTER WITH TOOL-CALLING (ai_provider.py)
================================================================================
Section 6/17/18. Groq (primary) -> OpenRouter free-tier (secondary) ->
Gemini (tertiary) -> deterministic fallback, now with genuine
tool-calling (tool_choice="auto") — the previous build's provider layer
was pure text completion with no function-calling at all, which is why
this had to be rebuilt rather than patched.

ai_consultant.py never sees a provider-specific detail: it calls
generate_with_fallback() once with a tool schema list and gets back
either a final answer, a list of tool calls to execute, or ok=False (in
which case it falls back to a deterministic reply exactly as a
single-provider failure always did).

Groq and OpenRouter are OpenAI-chat-completions-compatible (including
their tool-calling shape) — called with stdlib urllib only, no new SDK
dependency. Gemini's function-calling API has a different native shape
(types.Tool/FunctionDeclaration, function_call response parts); this file
translates OpenAI-shaped tool schemas to and from Gemini's format so
ai_tools.py only needs to define one schema shape for all three
providers.

--------------------------------------------------------------------------
COST-SAFETY NOTE — read before touching OpenRouterProvider
--------------------------------------------------------------------------
OpenRouter has no separate "free API" — the SAME endpoint serves both
free and paid models, distinguished only by whether the model ID ends in
the literal suffix ":free". Calling a model ID WITHOUT that suffix bills
the account if it has credit. The free-model lineup rotates
unpredictably. OpenRouterProvider therefore has NO default model and
REFUSES to call at all unless OPENROUTER_MODEL is explicitly set AND
ends with ":free" — verified directly, not assumed, in the investigation
that preceded this rebuild.
================================================================================
"""
import os
import json
import time
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

logger = logging.getLogger("kytron.ai_consultant")


@dataclass
class ProviderMessage:
    role: str                          # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: Optional[List[dict]] = None   # assistant message requesting tools: [{"id", "name", "arguments"}]
    tool_call_id: Optional[str] = None        # tool-result message: which call this answers
    name: Optional[str] = None                # tool-result message: which tool ran


@dataclass
class ProviderResponse:
    text: str = ""
    provider: str = ""
    ok: bool = True
    error: Optional[str] = None
    tokens_used: Optional[int] = None
    tool_calls: List[dict] = field(default_factory=list)  # [{"id", "name", "arguments"}] — empty if a final answer


def _redact(text, env_key_name):
    if not text:
        return text
    key_value = os.environ.get(env_key_name)
    if key_value and key_value in text:
        return text.replace(key_value, "[REDACTED]")
    return text


class AIProvider:
    name = "base"

    def generate(self, system_prompt: str, messages: List[ProviderMessage], tools: Optional[list] = None) -> ProviderResponse:
        raise NotImplementedError

    def is_configured(self) -> bool:
        raise NotImplementedError


# ==============================================================================
# Shared HTTP mechanics for any OpenAI-chat-completions-compatible endpoint —
# Groq and OpenRouter both speak this exact request/response/tool shape.
# ==============================================================================
class _OpenAICompatibleProvider(AIProvider):
    BASE_URL = None
    ENV_KEY = None
    ENV_MODEL = None
    DEFAULT_MODEL = None
    EXTRA_HEADERS = {}
    REQUEST_TIMEOUT_SECONDS = 20

    def is_configured(self) -> bool:
        return bool(os.environ.get(self.ENV_KEY))

    def _model_name(self):
        return os.environ.get(self.ENV_MODEL, self.DEFAULT_MODEL)

    def _validate_model(self, model):
        return (True, None) if model else (False, f"{self.ENV_MODEL} not set")

    def _to_wire_messages(self, system_prompt, messages):
        wire = [{"role": "system", "content": system_prompt}]
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                wire.append({
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                        for tc in m.tool_calls
                    ],
                })
            elif m.role == "tool":
                wire.append({"role": "tool", "tool_call_id": m.tool_call_id, "name": m.name, "content": m.content})
            else:
                wire.append({"role": m.role, "content": m.content})
        return wire

    def generate(self, system_prompt, messages, tools=None) -> ProviderResponse:
        import ai_usage

        if not self.is_configured():
            return ProviderResponse(provider=self.name, ok=False, error="provider_not_configured")

        model = self._model_name()
        valid, reason = self._validate_model(model)
        if not valid:
            logger.warning("%s: refusing to call — %s", self.name, reason)
            ai_usage.record_error(self.name, reason, quota=False)
            return ProviderResponse(provider=self.name, ok=False, error="misconfigured_model")

        payload = {
            "model": model,
            "messages": self._to_wire_messages(system_prompt, messages),
            "temperature": 0.4,
            "max_tokens": 700,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Authorization": f"Bearer {os.environ[self.ENV_KEY]}", "Content-Type": "application/json"}
        headers.update(self.EXTRA_HEADERS)

        started = time.monotonic()
        request = urllib.request.Request(
            self.BASE_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.REQUEST_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
            latency_ms = int((time.monotonic() - started) * 1000)
            message = ((body.get("choices") or [{}])[0].get("message") or {})
            tokens = (body.get("usage") or {}).get("total_tokens")

            raw_tool_calls = message.get("tool_calls") or []
            tool_calls = []
            for tc in raw_tool_calls:
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except (ValueError, KeyError):
                    args = {}
                tool_calls.append({"id": tc.get("id", ""), "name": tc["function"]["name"], "arguments": args})

            ai_usage.record_success(self.name, tokens_used=tokens, latency_ms=latency_ms, tool_calls_count=len(tool_calls))
            return ProviderResponse(
                text=message.get("content") or "", provider=self.name, ok=True,
                tokens_used=tokens, tool_calls=tool_calls,
            )

        except urllib.error.HTTPError as exc:
            message = _redact(f"HTTP {exc.code}: {exc.reason}", self.ENV_KEY)
            is_quota = exc.code == 429
            ai_usage.record_error(self.name, message, quota=is_quota)
            error_kind = "quota_exceeded" if is_quota else ("auth_error" if exc.code in (401, 403) else "provider_error")
            return ProviderResponse(provider=self.name, ok=False, error=error_kind)

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            ai_usage.record_error(self.name, _redact(str(exc), self.ENV_KEY), quota=False)
            return ProviderResponse(provider=self.name, ok=False, error="connection_error")

        except (ValueError, KeyError, IndexError, TypeError) as exc:
            ai_usage.record_error(self.name, _redact(f"malformed response: {exc}", self.ENV_KEY), quota=False)
            return ProviderResponse(provider=self.name, ok=False, error="malformed_response")


class GroqProvider(_OpenAICompatibleProvider):
    name = "groq"
    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
    ENV_KEY = "GROQ_API_KEY"
    ENV_MODEL = "GROQ_MODEL"
    DEFAULT_MODEL = "llama-3.3-70b-versatile"


class OpenRouterProvider(_OpenAICompatibleProvider):
    name = "openrouter"
    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    ENV_KEY = "OPENROUTER_API_KEY"
    ENV_MODEL = "OPENROUTER_MODEL"
    DEFAULT_MODEL = None
    EXTRA_HEADERS = {"X-Title": "Kytron AI Consultant"}

    def _validate_model(self, model):
        if not model:
            return False, "OPENROUTER_MODEL not set — no default is provided on purpose, see cost-safety note"
        if not model.endswith(":free"):
            return False, f"OPENROUTER_MODEL '{model}' does not end with ':free' — refusing to risk a paid call"
        return True, None


class GeminiProvider(AIProvider):
    """Tertiary. Translates the same OpenAI-shaped tool schemas to
    Gemini's native function-declaration format, and translates Gemini's
    function_call response parts back to the same {"id","name","arguments"}
    shape every other provider returns — ai_consultant.py never needs to
    know the difference."""

    name = "gemini"
    ENV_KEY = "GEMINI_API_KEY"
    ENV_MODEL = "GEMINI_MODEL"
    DEFAULT_MODEL = "gemini-2.5-flash"

    def is_configured(self) -> bool:
        return bool(os.environ.get(self.ENV_KEY))

    def generate(self, system_prompt, messages, tools=None) -> ProviderResponse:
        import ai_usage

        if not self.is_configured():
            return ProviderResponse(provider=self.name, ok=False, error="provider_not_configured")

        started = time.monotonic()
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=os.environ[self.ENV_KEY])
            model = os.environ.get(self.ENV_MODEL, self.DEFAULT_MODEL)

            contents = []
            for m in messages:
                if m.role == "assistant" and m.tool_calls:
                    parts = [types.Part.from_function_call(name=tc["name"], args=tc["arguments"]) for tc in m.tool_calls]
                    contents.append(types.Content(role="model", parts=parts))
                elif m.role == "tool":
                    contents.append(types.Content(
                        role="function",
                        parts=[types.Part.from_function_response(name=m.name, response={"result": m.content})],
                    ))
                else:
                    contents.append(types.Content(
                        role=("user" if m.role == "user" else "model"),
                        parts=[types.Part.from_text(text=m.content)],
                    ))

            gemini_tools = None
            if tools:
                declarations = [
                    types.FunctionDeclaration(
                        name=t["function"]["name"],
                        description=t["function"]["description"],
                        parameters=t["function"]["parameters"],
                    )
                    for t in tools
                ]
                gemini_tools = [types.Tool(function_declarations=declarations)]

            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt, temperature=0.4, max_output_tokens=700,
                    tools=gemini_tools,
                ),
            )

            latency_ms = int((time.monotonic() - started) * 1000)
            tokens = getattr(getattr(response, "usage_metadata", None), "total_token_count", None)

            tool_calls = []
            text = ""
            candidate = (response.candidates or [None])[0]
            if candidate and candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    fc = getattr(part, "function_call", None)
                    if fc:
                        tool_calls.append({"id": fc.name, "name": fc.name, "arguments": dict(fc.args or {})})
                    elif getattr(part, "text", None):
                        text += part.text

            ai_usage.record_success(self.name, tokens_used=tokens, latency_ms=latency_ms, tool_calls_count=len(tool_calls))
            return ProviderResponse(text=text, provider=self.name, ok=True, tokens_used=tokens, tool_calls=tool_calls)

        except Exception as exc:  # noqa: BLE001 — provider errors must never propagate as a 500
            message = _redact(str(exc), self.ENV_KEY)
            logger.warning("Gemini request failed: %s", message)
            is_quota = any(t in message.lower() for t in ("quota", "rate limit", "429", "resource_exhausted"))
            ai_usage.record_error(self.name, message, quota=is_quota)
            return ProviderResponse(provider=self.name, ok=False, error="quota_exceeded" if is_quota else "provider_error")


PROVIDER_CHAIN = [GroqProvider, OpenRouterProvider, GeminiProvider]
_PROVIDER_REGISTRY = {p.name: p for p in PROVIDER_CHAIN}


def get_provider(name: Optional[str] = None) -> AIProvider:
    provider_cls = _PROVIDER_REGISTRY.get((name or "groq").strip().lower(), GroqProvider)
    return provider_cls()


def provider_config_status(provider: AIProvider) -> dict:
    """API-key presence and model validity are separate concerns — see
    the investigation that established this distinction. Never touches a
    credential value."""
    key_present = provider.is_configured()
    if not key_present:
        return {"key_present": False, "model_configured": None, "model_issue": None}
    model_fn = getattr(provider, "_model_name", None)
    validate_fn = getattr(provider, "_validate_model", None)
    if model_fn is None or validate_fn is None:
        return {"key_present": True, "model_configured": True, "model_issue": None}
    valid, reason = validate_fn(model_fn())
    return {"key_present": True, "model_configured": valid, "model_issue": None if valid else reason}


def generate_with_fallback(system_prompt: str, messages: List[ProviderMessage], tools: Optional[list] = None) -> ProviderResponse:
    """Tries Groq -> OpenRouter -> Gemini, skipping unconfigured/
    individually-cooled-down providers, stopping at the first success —
    "success" meaning either real text OR at least one tool call
    (ai_consultant.py handles both). Only genuine failures advance the
    chain; a provider is never retried within the same request."""
    import ai_usage

    last_response = ProviderResponse(provider="none", ok=False, error="no_provider_attempted")
    attempted = []

    for provider_cls in PROVIDER_CHAIN:
        provider = provider_cls()
        if not provider.is_configured():
            continue
        if not ai_usage.should_attempt_provider(provider.name):
            ai_usage.record_skip(provider.name)
            continue

        if attempted:
            ai_usage.record_fallback(attempted[-1], provider.name)
        attempted.append(provider.name)

        response = provider.generate(system_prompt, messages, tools=tools)
        if response.ok and (response.text or response.tool_calls):
            return response
        last_response = response

    if not attempted:
        last_response = ProviderResponse(provider="none", ok=False, error="no_provider_configured")
    return last_response
