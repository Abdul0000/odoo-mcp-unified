"""Validate read domains before passing them to an Odoo transport."""
from typing import Any


def normalize_domain(domain: Any) -> list:
    if domain is None:
        return []
    if not isinstance(domain, (list, tuple)):
        raise ValueError("domain must be a list of [field, operator, value] conditions")
    # A single condition is unambiguous; wrap it rather than sending its field
    # name as a domain token (which makes Odoo unpack characters as a leaf).
    if len(domain) == 3 and isinstance(domain[0], str) and domain[0] not in {"&", "|", "!"} and isinstance(domain[1], str):
        domain = [domain]
    result = []
    needed = 1
    for index, token in enumerate(domain):
        if needed == 0:
            needed = 1  # Odoo implicitly ANDs successive expressions.
        if isinstance(token, str) and token in {"&", "|", "!"}:
            needed += {"&": 1, "|": 1, "!": 0}[token]
            result.append(token)
            continue
        if not isinstance(token, (list, tuple)) or len(token) != 3:
            raise ValueError(f"domain[{index}] must be [field, operator, value] or &, |, !")
        field, operator, value = token
        if not isinstance(field, (str, int)) or isinstance(field, bool) or not isinstance(operator, str) or not operator:
            raise ValueError(f"domain[{index}] requires a field and an operator")
        result.append([field, operator, value])
        needed -= 1
    if result and needed:
        raise ValueError("domain has a logical operator with missing operands")
    return result
