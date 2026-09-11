"""Telegram delivery."""
import html
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config
from .matcher import ABSENT, FOUND, NO_LIST, UNPARSED

API = "https://api.telegram.org/bot{token}/{method}"

# api.telegram.org is intermittently slow or blocked on some Indian ISPs, so
# ride out a blip rather than dropping the alert. A send that still fails
# raises, and the caller then leaves the mail unmarked so it retries later.
_RETRY = Retry(total=4, backoff_factor=2, connect=4, read=2,
               status_forcelist=(429, 500, 502, 503, 504),
               allowed_methods=frozenset(["GET", "POST"]))
_session = requests.Session()
_session.mount("https://", HTTPAdapter(max_retries=_RETRY))
MAIL_URL = "https://mail.google.com/mail/u/0/#inbox/{msg_id}"


def is_configured():
    """Both halves present and the chat id actually numeric."""
    return bool(
        config.TELEGRAM_BOT_TOKEN
        and config.TELEGRAM_CHAT_ID
        and re.fullmatch(r"-?\d+", str(config.TELEGRAM_CHAT_ID))
    )


def _esc(text):
    return html.escape(str(text or ""))


class TelegramError(RuntimeError):
    pass


def _call(method, **kwargs):
    if not config.TELEGRAM_BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    target = kwargs.get("chat_id") or config.TELEGRAM_CHAT_ID
    if not target:
        raise TelegramError("TELEGRAM_CHAT_ID is not set")
    if not re.fullmatch(r"-?\d+", str(target)):
        raise TelegramError(
            "TELEGRAM_CHAT_ID must be a number (yours, not the bot's username) "
            "-- got {!r}. Run: python -m cdcwatch.main telegram".format(target)
        )
    files = kwargs.pop("files", None)
    chat_id = kwargs.pop("chat_id", None) or config.TELEGRAM_CHAT_ID
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    payload = dict(chat_id=chat_id, **kwargs)
    if files:
        resp = _session.post(url, data=payload, files=files, timeout=60)
    else:
        resp = _session.post(url, json=payload, timeout=30)
    # Telegram puts the real reason in the JSON body; raise_for_status alone
    # would throw it away and leave a bare "400 Bad Request".
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not data.get("ok"):
        raise TelegramError("{} failed: {}".format(
            method, data.get("description") or "HTTP {}".format(resp.status_code)))
    return data


def send(text, buttons=None, silent=False, chat_id=None, force_reply=None):
    payload = dict(text=text, parse_mode="HTML", disable_notification=silent,
                   chat_id=chat_id)
    payload["link_preview_options"] = {"is_disabled": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    elif force_reply:
        # Opens the keyboard with a focused input box, so the user types just
        # the value instead of retyping the command with an argument.
        payload["reply_markup"] = {
            "force_reply": True,
            "input_field_placeholder": str(force_reply)[:64],
        }
    return _call("sendMessage", **payload)


def send_document(filename, data, caption="", chat_id=None):
    return _call(
        "sendDocument",
        chat_id=chat_id,
        caption=caption[:1024],
        parse_mode="HTML",
        files={"document": (filename, data)},
    )


def send_reauth(auth_url):  # owner only -- only they can re-consent
    """Re-consent cannot happen without a human at Google's screen -- so make
    it one tap from wherever the user happens to be."""
    return send(
        "🔑 <b>Gmail authorisation expired</b>\n\n"
        "CDC mail is no longer being watched until you reconnect.",
        buttons=[[{"text": "Reconnect Gmail", "url": auth_url}]],
    )


def format_verdict(verdict, subject, company, round_label, msg_id):
    who = " · ".join(x for x in (company, round_label) if x) or "CDC mail"
    link = MAIL_URL.format(msg_id=msg_id)
    lines = []

    if verdict.status == FOUND:
        label = {"neo_id": "Neo ID", "reg_no": "registration number", "name": "name"}
        lines.append("🎉 <b>YOU ARE SHORTLISTED</b> — {}".format(_esc(who)))
        lines.append("Matched on your {}.".format(_esc(label.get(verdict.matched_on, "details"))))
        if verdict.matched_on == "name":
            lines.append("<i>Name-only match — confirm this one yourself.</i>")
        if verdict.total_ids:
            lines.append("List size: {}".format(verdict.total_ids))
    elif verdict.status == ABSENT:
        lines.append("⚪️ <b>Not in this list</b> — {}".format(_esc(who)))
        lines.append("Parsed {} IDs; yours was not among them.".format(verdict.total_ids))
        lines.append("<i>Not a rejection — the list may be partial. Check the portal.</i>")
    elif verdict.status == UNPARSED:
        lines.append("⚠️ <b>Needs a manual check</b> — {}".format(_esc(who)))
        lines.append("This mail says it carries a list, but none could be read.")
    else:
        lines.append("· <b>No list in this mail</b> — {}".format(_esc(who)))
        lines.append("Announcement or reminder only.")

    for warning in verdict.warnings:
        lines.append("⚠️ {}".format(_esc(warning)))

    lines.append("")
    lines.append("<b>Subject:</b> {}".format(_esc(subject)))
    lines.append('<a href="{}">Open in Gmail</a>'.format(link))
    return "\n".join(lines)
