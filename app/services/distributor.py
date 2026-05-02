"""
Ticket multiplication across accounts (NEW LOGIC):

When the user enters quantity = N, EVERY ready account tries to book N
tickets (clamped by the event's max_per_order). The total booked is
therefore:

    min(N, max_per_order)  ×  len(accounts)

Example: user enters 3, max_per_order = 5, 2 accounts  → each account
books 3, total = 6.

Example: user enters 7, max_per_order = 5, 3 accounts  → each account
books 5 (clamped), total = 15, and the bot tells the user it clamped.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Assignment:
    account_id: str
    quantity: int


def distribute(total: int, *, accounts: list[dict], max_per_order: int,
               min_per_order: int = 1) -> tuple[list[Assignment], dict]:
    """
    Return a list of Assignments (one per ready account, all with the
    same quantity) plus a meta dict describing what happened:

      {
        "requested_per_account": int,     # what user typed
        "actual_per_account":    int,     # after clamp
        "clamped":               bool,
        "accounts_count":        int,
        "total_tickets":         int,
      }

    Raises ValueError only if there are no accounts.
    """
    if not accounts:
        raise ValueError("لا توجد حسابات جاهزة (سجّل دخول حساب أولاً)")
    if total <= 0:
        raise ValueError("العدد يجب أن يكون أكبر من صفر")
    if max_per_order <= 0:
        raise ValueError("max_per_order غير صالح")

    actual = min(max(total, min_per_order), max_per_order)
    clamped = actual != total

    plan = [Assignment(account_id=a["id"], quantity=actual)
            for a in accounts]
    meta = {
        "requested_per_account": total,
        "actual_per_account": actual,
        "clamped": clamped,
        "accounts_count": len(accounts),
        "total_tickets": actual * len(accounts),
    }
    return plan, meta


def describe_plan(plan: list[Assignment], accounts: list[dict],
                  meta: dict) -> str:
    """Human-readable Arabic summary."""
    acc_by_id = {a["id"]: a for a in accounts}
    lines = []
    for a in plan:
        acc = acc_by_id.get(a.account_id, {})
        label = acc.get("label") or acc.get("email", a.account_id)
        lines.append(f"• <code>{label}</code> → <b>{a.quantity}</b> تذكرة")
    if meta.get("clamped"):
        lines.append(
            f"\n⚠️ <i>طلبت {meta['requested_per_account']} لكن الحد الأقصى "
            f"لكل حساب هو {meta['actual_per_account']} في هذه الفعالية.</i>"
        )
    return "\n".join(lines)
