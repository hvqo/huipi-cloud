"""Fixed web operations over existing paper services; no deployment/grant endpoints."""

PREFIX = "/papers"
POLICIES = {
    "paper_web_list": ("GET", "", None),
    "paper_web_sources": ("GET", "/sources", "paper.create"),
    "paper_web_create": ("POST", "", "paper.create"),
    "paper_web_read": ("GET", "/{paper_id}", "paper.read"),
    "paper_web_history": ("GET", "/{paper_id}/history", "paper.read"),
    "paper_web_edit": ("POST", "/{paper_id}/questions", "paper.edit"),
    "paper_web_reorder": ("POST", "/{paper_id}/order", "paper.edit"),
    "paper_web_lock": ("POST", "/{paper_id}/lock", "paper.lock"),
    "paper_web_fork": ("POST", "/{paper_id}/fork", "paper.edit"),
    "paper_web_retire": ("POST", "/{paper_id}/retire", "paper.edit"),
}
