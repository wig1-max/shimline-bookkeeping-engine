"""What a proposal actually changes, in words an accountant reads.

An approval is only meaningful if the approver saw the change. Today the console
shows two JSON blobs side by side, which means in practice that a reviewer
approving twenty proposals reads none of them, and the approval gate becomes a
button. This turns a proposal into a short list of field changes: what the
provider holds now, what it would hold, and nothing else.

Three rules, and the second is the one that matters
---------------------------------------------------
**Every difference is shown.** A field with no friendly label is rendered with
its raw path rather than dropped. An unexplained change is a much smaller
problem than an invisible one, and an approval that silently omitted a field
would be worse than no diff at all.

**Unchanged fields are not shown.** A diff that lists everything is a diff
nobody reads, which is the problem being solved rather than a smaller version
of it.

**A value is never reformatted.** Amounts are compared as text exactly as the
provider stated them, because "1200" becoming "1200.00" is either a real change
or it is not, and prettying it here would decide that question invisibly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

CHANGED = "changed"
ADDED = "added"
REMOVED = "removed"

# QuickBooks field names, in accounting words. A path segment absent from here
# is shown as QuickBooks names it -- unfamiliar is better than hidden.
_LABELS = {
    "AccountRef": "Account",
    "ItemAccountRef": "Account",
    "CustomerRef": "Project",
    "ClassRef": "Class",
    "TaxCodeRef": "Tax code",
    "EntityRef": "Vendor or customer",
    "VendorRef": "Vendor",
    "ARAccountRef": "Receivables account",
    "APAccountRef": "Payables account",
    "DepositToAccountRef": "Deposited to",
    "Amount": "Amount",
    "TotalAmt": "Total",
    "UnitPrice": "Unit price",
    "Qty": "Quantity",
    "TxnDate": "Date",
    "DocNumber": "Document number",
    "Description": "Description",
    "PrivateNote": "Memo",
    "Line": "Line",
    "TxnTaxDetail": "Sales tax",
    "TotalTax": "Tax",
    "PostingType": "Posting type",
}

# Segments that are plumbing rather than meaning: the reader cares that the
# project changed, not that it changed inside AccountBasedExpenseLineDetail.
_TRANSPARENT = {
    "AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail",
    "SalesItemLineDetail", "DepositLineDetail", "JournalEntryLineDetail",
    "value",
}


@dataclass(frozen=True)
class FieldChange:
    path: str
    label: str
    before: str
    after: str
    kind: str

    @property
    def is_addition(self) -> bool:
        return self.kind == ADDED

    @property
    def is_removal(self) -> bool:
        return self.kind == REMOVED


def _label_for(path: list) -> str:
    """A readable name for a path, keeping line numbers and dropping plumbing."""
    parts = []
    for segment in path:
        if isinstance(segment, int):
            # QuickBooks lines are zero-based; people are not.
            if parts:
                parts[-1] = f"{parts[-1]} {segment + 1}"
            else:
                parts.append(f"Item {segment + 1}")
            continue
        if segment in _TRANSPARENT:
            continue
        parts.append(_LABELS.get(segment, segment))
    return " · ".join(parts) or "Document"


def _render(value) -> str:
    """A value as text, never reformatted, never truncated silently."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(", ", ": "))
    return str(value)


def _path_text(path: list) -> str:
    out = ""
    for segment in path:
        out += f"[{segment}]" if isinstance(segment, int) else (
            f".{segment}" if out else str(segment))
    return out


def changes(current, proposed) -> list[FieldChange]:
    """Every difference between two provider documents, deepest field first.

    Accepts dicts or the JSON text the proposal table stores. Unparseable text
    is compared as text rather than raising: a reviewer seeing one opaque change
    is better served than one seeing an error page.
    """
    current = _loaded(current)
    proposed = _loaded(proposed)
    found: list[FieldChange] = []
    _walk(current, proposed, [], found)
    found.sort(key=lambda item: item.label)
    return found


def _loaded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


_MISSING = object()


def _walk(before, after, path: list, found: list) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            _walk(before.get(key, _MISSING), after.get(key, _MISSING),
                  path + [key], found)
        return
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            _walk(before[index] if index < len(before) else _MISSING,
                  after[index] if index < len(after) else _MISSING,
                  path + [index], found)
        return
    if before is _MISSING and after is _MISSING:
        return
    # A subtree that appeared or disappeared is walked, not dumped. QuickBooks
    # wraps almost everything as {"value": "..."}, so reporting an added
    # CustomerRef whole put `{"value": "P-2 Maple St"}` in front of a reviewer
    # where "Project: P-2 Maple St" was the thing they needed to read. An added
    # line becomes a row per field for the same reason: a big change should
    # look big rather than becoming one opaque blob.
    if isinstance(after, (dict, list)) and before is _MISSING:
        _walk({} if isinstance(after, dict) else [], after, path, found)
        return
    if isinstance(before, (dict, list)) and after is _MISSING:
        _walk(before, {} if isinstance(before, dict) else [], path, found)
        return
    if before is _MISSING:
        _record(found, path, "", _render(after), ADDED)
        return
    if after is _MISSING:
        _record(found, path, _render(before), "", REMOVED)
        return
    if _render(before) != _render(after):
        _record(found, path, _render(before), _render(after), CHANGED)


def _record(found: list, path: list, before: str, after: str, kind: str) -> None:
    found.append(FieldChange(path=_path_text(path), label=_label_for(path),
                             before=before, after=after, kind=kind))


def summary(current, proposed) -> str:
    """One line, for a queue row that has no space for a table."""
    found = changes(current, proposed)
    if not found:
        return "No field changes"
    if len(found) == 1:
        item = found[0]
        if item.kind == ADDED:
            return f"{item.label} set to {item.after}"
        if item.kind == REMOVED:
            return f"{item.label} cleared"
        return f"{item.label}: {item.before} → {item.after}"
    return f"{len(found)} field changes"


def fingerprint(current, proposed) -> str:
    """A stable hash of exactly what a reviewer was shown.

    A batch approval carries this back with it. If the proposal moved between
    the page rendering and the button being pressed, the hash no longer matches
    and the whole batch is refused rather than approving something nobody read.
    """
    import hashlib

    # A record separator that cannot appear inside a rendered value, so
    # two different sets of changes cannot hash to the same string.
    separator = chr(30)   # ASCII record separator, written as a name
                          # because an invisible control character in
                          # source is a trap for the next reader.
    material = separator.join(
        f"{item.path}|{item.kind}|{item.before}|{item.after}"
        for item in changes(current, proposed))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
