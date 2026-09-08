"""
================================================================================
 KYTRON AI CONSULTANT — KNOWLEDGE CONTENT (ai_knowledge.py)
================================================================================
Static, in-code content — Project A has no FAQ/glossary table to pull
this from (same situation as the previous build; still true, still not
worth inventing a CMS table for). Used in TWO distinct ways, which must
stay distinct per Section 7 ("tools must return structured data, never
conversational state logic"):

  1. The zero-AI option system (ai_options.py) uses TOPICS/GLOSSARY
     directly to render a deterministic, pre-written answer when a
     customer picks a canned question from a menu — no AI involved.

  2. The AI tools (ai_tools.py's get_technical_term/search_kytron_knowledge)
     return the same raw facts as DATA — term + definition, or a list of
     matching snippets — for the model to synthesize into its own
     response. The tool functions here never return a finished chat
     reply; they return facts.
================================================================================
"""

GLOSSARY = {
    "api": "An API is how two pieces of software talk to each other — for example, how a website's frontend fetches order details from a backend.",
    "backend": "The backend is the part of an application that runs on the server — data, logic, and security, out of view of the person using the app.",
    "frontend": "The frontend is everything a user sees and interacts with directly — the layout, buttons, and screens.",
    "database": "A database is where an application's data (users, orders, content) is stored and organized so it can be retrieved reliably.",
    "hosting": "Hosting is the service that keeps a website or app running and accessible on the internet.",
    "domain": "A domain is a site's address on the internet — like yourbusiness.com.",
    "ssl": "SSL/TLS is what puts the padlock in a browser's address bar — it encrypts traffic between a visitor and a site.",
    "mvp": "An MVP (Minimum Viable Product) is the smallest working version of a product that still delivers real value — built first to validate an idea before adding everything else.",
    "responsive": "A responsive design automatically adapts to different screen sizes — phone, tablet, desktop — from a single codebase.",
    "cms": "A CMS (Content Management System) lets someone edit their own website's content (text, images, pages) without touching code.",
    "invoice": "An invoice is the formal billing document issued for a project, showing what's owed and when it's due.",
    "proposal": "A proposal is the document outlining the scope, cost, and timeline proposed for a project before work begins.",
    "milestone": "A milestone is a key checkpoint in a project's timeline — reaching one usually marks a phase of work as complete.",
    "requirement": "A requirement is a specific piece of information or material needed from a client for their project.",
    "receipt": "A payment receipt confirms a payment verified against an order.",
    "revision": "A revision is a requested change to a project after work has started — projects include a set number of revision rounds.",
    "web app": "A web application is a piece of software that runs in a browser and does more than display information — it lets users log in, interact, and perform actions (e.g. a booking system or dashboard).",
    "ecommerce": "An e-commerce site is a website built around selling products or services online, with a catalogue, cart, and checkout.",
    "upi": "UPI (Unified Payments Interface) is a widely used real-time payment system in India that lets customers pay directly from their bank account via apps like GPay or PhonePe.",
}

# Topic-level content for the zero-AI option menus (Understand My Project,
# Billing & Payments) — pre-written answers, never touched by AI.
TOPICS = {
    "workflow": {
        "label": "What is the project workflow?",
        "answer": "Projects move through Registered → Under Process → Testing Phase → Completed, with an On Revision stage if changes are requested, and Ready for Delivery / Delivered once work is finished.",
    },
    "after_registration": {
        "label": "What happens after registration?",
        "answer": "Once a project is registered, our team reviews it, confirms scope and requirements, and moves it into Under Process.",
    },
    "revisions": {
        "label": "How do revisions work?",
        "answer": "Projects include a set number of revision rounds — the exact allowance is shown on the project record. Additional revisions beyond that can be requested and reviewed by the team.",
    },
    "support": {
        "label": "What support is included after delivery?",
        "answer": "Post-launch support duration varies by project and is confirmed at approval. Support status is visible in the Client Portal once a project is live.",
    },
}


def get_technical_term(term):
    """Tool: returns raw {term, definition} data, or None. The model
    decides how to phrase this — this function never writes a reply."""
    if not term:
        return None
    key = term.strip().lower()
    if key in GLOSSARY:
        return {"term": key, "definition": GLOSSARY[key]}
    for k, v in GLOSSARY.items():
        if k in key or key in k:
            return {"term": k, "definition": v}
    return None


def search_kytron_knowledge(query, limit=3):
    """Tool: keyword-overlap search across glossary + topics, returning
    structured matches for the model to synthesize — not a finished
    answer. Deliberately simple (no embeddings/fuzzy matching) — this is
    a small, curated content set, not a search engine."""
    if not query:
        return []
    words = set(query.lower().split())
    results = []
    for term, definition in GLOSSARY.items():
        if term in query.lower() or words & set(term.split()):
            results.append({"type": "term", "term": term, "content": definition})
    for key, topic in TOPICS.items():
        if words & set(topic["label"].lower().split()):
            results.append({"type": "topic", "term": topic["label"], "content": topic["answer"]})
    return results[:limit]
