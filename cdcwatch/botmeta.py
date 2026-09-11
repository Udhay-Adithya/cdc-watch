"""Bot profile text and command menus, pushed to telegram via the bot api.

Everything here is settable programmatically. The two things that are not --
the bot's profile photo and the picture shown above the description -- have
no bot api method at all and must be set through @BotFather.
"""
from . import config, notify

REPO = "https://github.com/Udhay-Adithya/cdc-watch"

NAME = "VITAP CDC Watch"

# Shown on the bot's profile page. Hard limit 120 characters.
SHORT_DESCRIPTION = (
    "Watches VIT-AP CDC placement mail and tells you the moment your Neo ID "
    "lands on a shortlist."
)

# Shown in an empty chat, under "What can this bot do?". Hard limit 512.
DESCRIPTION = (
    "I read placement mail from the VIT-AP CDC and check every shortlist for "
    "your Neo ID — the moment it arrives, not whenever you next open your "
    "inbox.\n\n"
    "Lists are read from the mail body or the attached sheet. Drives that use "
    "their own ids (TCS reference numbers, Cognizant Superset ids) work too.\n\n"
    "🎉 you are on the list — with the sheet attached\n"
    "⚪️ not on this one — quietly, and never called a rejection\n"
    "⚠️ the list could not be read — so you can check it yourself\n\n"
    "Send /start with an invite code to begin."
)

# Public menu. Owner-only commands are added for the owner's chat alone, so
# other users never see controls they cannot use.
COMMANDS = [
    ("start", "register with an invite code"),
    ("whoami", "show what is being matched for you"),
    ("setneo", "set your neo id, e.g. /setneo B5R7O9J8"),
    ("setreg", "set your registration number"),
    ("setname", "set your full name"),
    ("addid", "add a drive specific id (tcs, superset)"),
    ("delid", "remove a drive specific id"),
    ("search", "check past cdc mail, e.g. /search foodhub"),
    ("status", "recent drives seen for you"),
    ("check", "sweep for new cdc mail now"),
    ("stop", "unregister and stop alerts"),
    ("cancel", "abandon whatever i just asked for"),
    ("help", "show every command"),
]

OWNER_COMMANDS = COMMANDS + [
    ("users", "list registered users"),
    ("kick", "remove a user by chat id"),
]

WELCOME = (
    "👋 <b>VITAP CDC Watch</b>\n\n"
    "I watch CDC placement mail and check every shortlist for your Neo ID, so "
    "you hear about it the moment it lands.\n\n"
    "To register, send me the invite code.\n"
    "Ask whoever runs this watcher for it."
)

STAR = (
    "\n\n⭐ if this saved you an inbox refresh, a star on the repo is "
    'appreciated: <a href="{}">github</a>'.format(REPO)
)


def _post(method, payload):
    resp = notify._session.post(
        notify.API.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
        json=payload, timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise notify.TelegramError("{}: {}".format(method, data.get("description")))
    return data


def _commands(pairs):
    return [{"command": c, "description": d} for c, d in pairs]


def apply_all():
    """Push name, descriptions and command menus. Returns what was set."""
    done = []

    _post("setMyName", {"name": NAME})
    done.append("name")

    _post("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})
    done.append("short description")

    _post("setMyDescription", {"description": DESCRIPTION})
    done.append("description")

    _post("setMyCommands", {"commands": _commands(COMMANDS)})
    done.append("commands ({})".format(len(COMMANDS)))

    if config.TELEGRAM_CHAT_ID:
        _post("setMyCommands", {
            "commands": _commands(OWNER_COMMANDS),
            "scope": {"type": "chat", "chat_id": int(config.TELEGRAM_CHAT_ID)},
        })
        done.append("owner commands ({})".format(len(OWNER_COMMANDS)))

    return done
