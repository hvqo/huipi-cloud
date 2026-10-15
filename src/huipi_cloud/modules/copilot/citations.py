"""Validate explicit citation identities from this run; never infer semantic correctness."""

import re

from huipi_cloud.modules.copilot.protocol import CopilotError


def cited_ids(answer: str, available: dict):
    labels = re.findall(r"\[\[([^\[\]\n]{1,100})\]\]", answer)
    stripped = re.sub(r"\[\[[^\[\]\n]{1,100}\]\]", "", answer)
    if "[[" in stripped or "]]" in stripped:
        raise CopilotError("invalid_citation_format")
    if any(label not in available for label in labels):
        raise CopilotError("unknown_citation_id")
    return list(dict.fromkeys(labels))
