"""Telegram command loop for a shared watcher.

Each chat is a separate user with its own Neo ID. Registration is gated by an
invite code: the bot token is a bearer credential, so without a gate anyone
who found the bot could register and use someone else's Pi and mailbox.
"""
import threading
import time

import requests

from . import botmeta, config, identity, notify

OFFSET_KEY = "tg_offset"

HELP = """<b>CDC watcher</b>

/whoami — show what is being matched for you
/setneo &lt;id&gt; — set your Neo ID (e.g. B5R7O9J8)
/setreg &lt;no&gt; — set your registration number (e.g. 23BCE7625)
/setname &lt;name&gt; — set your full name
/addid &lt;ref&gt; — add a drive-specific id (TCS CT…, Superset 8254250)
/delid &lt;ref&gt; — remove one
/status — the last few drives seen for you
/check — sweep for new CDC mail now
/stop — unregister and stop receiving alerts"""

OWNER_HELP = """
<b>Owner</b>
/users — who is registered
/kick &lt;chat id&gt; — remove someone"""


class Bot:
    def __init__(self, store, on_check=None):
        self.store = store
        self.on_check = on_check
        self._stop = threading.Event()

    # --- transport --------------------------------------------------------
    def _get_updates(self, offset, timeout=30):
        resp = notify._session.get(
            notify.API.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates"),
            params={"offset": offset, "timeout": timeout},
            timeout=timeout + 15,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    # --- registration -----------------------------------------------------
    def _register(self, chat_id, arg):
        if not config.INVITE_CODE:
            return ("This watcher is private. Ask the owner to set an invite "
                    "code if you should have access.")
        if arg.strip() != config.INVITE_CODE:
            return "❌ Wrong invite code."
        self.store.add_user(chat_id)
        return ("✅ Registered.\n\nNow set your Neo ID:\n"
                "<code>/setneo B5R7O9J8</code>\n\n"
                "You will only get alerts once it is set." + botmeta.STAR)

    # --- commands ---------------------------------------------------------
    def handle(self, text, chat_id):
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        user = self.store.get_user(chat_id)
        if user is None:
            if cmd == "start" and arg:
                return self._register(chat_id, arg)
            if cmd in ("start", "help"):
                return botmeta.WELCOME
            return "Send <code>/start &lt;invite code&gt;</code> to register."

        is_owner = bool(user.get("is_owner"))

        if cmd in ("start", "help"):
            return HELP + (OWNER_HELP if is_owner else "")
        if cmd == "whoami":
            return self._whoami(chat_id)
        if cmd in ("setneo", "setreg", "setname"):
            field = {"setneo": "neo_id", "setreg": "reg_no", "setname": "name"}[cmd]
            if not arg:
                return "Usage: /{} &lt;value&gt;".format(cmd)
            try:
                value = identity.set_field(self.store, chat_id, field, arg)
            except identity.InvalidValue as exc:
                return "❌ {}".format(exc)
            return "✅ {} set to <code>{}</code>".format(identity.LABELS[field], value)
        if cmd in ("addid", "delid"):
            if not arg:
                return "Usage: /{} &lt;reference id&gt;".format(cmd)
            try:
                if cmd == "addid":
                    value = identity.add_extra_id(self.store, chat_id, arg)
                    return "✅ Now also matching <code>{}</code>".format(value)
                value = identity.remove_extra_id(self.store, chat_id, arg)
                return "✅ No longer matching <code>{}</code>".format(value)
            except identity.InvalidValue as exc:
                return "❌ {}".format(exc)
        if cmd == "status":
            return self._status(chat_id)
        if cmd == "stop":
            if is_owner:
                return "The owner cannot unregister."
            self.store.remove_user(chat_id)
            return "Unregistered. Send /start with the invite code to return."
        if cmd == "check":
            if not self.on_check:
                return "Sweep is not available in this mode."
            threading.Thread(target=self._run_check, args=(chat_id,),
                             daemon=True).start()
            return "🔎 Sweeping for new CDC mail…"
        if cmd == "users" and is_owner:
            return self._users()
        if cmd == "kick" and is_owner:
            removed = self.store.remove_user(arg.strip())
            return "✅ Removed." if removed else "No such user (or that is you)."
        return "Unknown command. Try /help"

    def _run_check(self, chat_id):
        try:
            count = self.on_check()
            notify.send("✅ Sweep done — {} new message(s).".format(count),
                        chat_id=chat_id)
        except Exception as exc:
            notify.send("⚠️ Sweep failed: {}".format(notify._esc(exc)),
                        chat_id=chat_id)

    def _whoami(self, chat_id):
        me = identity.current(self.store, chat_id)
        lines = ["<b>Matching for you</b>"]
        for field in identity.FIELDS:
            lines.append("{}: <code>{}</code>".format(
                identity.LABELS[field], me[field] or "— not set —"))
        lines.append("Reference ids: {}".format(
            ", ".join("<code>{}</code>".format(e) for e in me["extra_ids"]) or "— none —"))
        if not identity.is_usable(me):
            lines.append("\n⚠️ No id set, so you are not being matched yet.")
        return "\n".join(lines)

    def _status(self, chat_id):
        rows = self.store.timeline(chat_id=chat_id)[-8:]
        if not rows:
            return "Nothing seen yet."
        icon = {"FOUND": "🎉", "ABSENT": "⚪️", "UNPARSED": "⚠️", "NO_LIST": "·"}
        lines = ["<b>Recent</b>"]
        for row in rows:
            lines.append("{} {} — {}".format(
                icon.get(row["status"], "·"),
                notify._esc((row["company"] or row["subject"] or "?")[:40]),
                notify._esc(row["round_label"] or row["status"])))
        return "\n".join(lines)

    def _users(self):
        lines = ["<b>Registered</b>"]
        for user in self.store.list_users():
            lines.append("<code>{}</code> — {} {}".format(
                user["chat_id"], user.get("neo_id") or "(no id)",
                "· owner" if user["is_owner"] else ""))
        return "\n".join(lines)

    # --- loop -------------------------------------------------------------
    def poll_forever(self):
        offset = int(self.store.get(OFFSET_KEY, 0) or 0)
        while not self._stop.is_set():
            try:
                for update in self._get_updates(offset):
                    offset = update["update_id"] + 1
                    self.store.set(OFFSET_KEY, offset)
                    message = update.get("message") or update.get("edited_message")
                    if not message or "text" not in message:
                        continue
                    chat_id = str(message["chat"]["id"])
                    try:
                        notify.send(self.handle(message["text"], chat_id),
                                    chat_id=chat_id)
                    except Exception:
                        pass  # one bad chat must not stop the loop
            except Exception:
                time.sleep(10)  # network blip; getUpdates resumes from offset

    def start(self):
        thread = threading.Thread(target=self.poll_forever, daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop.set()
