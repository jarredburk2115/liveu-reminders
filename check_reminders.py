"""Emails photographers who still have a LiveU backpack checked out after N hours.

Runs on a schedule (GitHub Actions). Each reminder is sent once per checkout.
Reads/writes the same Firestore document the LiveU board uses (liveu-board/state).
"""
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import quote
from zoneinfo import ZoneInfo

REMINDER_HOURS = float(os.environ.get("REMINDER_HOURS") or "9")
TZ = ZoneInfo(os.environ.get("TIMEZONE") or "America/Chicago")
DRY_RUN = (os.environ.get("DRY_RUN") or "").strip().lower() in ("1", "true", "yes")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def find_due(state, now):
    """Pure function: returns the checkouts that need a reminder right now."""
    due = []
    for unit_id, co in (state.get("checkouts") or {}).items():
        if co.get("reminderSentAt"):
            continue
        email = (co.get("email") or "").strip()
        if not email or not co.get("checkedOutAt"):
            continue
        out_at = parse_iso(co["checkedOutAt"])
        if now - out_at < timedelta(hours=REMINDER_HOURS):
            continue
        due.append({
            "unit_id": unit_id,
            "name": co.get("photographer") or "there",
            "email": email,
            "assignment": co.get("assignment") or "",
            "checked_out_at": co["checkedOutAt"],
        })
    return due


def board_link(state, unit_id):
    base = (state.get("boardUrl") or "").strip().rstrip("/")
    return f"{base}?unit={quote(unit_id)}" if base else ""


def build_message(item, link, sender):
    out_local = parse_iso(item["checked_out_at"]).astimezone(TZ)
    when = out_local.strftime("%-I:%M %p on %b %-d")
    hours = int(REMINDER_HOURS) if REMINDER_HOURS == int(REMINDER_HOURS) else REMINDER_HOURS
    lines = [
        f"Hi {item['name']},",
        "",
        f"LiveU backpack {item['unit_id']} was checked out to you at {when}"
        + (f" for: {item['assignment']}." if item["assignment"] else "."),
        f"That was {hours}+ hours ago.",
        "",
        "If you're finished with it, please check it in so it's available for the next crew."
        + (f"\nOne tap: {link}" if link else ""),
        "",
        "If you still need it, no action is needed. This reminder is only sent once.",
    ]
    msg = EmailMessage()
    msg["Subject"] = f"Reminder: check in LiveU backpack {item['unit_id']}"
    msg["From"] = sender
    msg["To"] = item["email"]
    msg.set_content("\n".join(lines))
    return msg


def send(msg):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)


def main():
    import firebase_admin
    from firebase_admin import credentials, firestore

    creds = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
    firebase_admin.initialize_app(creds)
    db = firestore.client()
    ref = db.collection("liveu-board").document("state")

    @firestore.transactional
    def claim(tx):
        """Marks due checkouts as reminded (atomically) and returns them."""
        snap = ref.get(transaction=tx)
        if not snap.exists or not (snap.to_dict() or {}).get("payload"):
            return [], {}
        state = json.loads(snap.to_dict()["payload"])
        due = find_due(state, datetime.now(timezone.utc))
        if due and not DRY_RUN:
            stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            for item in due:
                state["checkouts"][item["unit_id"]]["reminderSentAt"] = stamp
            tx.set(ref, {
                "payload": json.dumps(state, ensure_ascii=False),
                "updatedAt": firestore.SERVER_TIMESTAMP,
            })
        return due, state

    @firestore.transactional
    def unclaim(tx, unit_id, checked_out_at):
        """If an email failed, clear the mark so the next run retries."""
        snap = ref.get(transaction=tx)
        state = json.loads(snap.to_dict()["payload"])
        co = (state.get("checkouts") or {}).get(unit_id)
        if co and co.get("checkedOutAt") == checked_out_at:
            co["reminderSentAt"] = None
            tx.set(ref, {
                "payload": json.dumps(state, ensure_ascii=False),
                "updatedAt": firestore.SERVER_TIMESTAMP,
            })

    due, state = claim(db.transaction())
    if not due:
        print("No reminders due.")
        return

    sender = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER", "")
    failures = 0
    for item in due:
        msg = build_message(item, board_link(state, item["unit_id"]), sender)
        if DRY_RUN:
            print(f"[DRY RUN] would email {item['email']} about {item['unit_id']}")
            continue
        try:
            send(msg)
            print(f"Sent reminder for {item['unit_id']} to {item['email']}")
        except Exception as exc:  # retry on the next run
            failures += 1
            print(f"FAILED for {item['unit_id']}: {exc}", file=sys.stderr)
            unclaim(db.transaction(), item["unit_id"], item["checked_out_at"])
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
