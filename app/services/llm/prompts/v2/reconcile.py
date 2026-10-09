"""Prompt for the reconciliation pass (Phase 12).

After every section is drafted and validated on its own, one narrow call reads the whole document's prose claims
together and looks for what a per-section check cannot see: two claims in different sections that contradict each
other, the same statement made twice, or one thing named two ways. It may propose a patch (a new wording for one
reworded claim, with a reason); the patch is applied only if the claim still passes the checks.
"""

PROMPT_VERSION = "reconcile/1"

SYSTEM_PROMPT = """You check a migrated procedure document (SOP) as a whole. Each section was written and checked on its
own; you read all its claims together and report only problems BETWEEN claims, usually in different sections:

- contradiction: two claims cannot both be true (different actors, deadlines, approvals or conditions for the same
  thing).
- duplicate: two claims state the same thing, so a reader meets it twice.
- terminology: the same thing is named in two different ways (a role, a system, a document, a term), which could
  make a reader think they are different things.

Rules:
- Report only what you can show by quoting both claims. Different details about one topic are not a contradiction.
- A claim marked (verbatim) is the source's own wording: report a problem in it, but never patch it.
- patch (optional, only for a claim marked (reworded)): the claim's new text that removes the problem without
  changing what it says about anything else. Keep every number, document ID, role name, obligation word
  (must / should / may) and {{ref:...}} token exactly. Never patch to remove content: report duplicates instead.
- claim_ids: the claim IDs exactly as given, the claims involved. note: one or two sentences.
- Return no findings when the document is consistent."""

USER_PROMPT = """DOCUMENT CLAIMS (by section; each line: [claim ID] (kind, verbatim|reworded) text)
{claims}"""
