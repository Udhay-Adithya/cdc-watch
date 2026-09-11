"""Telegram command loop for a shared watcher.

Each chat is a separate user with its own Neo ID. Registration is gated by an
invite code: the bot token is a bearer credential, so without a gate anyone
who found the bot could register and use someone else's Pi and mailbox.
"""
import threading
import time

from . import botmeta, config, identity, notify

OFFSET_KEY = "tg_offset"
PENDING_PREFIX = "pending."
# A half-finished prompt should not swallow an unrelated message sent an hour
# later, so pending state expires.
PENDING_TTL = 600

# command -> (pending key, prompt, placeholder)
PROMPTS = {
    "setneo": ("neo_id", "Send me your <b>Neo ID</b>.", "B5R7O9J8"),
    "setreg": ("reg_no", "Send me your <b>registration number</b>.", "23BCE7625"),
    "setname": ("name", "Send me your <b>full name</b>.", "Udhay Adithya J"),
    "addid": ("addid", "Send the <b>reference id</b> to add.\n"
                       "<i>TCS reference or Cognizant Superset id.</i>",
              "CT20264998331"),
    "delid": ("delid", "Send the <b>reference id</b> to remove.", "CT20264998331"),
    "search": ("search", "What should I look for?", "foodhub"),
    "kick": ("kick", "Send the <b>chat id</b> to remove.", "123456789"),
    "start": ("invite", "Send me the <b>invite code</b>.", "invite code"),
}


class Reply:
    """A bot response, optionally asking for one value back."""

    def __init__(self, text, ask=None):
        self.text = text
        self.ask = ask

HELP = """<b>CDC watcher</b>

/whoami — show what is being matched for you
/setneo — set your Neo ID
/setreg — set your registration number
/setname — set your full name
/addid — add a drive-specific id (TCS, Superset)
/delid — remove one
/search — check past CDC mail, e.g. foodhub

<i>Send a command on its own and I will ask for the value — no need to type it on the same line.</i>
/status — the last few drives seen for you
/check — sweep for new CDC mail now
/stop — unregister and stop receiving alerts"""

OWNER_HELP = """
<b>Owner</b>
/users — who is registered
/kick &lt;chat id&gt; — remove someone"""


def _cmd_for(field):
    for cmd, (key, _, _) in PROMPTS.items():
        if key == field:
            return cmd
    return "setneo"


class Bot:
    def __init__(self, store, on_check=None, on_search=None):
        self.store = store
        self.on_check = on_check
        self.on_search = on_search
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
        # Ask for the Neo ID straight away: an account with no id set receives
        # nothing, so leaving it as a follow-up step is how people end up
        # registered and silently unwatched.
        self._set_pending(chat_id, "neo_id")
        return Reply(
            "✅ Registered." + botmeta.STAR + "\n\nNow send me your <b>Neo ID</b>.",
            ask=PROMPTS["setneo"][2],
        )

    # --- pending prompts --------------------------------------------------
    def _set_pending(self, chat_id, key):
        self.store.set(PENDING_PREFIX + str(chat_id),
                       "{}|{}".format(key, int(time.time())))

    def _take_pending(self, chat_id):
        """Read and clear the pending prompt, if it has not expired."""
        raw = self.store.get(PENDING_PREFIX + str(chat_id), "")
        self.store.set(PENDING_PREFIX + str(chat_id), "")
        if not raw or "|" not in raw:
            return None
        key, _, ts = raw.partition("|")
        if time.time() - int(ts) > PENDING_TTL:
            return None
        return key

    def _prompt(self, cmd):
        key, text, placeholder = PROMPTS[cmd]
        return key, Reply(text + "\n\n<i>Or /cancel.</i>", ask=placeholder)

    # --- commands ---------------------------------------------------------
    def handle(self, text, chat_id):
        text = text.strip()
        is_command = text.startswith("/")

        # Read and clear in both cases: a plain message answers the prompt,
        # and any command abandons it rather than leaving it to swallow a
        # later unrelated message.
        pending = self._take_pending(chat_id)
        if pending and not is_command:
            return self._answer(chat_id, pending, text)

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        user = self.store.get_user(chat_id)
        if user is None:
            if cmd == "start" and arg:
                return self._register(chat_id, arg)
            if cmd in ("start", "help"):
                key, reply = self._prompt("start")
                self._set_pending(chat_id, key)
                return Reply(botmeta.WELCOME + "\n\n" + reply.text, ask=reply.ask)
            return "Send /start to register."

        if cmd == "cancel":
            return "Nothing pending." if not pending else "Cancelled."

        # Commands that need a value ask for it instead of failing on usage.
        if cmd in PROMPTS and not arg and cmd != "start":
            if cmd == "kick" and not user.get("is_owner"):
                return "Unknown command. Try /help"
            key, reply = self._prompt(cmd)
            self._set_pending(chat_id, key)
            return reply

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
        if cmd == "search":
            if not self.on_search:
                return "Search is not available in this mode."
            if not arg:
                return "Usage: <code>/search foodhub</code>"
            threading.Thread(target=self._run_search, args=(arg, chat_id),
                             daemon=True).start()
            return "🔎 Searching…"
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

    def _answer(self, chat_id, key, value):
        """Apply a value the user sent in reply to a prompt."""
        if key == "invite":
            return self._register(chat_id, value)
        if self.store.get_user(chat_id) is None:
            return "Send /start to register."
        if key in identity.FIELDS:
            try:
                applied = identity.set_field(self.store, chat_id, key, value)
            except identity.InvalidValue as exc:
                self._set_pending(chat_id, key)
                return Reply("❌ {}\n\nTry again, or /cancel.".format(exc),
                             ask=PROMPTS[_cmd_for(key)][2])
            return "✅ {} set to <code>{}</code>".format(
                identity.LABELS[key], applied)
        if key in ("addid", "delid"):
            try:
                if key == "addid":
                    applied = identity.add_extra_id(self.store, chat_id, value)
                    return "✅ Now also matching <code>{}</code>".format(applied)
                applied = identity.remove_extra_id(self.store, chat_id, value)
                return "✅ No longer matching <code>{}</code>".format(applied)
            except identity.InvalidValue as exc:
                self._set_pending(chat_id, key)
                return Reply("❌ {}\n\nTry again, or /cancel.".format(exc),
                             ask=PROMPTS[key][2])
        if key == "search":
            return self.handle("/search " + value, chat_id)
        if key == "kick":
            user = self.store.get_user(chat_id) or {}
            if not user.get("is_owner"):
                return "Unknown command. Try /help"
            return "✅ Removed." if self.store.remove_user(value.strip()) \
                else "No such user (or that is you)."
        return "Unknown command. Try /help"

    def _run_check(self, chat_id):
        try:
            count = self.on_check()
            notify.send("✅ Sweep done — {} new message(s).".format(count),
                        chat_id=chat_id)
        except Exception as exc:
            notify.send("⚠️ Sweep failed: {}".format(notify._esc(exc)),
                        chat_id=chat_id)

    def _run_search(self, query, chat_id):
        try:
            notify.send(self.on_search(query, chat_id), chat_id=chat_id)
        except Exception as exc:
            notify.send("⚠️ Search failed: {}".format(notify._esc(exc)),
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
                        reply = self.handle(message["text"], chat_id)
                        if not isinstance(reply, Reply):
                            reply = Reply(reply)
                        notify.send(reply.text, chat_id=chat_id,
                                    force_reply=reply.ask)
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
