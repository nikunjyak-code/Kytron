"""
================================================================================
 KYTRON AI CONSULTANT — TOOLS (ai_tools.py)
================================================================================
Section 6-8. Tools are capabilities that return structured data — none of
them contain conversational logic, and none of them decide authorization.
Authorization is already fixed by the time a tool executes: `conversation`
and `customer` are supplied by ai_routes.py from Project A's own session/
ownership checks, never derived from anything the model said. A tool's
JSON arguments from the model can supply things like a search query or a
proposed order_id/email pair to VERIFY — but never a customer_id, and a
project-scoped tool never returns anything outside what ai_security.py's
existing scoping already allows, regardless of what argument the model
passes.

TOOL_SCHEMAS below are OpenAI-compatible function-calling schemas (Groq,
OpenRouter, and most current providers all speak this exact shape) — see
ai_provider.py for where these get attached to a request with
tool_choice="auto".

select_tools() implements Section 7's "3-5 relevant tools, not the whole
catalogue" — the selection depends ONLY on conversation-level STATE
(logged in? verified?), never on the current message's content. Content-
based tool filtering would smuggle back exactly the deterministic intent
classification Section 4 forbids; state-based filtering is just resource
management and carries no such risk, since the MODEL still decides
whether/which of the offered tools to actually call.
================================================================================
"""
import ai_security
import ai_knowledge
import ai_memory

TOOL_SCHEMAS = {
    "search_kytron_knowledge": {
        "type": "function",
        "function": {
            "name": "search_kytron_knowledge",
            "description": "Search Kytron's knowledge base for relevant facts (technical terms, project workflow, billing concepts) to help answer a question accurately. Returns raw matching facts, not a finished answer.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to search for"}},
                "required": ["query"],
            },
        },
    },
    "get_technical_term": {
        "type": "function",
        "function": {
            "name": "get_technical_term",
            "description": "Look up the definition of a specific technical term (e.g. API, hosting, SSL).",
            "parameters": {
                "type": "object",
                "properties": {"term": {"type": "string"}},
                "required": ["term"],
            },
        },
    },
    "get_services": {
        "type": "function",
        "function": {
            "name": "get_services",
            "description": "Get Kytron's current list of services.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "get_project_types": {
        "type": "function",
        "function": {
            "name": "get_project_types",
            "description": "Get Kytron's current list of project types and, optionally, the features available for one of them.",
            "parameters": {
                "type": "object",
                "properties": {"project_type_name": {"type": "string", "description": "Optional — get features for this specific type"}},
            },
        },
    },
    "record_project_facts": {
        "type": "function",
        "function": {
            "name": "record_project_facts",
            "description": (
                "Record structured facts you've understood from the conversation so far (project_type, business_type, "
                "target_audience, budget, timeline, features, platform, payment_methods, integrations, constraints, "
                "preferences). Call this whenever the person shares or corrects concrete project information — do not "
                "wait to be asked. Only include fields you actually learned just now."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "object",
                        "description": "Field/value pairs, e.g. {\"project_type\": \"e-commerce\", \"budget\": \"20000 INR\"}",
                    }
                },
                "required": ["facts"],
            },
        },
    },
    "verify_project": {
        "type": "function",
        "function": {
            "name": "verify_project",
            "description": "Verify a visitor's project using their Order ID and the email their project is registered under. Only call this with values the person has explicitly provided in this conversation — never guess or invent one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "email": {"type": "string"},
                },
                "required": ["order_id", "email"],
            },
        },
    },
    "get_my_projects": {
        "type": "function",
        "function": {
            "name": "get_my_projects",
            "description": "List the logged-in customer's own projects. Only usable for an authenticated client.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "get_project_status": {
        "type": "function",
        "function": {
            "name": "get_project_status",
            "description": "Get the current status, plain-language status explanation, and next-action guidance for the conversation's verified/authorized project.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "get_project_details": {
        "type": "function",
        "function": {
            "name": "get_project_details",
            "description": "Get the full display-safe summary of the conversation's verified/authorized project (title, status, progress, dates).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "create_consultant_escalation": {
        "type": "function",
        "function": {
            "name": "create_consultant_escalation",
            "description": "Flag this conversation for a human team member to follow up — use when the person asks for a human, or when their need is genuinely beyond what you can resolve.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "Brief reason for the human follow-up"}},
                "required": ["reason"],
            },
        },
    },
}


def select_tools(conversation, customer):
    """State-based selection only — see module docstring. get_services/
    get_project_types are Section 7's PUBLIC catalogue tools and belong
    in every request regardless of auth state (cheap, universally
    relevant, no authorization concern) — omitting them was a real gap
    caught by tracing Section 4's own worked example ("What services
    does Kytron offer?" mid-conversation must be answerable from real
    catalogue data, not left to the model to guess). This runs to 5-6
    tools rather than a strict "3-5" in the project-aware states —
    documented trade-off: shortchanging project-status tools felt worse
    than slightly exceeding the target count."""
    base = ["record_project_facts", "search_kytron_knowledge", "get_services", "get_project_types"]
    if customer:
        extra = ["get_my_projects", "get_project_status"]
    elif conversation.verified_order_id:
        extra = ["get_project_details", "create_consultant_escalation"]
    else:
        extra = ["verify_project", "create_consultant_escalation"]
    return [TOOL_SCHEMAS[n] for n in base + extra]


def execute_tool(tool_name, arguments, conversation, customer):
    """Dispatches to ai_security.py/ai_memory.py. Never raises to the
    caller (a tool error becomes a structured {"error": ...} result the
    model can see and react to, not a crash) and never returns anything
    beyond what ai_security.py's existing authorization already scopes,
    regardless of what the model's `arguments` contain."""
    try:
        if tool_name == "search_kytron_knowledge":
            return {"results": ai_knowledge.search_kytron_knowledge(arguments.get("query", ""))}

        if tool_name == "get_technical_term":
            result = ai_knowledge.get_technical_term(arguments.get("term", ""))
            return result or {"error": "Term not found in Kytron's knowledge base."}

        if tool_name == "get_services":
            return {"services": ai_security.catalog_snapshot()["services"]}

        if tool_name == "get_project_types":
            catalog = ai_security.catalog_snapshot()
            result = {"project_types": catalog["project_types"]}
            type_name = arguments.get("project_type_name")
            if type_name:
                result["features"] = ai_security.features_for_project_type(type_name)
            return result

        if tool_name == "record_project_facts":
            facts = arguments.get("facts") or {}
            if not isinstance(facts, dict):
                return {"error": "facts must be an object"}
            ai_memory.merge_facts(conversation, facts)
            return {"recorded": list(facts.keys())}

        if tool_name == "verify_project":
            # order_id/email come from the MODEL's arguments (which came
            # from what the person typed) — this is fine, since
            # ai_security.verify_project() itself performs the real,
            # authoritative match against Project A's data. The model
            # cannot bypass or weaken that check by what it passes here.
            summary, error = ai_security.verify_project(
                arguments.get("order_id", ""), arguments.get("email", ""), conversation,
            )
            if error:
                return {"verified": False, "error": error}
            return {"verified": True, "project": summary}

        if tool_name == "get_my_projects":
            # `customer` is the authenticated Customer object from
            # ai_routes.py's session check — NEVER anything from
            # `arguments`. The model has no way to ask for someone else's
            # projects; there is no customer_id parameter on this tool.
            if not customer:
                return {"error": "Not logged in — this is only available for an authenticated client."}
            return {"projects": ai_security.my_projects(customer)}

        if tool_name == "get_project_status":
            project = ai_security.project_details(conversation, customer)
            if not project:
                return {"error": "No verified/authorized project in this conversation yet."}
            return {
                "order_id": project["order_id"],
                "status": project["status"],
                "status_explanation": ai_security.explain_status(project["status"]),
                "next_action": ai_security.next_action_guidance(project),
            }

        if tool_name == "get_project_details":
            project = ai_security.project_details(conversation, customer)
            return {"project": project} if project else {"error": "No verified/authorized project in this conversation yet."}

        if tool_name == "create_consultant_escalation":
            # Consultant-owned bookkeeping ONLY — never writes a Project A
            # Lead row (Section: "never create Leads merely because a
            # customer asks for a human" — an explicit requirement from
            # this same rebuild's own prior investigation phase).
            from app import db
            conversation.escalated = True
            conversation.escalation_reason = (arguments.get("reason") or "")[:500]
            db.session.commit()
            return {"escalated": True}

        return {"error": f"Unknown tool: {tool_name}"}

    except Exception as exc:  # noqa: BLE001 — a tool failure must never crash the conversation
        return {"error": "This action couldn't be completed right now."}
