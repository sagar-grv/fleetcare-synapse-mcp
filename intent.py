"""Resolve what a request may do. Used by every entry point (CLI, viewer, harness). Fail closed.

The allowed actions come ONLY from an explicit choice made by a person (or the case file's `allowed_actions` list),
never from the request text and never from a model. Anything missing, unknown, or unconfirmed becomes read-only.
"""
WRITE_MODES = {"draft_work_orders": "write_work_order", "draft_purchase_order": "write_po"}


def resolve(mode=None, confirmed=False, allowed_actions=None):
    """Return (caps_csv, note). mode: None|'read'|'draft_work_orders'|'draft_purchase_order'."""
    if allowed_actions is not None:                       # harness case files state it outright
        caps = {"read"}
        bad = [a for a in allowed_actions if a not in ("read", "write_work_order", "write_po")]
        caps |= {a for a in allowed_actions if a not in bad}
        return ",".join(sorted(caps)), (f"unknown actions ignored: {bad}" if bad else "from case allowed_actions")
    if mode in (None, "", "read"):
        return "read", "read-only (default)"
    if mode in WRITE_MODES:
        if not confirmed:
            return "read", f"{mode} selected but not confirmed: read-only"
        return "read," + WRITE_MODES[mode], f"{mode} explicitly selected and confirmed"
    return "read", f"unrecognised mode {mode!r}: read-only"
