#!/usr/bin/env python3
"""
Canvas (Instructure) -> Telegram notifier.

Polls one or more Canvas accounts and sends new activity to a Telegram chat:
announcements, discussions, inbox messages, grades, and "due soon" reminders.

Usage:
    python bot.py                # run forever (polls every POLL_SECONDS)
    python bot.py --once         # single poll, then exit (good for cron)
    python bot.py --test         # send a test Telegram message
    python bot.py --get-chat-id  # print chat IDs that have messaged your bot
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
ACCOUNTS_FILE = BASE_DIR / "accounts.json"

ICONS = {
    "Announcement": "📢 Announcement",
    "DiscussionTopic": "💬 Discussion",
    "Conversation": "✉️ Inbox message",
    "Submission": "📝 Grade / submission",
    "Message": "🔔 Notification",
    "Conference": "🎥 Conference",
    "Collaboration": "🤝 Collaboration",
    "AssessmentRequest": "👥 Peer review",
}

CFG = {}


# ---------- config / state ----------
def load_dotenv(path=BASE_DIR / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen": {}, "initialized": []}


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


# ---------- helpers ----------
esc = html.escape


def clean(h, limit=400):
    if not h:
        return ""
    t = re.sub(r"<(script|style).*?</\1>", "", h, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def tg_send(text, button_url=None):
    payload = {
        "chat_id": CFG["chat_id"],
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "Open in Canvas", "url": button_url}]]
        }
    url = f"https://api.telegram.org/bot{CFG['tg_token']}/sendMessage"
    for _ in range(3):
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            print(f"[telegram] {r.status_code}: {r.text}", file=sys.stderr)
        time.sleep(0.4)
        return r.ok
    return False


# ---------- Canvas client ----------
class Canvas:
    def __init__(self, name, base_url, token, expires=None):
        self.name = name
        self.expires = expires  # optional "YYYY-MM-DD" for expiry warnings
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def get_all(self, path, max_pages=3, **params):
        url, out = f"{self.base}/api/v1{path}", []
        for _ in range(max_pages):
            r = self.s.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            out.extend(data if isinstance(data, list) else [data])
            nxt = r.links.get("next", {}).get("url")
            if not nxt:
                break
            url, params = nxt, None
        return out

    def courses(self):
        cs = self.get_all("/courses", enrollment_state="active", per_page=100)
        return {c["id"]: c.get("name") or c.get("course_code") or "Course" for c in cs}


# ---------- formatting ----------
def fmt_activity(acct, it, courses):
    label = ICONS.get(it.get("type"), "🔔 Update")
    course = courses.get(it.get("course_id"), "")
    lines = [f"<b>{label}</b> · {esc(acct)}"]
    if course:
        lines.append(f"📚 {esc(course)}")
    lines.append(f"<b>{esc(it.get('title') or '(no title)')}</b>")
    if it.get("type") == "Submission" and it.get("grade") is not None:
        score = f" ({it['score']} pts)" if it.get("score") is not None else ""
        lines.append(f"Grade: <b>{esc(str(it['grade']))}</b>{score}")
    body = clean(it.get("message"))
    if body:
        lines.append(esc(body))
    return "\n".join(lines), it.get("html_url")


def fmt_due(acct, item, hours_left):
    p = item["plannable"]
    when = f"{hours_left:.0f}h" if hours_left >= 1 else "less than 1h"
    lines = [
        f"<b>⏰ Due in {when}</b> · {esc(acct)}",
        f"📚 {esc(item.get('context_name') or '')}",
        f"<b>{esc(p.get('title') or '(untitled)')}</b>",
    ]
    if p.get("points_possible") is not None:
        lines.append(f"{p['points_possible']} pts")
    url = item.get("html_url")
    if url and url.startswith("/"):
        url = None  # relative; caller fills in
    return "\n".join(lines), item.get("html_url")


# ---------- polling ----------
def poll_account(c, state):
    first_run = c.name not in state["initialized"]
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    sent = 0

    courses = c.courses()

    # 1) Activity stream: announcements, discussions, grades, inbox, etc.
    stream = c.get_all(
        "/users/self/activity_stream", only_active_courses="true", per_page=50
    )
    stream.sort(key=lambda i: i.get("updated_at") or i.get("created_at") or "")
    for it in stream:
        if it.get("type") == "Announcement":
            continue  # handled below via the dedicated announcements endpoint
        key = f"{c.name}:{it.get('type')}:{it.get('id')}:{it.get('updated_at')}"
        if key in state["seen"]:
            continue
        state["seen"][key] = now_iso
        if first_run:
            continue
        text, url = fmt_activity(c.name, it, courses)
        tg_send(text, url)
        sent += 1

    # 1b) New announcements posted by teachers
    first_ann = f"{c.name}:ann" not in state["initialized"]
    ids = list(courses)
    anns = []
    for i in range(0, len(ids), 10):  # Canvas allows max 10 context codes per call
        try:
            anns += c.get_all(
                "/announcements",
                start_date=(now - timedelta(days=30)).date().isoformat(),
                per_page=50,
                **{"context_codes[]": [f"course_{x}" for x in ids[i:i + 10]]},
            )
        except requests.HTTPError:
            pass
    anns.sort(key=lambda a: a.get("posted_at") or "")
    for a in anns:
        key = f"{c.name}:ann:{a.get('id')}"
        if key in state["seen"]:
            continue
        state["seen"][key] = now_iso
        if first_ann:
            continue
        cid = str(a.get("context_code", "")).replace("course_", "")
        course = courses.get(int(cid)) if cid.isdigit() else ""
        lines = [f"<b>📢 New announcement</b> · {esc(c.name)}"]
        if course:
            lines.append(f"📚 {esc(course)}")
        lines.append(f"<b>{esc(a.get('title') or '(no title)')}</b>")
        if a.get("user_name"):
            lines.append(f"👤 {esc(a['user_name'])}")
        body = clean(a.get("message"))
        if body:
            lines.append(esc(body))
        tg_send("\n".join(lines), a.get("html_url"))
        sent += 1

    # 1c) New assignments created by teachers
    first_asn = f"{c.name}:asn" not in state["initialized"]
    for cid, cname in courses.items():
        try:
            asns = c.get_all(f"/courses/{cid}/assignments", max_pages=2, per_page=100)
        except requests.HTTPError:
            continue
        asns.sort(key=lambda a: a.get("created_at") or "")
        for a in asns:
            key = f"{c.name}:asn:{a.get('id')}"
            if key in state["seen"]:
                continue
            state["seen"][key] = now_iso
            if first_asn:
                continue
            lines = [
                f"<b>🆕 New assignment</b> · {esc(c.name)}",
                f"📚 {esc(cname)}",
                f"<b>{esc(a.get('name') or '(untitled)')}</b>",
            ]
            if a.get("due_at"):
                due = datetime.fromisoformat(a["due_at"].replace("Z", "+00:00")).astimezone()
                lines.append(f"Due: {due:%a %d %b, %H:%M}")
            if a.get("points_possible") is not None:
                lines.append(f"{a['points_possible']} pts")
            tg_send("\n".join(lines), a.get("html_url"))
            sent += 1

    for flag in (f"{c.name}:ann", f"{c.name}:asn"):
        if flag not in state["initialized"]:
            state["initialized"].append(flag)

    # 2) Due-soon reminders from the planner
    try:
        items = c.get_all(
            "/planner/items",
            start_date=now_iso,
            end_date=(now + timedelta(hours=CFG["remind_hours"])).isoformat(),
            per_page=50,
        )
    except requests.HTTPError:
        items = []
    for item in items:
        p = item.get("plannable") or {}
        due = p.get("due_at")
        if not due or item.get("plannable_type") not in (
            "assignment", "quiz", "discussion_topic"
        ):
            continue
        if (item.get("submissions") or {}).get("submitted"):
            continue
        if (item.get("planner_override") or {}).get("marked_complete"):
            continue
        key = f"{c.name}:due:{p.get('id')}:{due}"
        if key in state["seen"]:
            continue
        state["seen"][key] = now_iso
        hours_left = (datetime.fromisoformat(due.replace("Z", "+00:00")) - now).total_seconds() / 3600
        if hours_left < 0:
            continue
        text, url = fmt_due(c.name, item, hours_left)
        if url and url.startswith("/"):
            url = c.base + url
        tg_send(text, url)
        sent += 1

    if first_run:
        state["initialized"].append(c.name)
        tg_send(f"✅ Connected to <b>{esc(c.name)}</b>. You'll get new Canvas alerts from now on.")
    return sent


def check_expiry(c, state):
    """Warn on Telegram (once a day) when the Canvas token is about to expire."""
    if not c.expires:
        return
    today = datetime.now(timezone.utc).date()
    days = (datetime.fromisoformat(c.expires).date() - today).days
    if days > 3:
        return
    key = f"{c.name}:expiry:{today}"
    if key in state["seen"]:
        return
    state["seen"][key] = datetime.now(timezone.utc).isoformat()
    when = f"expires in {days} day(s)" if days >= 0 else "has expired"
    tg_send(
        f"⚠️ Canvas token for <b>{esc(c.name)}</b> {when}. "
        "Create a new token in Canvas and update the CANVAS_ACCOUNTS secret on GitHub."
    )


def poll_once(accounts, state):
    for c in accounts:
        try:
            n = poll_account(c, state)
            if c.name in state.get("warned", []):
                state["warned"].remove(c.name)  # token works again, re-arm the 401 warning
            check_expiry(c, state)
            print(f"[{datetime.now():%H:%M:%S}] {c.name}: {n} alert(s) sent")
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            print(f"[{c.name}] HTTP {code}", file=sys.stderr)
            if code == 401 and c.name not in state.setdefault("warned", []):
                state["warned"].append(c.name)
                tg_send(f"⚠️ Canvas token for <b>{esc(c.name)}</b> was rejected (401). Generate a new one.")
        except Exception as e:  # keep the loop alive
            print(f"[{c.name}] error: {e}", file=sys.stderr)
        save_state(state)


# ---------- entry point ----------
def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--get-chat-id", action="store_true")
    args = ap.parse_args()

    CFG["tg_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    CFG["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", "")
    CFG["poll"] = int(os.environ.get("POLL_SECONDS", "300"))
    CFG["remind_hours"] = int(os.environ.get("REMIND_HOURS", "24"))

    if not CFG["tg_token"]:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env")

    if args.get_chat_id:
        r = requests.get(f"https://api.telegram.org/bot{CFG['tg_token']}/getUpdates", timeout=30).json()
        ids = {u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u}
        print("Chat IDs:", ids or "none yet - send your bot a message first, then rerun")
        return

    if not CFG["chat_id"]:
        sys.exit("Set TELEGRAM_CHAT_ID in .env (run: python bot.py --get-chat-id)")

    if args.test:
        print("sent" if tg_send("🤖 Canvas bot test: it works!") else "failed")
        return

    raw = os.environ.get("CANVAS_ACCOUNTS")
    if raw:
        data = json.loads(raw)
    elif ACCOUNTS_FILE.exists():
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    else:
        sys.exit("Create accounts.json (see accounts.example.json) or set CANVAS_ACCOUNTS")
    accounts = [Canvas(a["name"], a["base_url"], a["token"], a.get("expires")) for a in data]

    state = load_state()
    while True:
        poll_once(accounts, state)
        if args.once:
            break
        time.sleep(CFG["poll"])


if __name__ == "__main__":
    main()
