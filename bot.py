#!/usr/bin/env python3
"""
Canvas (Instructure) -> Telegram notifier.

Alerts sent to Telegram:
  - new announcements / edited announcements
  - new assignments / edited assignments (title, due date, points, dates, description)
  - new items in course modules (lectures, files, pages, links, videos)
  - renamed module items
  - grades, inbox messages, discussions (Canvas activity feed)
  - "due soon" reminders
Commands (answered at the next check): /upcoming [days], /courses, /status, /help

Usage:
    python bot.py                # run forever (polls every POLL_SECONDS)
    python bot.py --once         # single poll, then exit (GitHub Actions / cron)
    python bot.py --test         # send a test Telegram message
    python bot.py --get-chat-id  # print chat IDs that have messaged your bot
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # very old Python
    ZoneInfo = None

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
MODULE_ICONS = {
    "File": "📄", "Page": "📃", "Discussion": "💬", "Quiz": "❓",
    "ExternalUrl": "🔗", "ExternalTool": "🧩",
}
FIELD_LABELS = {
    "name": "Title", "title": "Title", "due_at": "Due date", "points_possible": "Points",
    "unlock_at": "Available from", "lock_at": "Available until",
}
DATE_FIELDS = ("due_at", "unlock_at", "lock_at")

CFG = {"tz": timezone.utc}
esc = html.escape


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
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
    state.setdefault("seen", {})        # key -> iso time last (re)confirmed
    state.setdefault("fp", {})          # key -> {"t": iso, "v": {field: value}} for change detection
    state.setdefault("initialized", [])
    return state


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    state["fp"] = {k: v for k, v in state["fp"].items() if v["t"] >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def seen_check(state, key, now):
    """Return True if `key` is new. Refreshes the timestamp rarely so live items never expire."""
    old = state["seen"].get(key)
    if old is None or old < (now - timedelta(days=30)).isoformat():
        state["seen"][key] = now.isoformat()
    return old is None


def fp_check(state, key, vals, now):
    """Compare fingerprint to the stored one. Returns {field: (old, new)} of changes.
    The first time an item is fingerprinted nothing is reported (it just gets stored)."""
    rec = state["fp"].get(key)
    if rec is None:
        state["fp"][key] = {"t": now.isoformat(), "v": vals}
        return {}
    old = rec["v"]
    changed = {k: (old.get(k), vals.get(k)) for k in vals if old.get(k) != vals.get(k)}
    if changed:
        state["fp"][key] = {"t": now.isoformat(), "v": vals}
    elif rec["t"] < (now - timedelta(days=30)).isoformat():
        rec["t"] = now.isoformat()
    return changed


# ---------- helpers ----------
def clean(h, limit=400):
    if not h:
        return ""
    t = re.sub(r"<(script|style).*?</\1>", "", h, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def digest(h):
    """Short hash of the visible text of an HTML body (ignores tags and link tokens)."""
    text = re.sub(r"\s+", " ", clean(h, limit=10**7)).strip()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def local(dt_str):
    if not dt_str:
        return "none"
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(CFG["tz"])
    return f"{dt:%a %d %b, %H:%M}"


def change_lines(changed):
    out = []
    for k, (o, n) in changed.items():
        if k == "desc":
            out.append("• Description edited")
        elif k == "body":
            out.append("• Text edited")
        elif k in DATE_FIELDS:
            out.append(f"• {FIELD_LABELS[k]}: {esc(local(o))} → {esc(local(n))}")
        else:
            show = lambda v: esc(str(v)) if v not in (None, "") else "none"
            out.append(f"• {FIELD_LABELS.get(k, k)}: {show(o)} → {show(n)}")
    return out


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


# ---------- feature: activity feed (grades, inbox, discussions, ...) ----------
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


def poll_stream(c, state, courses, now, first_run):
    sent = 0
    stream = c.get_all("/users/self/activity_stream", only_active_courses="true", per_page=50)
    stream.sort(key=lambda i: i.get("updated_at") or i.get("created_at") or "")
    for it in stream:
        if it.get("type") == "Announcement":
            continue  # handled by poll_announcements
        key = f"{c.name}:{it.get('type')}:{it.get('id')}:{it.get('updated_at')}"
        if not seen_check(state, key, now) or first_run:
            continue
        text, url = fmt_activity(c.name, it, courses)
        tg_send(text, url)
        sent += 1
    return sent


# ---------- feature: announcements (new + edited) ----------
def poll_announcements(c, state, courses, now):
    first = f"{c.name}:ann" not in state["initialized"]
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
    sent = 0
    for a in anns:
        key = f"{c.name}:ann:{a.get('id')}"
        vals = {"title": a.get("title") or "", "body": digest(a.get("message"))}
        is_new = seen_check(state, key, now)
        changed = fp_check(state, key, vals, now)
        cid = str(a.get("context_code", "")).replace("course_", "")
        course = courses.get(int(cid)) if cid.isdigit() else ""
        if is_new and not first:
            head = "📢 New announcement"
        elif not is_new and changed:
            head = "✏️ Announcement edited"
        else:
            continue
        lines = [f"<b>{head}</b> · {esc(c.name)}"]
        if course:
            lines.append(f"📚 {esc(course)}")
        lines.append(f"<b>{esc(vals['title'] or '(no title)')}</b>")
        if a.get("user_name"):
            lines.append(f"👤 {esc(a['user_name'])}")
        if is_new:
            body = clean(a.get("message"))
            if body:
                lines.append(esc(body))
        else:
            if "title" in changed:
                lines.append(f"• Title was: {esc(changed['title'][0] or 'none')}")
            if "body" in changed:
                lines.append("• Text edited:")
                lines.append(esc(clean(a.get("message"))))
        tg_send("\n".join(lines), a.get("html_url"))
        sent += 1
    return sent


# ---------- feature: assignments (new + edited) ----------
def poll_assignments(c, state, courses, now):
    first = f"{c.name}:asn" not in state["initialized"]
    sent = 0
    for cid, cname in courses.items():
        try:
            asns = c.get_all(f"/courses/{cid}/assignments", max_pages=2, per_page=100)
        except requests.HTTPError:
            continue
        asns.sort(key=lambda a: a.get("created_at") or "")
        for a in asns:
            key = f"{c.name}:asn:{a.get('id')}"
            vals = {
                "name": a.get("name") or "",
                "due_at": a.get("due_at"),
                "points_possible": a.get("points_possible"),
                "unlock_at": a.get("unlock_at"),
                "lock_at": a.get("lock_at"),
                "desc": digest(a.get("description")),
            }
            is_new = seen_check(state, key, now)
            changed = fp_check(state, key, vals, now)
            if is_new and not first:
                lines = [
                    f"<b>🆕 New assignment</b> · {esc(c.name)}",
                    f"📚 {esc(cname)}",
                    f"<b>{esc(vals['name'] or '(untitled)')}</b>",
                ]
                if a.get("due_at"):
                    lines.append(f"Due: {esc(local(a['due_at']))}")
                if a.get("points_possible") is not None:
                    lines.append(f"{a['points_possible']} pts")
            elif not is_new and changed:
                lines = [
                    f"<b>✏️ Assignment updated</b> · {esc(c.name)}",
                    f"📚 {esc(cname)}",
                    f"<b>{esc(vals['name'] or '(untitled)')}</b>",
                ] + change_lines(changed)
            else:
                continue
            tg_send("\n".join(lines), a.get("html_url"))
            sent += 1
    return sent


# ---------- feature: module items (new lectures/files/pages/links) ----------
def poll_modules(c, state, courses, now):
    first = f"{c.name}:mod" not in state["initialized"]
    sent = 0
    for cid, cname in courses.items():
        try:
            mods = c.get_all(f"/courses/{cid}/modules", max_pages=3, per_page=50,
                             **{"include[]": ["items"]})
        except requests.HTTPError:
            continue
        for m in mods:
            items = m.get("items")
            if items is None:  # Canvas may omit items inline for big modules
                try:
                    items = c.get_all(f"/courses/{cid}/modules/{m['id']}/items", per_page=100)
                except requests.HTTPError:
                    continue
            for it in items:
                if it.get("type") == "SubHeader":
                    continue
                key = f"{c.name}:mod:{it.get('id')}"
                title = it.get("title") or "(untitled)"
                is_new = seen_check(state, key, now)
                changed = fp_check(state, key, {"title": title}, now)
                if it.get("type") == "Assignment":
                    continue  # assignments already alert via poll_assignments
                icon = MODULE_ICONS.get(it.get("type"), "📌")
                if is_new and not first:
                    lines = [
                        f"<b>📚 New in module</b> · {esc(c.name)}",
                        f"🎓 {esc(cname)}",
                        f"📂 {esc(m.get('name') or 'Module')}",
                        f"{icon} <b>{esc(title)}</b>",
                    ]
                elif not is_new and changed:
                    lines = [
                        f"<b>✏️ Module item renamed</b> · {esc(c.name)}",
                        f"🎓 {esc(cname)}",
                        f"📂 {esc(m.get('name') or 'Module')}",
                        f"{icon} <b>{esc(title)}</b>",
                    ] + change_lines(changed)
                else:
                    continue
                tg_send("\n".join(lines), it.get("html_url"))
                sent += 1
    return sent


# ---------- feature: due-soon reminders ----------
def poll_due(c, state, now):
    sent = 0
    try:
        items = c.get_all(
            "/planner/items",
            start_date=now.isoformat(),
            end_date=(now + timedelta(hours=CFG["remind_hours"])).isoformat(),
            per_page=50,
        )
    except requests.HTTPError:
        return 0
    for item in items:
        p = item.get("plannable") or {}
        due = p.get("due_at")
        if not due or item.get("plannable_type") not in ("assignment", "quiz", "discussion_topic"):
            continue
        if (item.get("submissions") or {}).get("submitted"):
            continue
        if (item.get("planner_override") or {}).get("marked_complete"):
            continue
        if not seen_check(state, f"{c.name}:due:{p.get('id')}:{due}", now):
            continue
        hours_left = (datetime.fromisoformat(due.replace("Z", "+00:00")) - now).total_seconds() / 3600
        if hours_left < 0:
            continue
        when = f"{hours_left:.0f}h" if hours_left >= 1 else "less than 1h"
        lines = [
            f"<b>⏰ Due in {when}</b> · {esc(c.name)}",
            f"📚 {esc(item.get('context_name') or '')}",
            f"<b>{esc(p.get('title') or '(untitled)')}</b>",
            f"Due: {esc(local(due))}",
        ]
        url = item.get("html_url")
        if url and url.startswith("/"):
            url = c.base + url
        tg_send("\n".join(lines), url)
        sent += 1
    return sent


# ---------- orchestration ----------
def poll_account(c, state):
    now = datetime.now(timezone.utc)
    first_run = c.name not in state["initialized"]
    courses = c.courses()
    sent = 0
    sent += poll_stream(c, state, courses, now, first_run)
    sent += poll_announcements(c, state, courses, now)
    sent += poll_assignments(c, state, courses, now)
    sent += poll_modules(c, state, courses, now)
    sent += poll_due(c, state, now)
    for flag in (c.name, f"{c.name}:ann", f"{c.name}:asn", f"{c.name}:mod"):
        if flag not in state["initialized"]:
            state["initialized"].append(flag)
    if first_run:
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


# ---------- Telegram commands ----------
HELP = (
    "<b>Commands</b>\n"
    "/upcoming [days] - deadlines (default 7 days)\n"
    "/courses - your active courses\n"
    "/status - is the bot running\n"
    "/help - this message\n\n"
    "Replies arrive at the bot's next check (about every 10 min on GitHub)."
)


def cmd_upcoming(accounts, days):
    now = datetime.now(timezone.utc)
    rows = []
    for c in accounts:
        try:
            items = c.get_all(
                "/planner/items", start_date=now.isoformat(),
                end_date=(now + timedelta(days=days)).isoformat(), per_page=50,
            )
        except requests.HTTPError:
            continue
        for it in items:
            p = it.get("plannable") or {}
            if not p.get("due_at") or it.get("plannable_type") not in ("assignment", "quiz", "discussion_topic"):
                continue
            if (it.get("submissions") or {}).get("submitted"):
                continue
            if (it.get("planner_override") or {}).get("marked_complete"):
                continue
            rows.append((p["due_at"], p.get("title") or "(untitled)", it.get("context_name") or ""))
    rows.sort()
    if not rows:
        return f"🎉 Nothing due in the next {days} day(s)."
    lines = [f"<b>⏰ Due in the next {days} day(s)</b> ({len(rows)})"]
    for due, title, course in rows[:20]:
        lines.append(f"• {esc(local(due))} - <b>{esc(title)}</b> ({esc(course)})")
    if len(rows) > 20:
        lines.append(f"… and {len(rows) - 20} more")
    return "\n".join(lines)


def cmd_courses(accounts):
    lines = []
    for c in accounts:
        try:
            names = sorted(c.courses().values())
        except Exception:
            continue
        lines.append(f"<b>{esc(c.name)}</b> ({len(names)})")
        lines += [f"• {esc(n)}" for n in names]
    return "\n".join(lines) or "No courses found."


def handle_commands(accounts, state):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{CFG['tg_token']}/getUpdates",
            params={"offset": state.get("tg_offset", 0), "timeout": 0}, timeout=30,
        ).json()
    except Exception as e:
        print(f"[commands] {e}", file=sys.stderr)
        return
    for u in r.get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(CFG["chat_id"]):
            continue  # ignore anyone except you
        parts = (m.get("text") or "").strip().split()
        if not parts:
            continue
        cmd = parts[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            tg_send(HELP)
        elif cmd == "/upcoming":
            days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 7
            tg_send(cmd_upcoming(accounts, max(1, min(days, 30))))
        elif cmd == "/courses":
            tg_send(cmd_courses(accounts))
        elif cmd == "/status":
            tg_send(
                f"✅ Running. Watching {len(accounts)} Canvas account(s). "
                f"Due-soon reminders: {CFG['remind_hours']}h before the deadline."
            )


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
    tzname = os.environ.get("TIMEZONE", "Asia/Phnom_Penh")
    try:
        CFG["tz"] = ZoneInfo(tzname)
    except Exception:
        print(f"[warn] unknown TIMEZONE '{tzname}', showing times in UTC", file=sys.stderr)

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
        handle_commands(accounts, state)
        save_state(state)
        if args.once:
            break
        time.sleep(CFG["poll"])


if __name__ == "__main__":
    main()
