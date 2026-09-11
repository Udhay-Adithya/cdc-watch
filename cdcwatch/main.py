"""Entry point: doctor / login / backfill / run / timeline."""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import botmeta, classify, config, gmail, identity, notify, telegram_bot
from .extract import extract
from .matcher import ABSENT, FOUND, NO_LIST, claims_list, evaluate, strip_quotes
from .store import Store

HISTORY_KEY = "history_id"
LABEL_KEY = "label_id"


def log(*parts):
    print(time.strftime("[%H:%M:%S]"), *parts, flush=True)


def load_mail(svc, msg_id):
    """Fetch a mail and flatten it to text, resolving the thread fallback.

    Shared by the watcher and by /search so both see a mail identically.
    Returns (mail, parsed, claim_text).
    """
    mail = gmail.fetch(svc, msg_id)
    parsed = extract(mail.body_text, mail.body_html, mail.attachments)
    # Claims are judged on the plain body only: the HTML alternative renders
    # quoted parent text without the ">" markers that identify it as quoted.
    claim_text = strip_quotes(mail.body_text) if mail.body_text else parsed.text

    # CDC frequently replies to their own announcement -- "please find the
    # attached shortlist" -- with the sheet on the parent message. Resolved
    # once for the mail, not once per user.
    if (mail.thread_id and claims_list(claim_text, mail.subject)
            and not _any_ids(parsed.text)):
        try:
            extra = gmail.thread_attachments(svc, mail.thread_id, msg_id)
        except Exception as exc:
            extra = []
            log("thread lookup failed:", exc)
        if extra:
            retry = extract(mail.body_text, mail.body_html,
                            list(mail.attachments) + extra)
            if _any_ids(retry.text):
                retry.sources.append("thread attachment")
                parsed = retry
    return mail, parsed, claim_text


def process(svc, store, msg_id, dry_run=False, verbose=False):
    """Fetch and flatten once, then evaluate and notify per registered user."""
    if store.seen(msg_id) and not dry_run:
        return None

    mail, parsed, claim_text = load_mail(svc, msg_id)
    meta = classify.classify(mail.subject)

    recipients = [u for u in store.list_users()
                  if identity.is_usable(identity.current(store, u["chat_id"]))]
    if not recipients:
        log("no registered users with an id set -- nothing to match against")

    statuses, all_sent = [], True
    for user in recipients:
        chat_id = user["chat_id"]
        me = identity.current(store, chat_id)
        verdict = evaluate(parsed.text, me, parsed.warnings, mail.subject, claim_text)
        statuses.append(verdict.status)
        quiet = verdict.status == NO_LIST
        text = notify.format_verdict(
            verdict, mail.subject, meta.get("company"), meta.get("round"), msg_id)

        if dry_run:
            if not quiet or verbose:
                print(text)
                print("-" * 60)
            continue
        if store.was_delivered(msg_id, chat_id):
            continue
        if quiet:
            # Evaluated, nothing worth sending. Still marked so it is counted
            # once and never re-evaluated for this user.
            store.mark_delivered(msg_id, chat_id)
        else:
            try:
                notify.send(text, silent=(verdict.status != FOUND),
                            chat_id=chat_id)
                if verdict.status == FOUND:
                    for filename, data in mail.attachments:
                        try:
                            notify.send_document(filename, data,
                                                 caption="Source list",
                                                 chat_id=chat_id)
                        except Exception as exc:
                            log("attachment upload failed:", exc)
                store.mark_delivered(msg_id, chat_id)
            except Exception as exc:
                all_sent = False
                log("send to {} failed: {}".format(chat_id, exc))
                continue

        store.add_event(
            msg_id=msg_id, chat_id=chat_id, thread_id=mail.thread_id,
            subject=mail.subject, company=meta.get("company"),
            round_label=meta.get("round"), status=verdict.status,
            matched_on=verdict.matched_on, total_ids=verdict.total_ids,
            received_at=mail.received_at,
        )

    summary = "/".join(sorted(set(statuses))) or "no-users"
    log("{} | {} | {}".format(summary, mail.subject[:60], parsed.sources))

    if dry_run:
        return statuses[0] if statuses else None
    # Only when everyone who needed it got it, so a failure retries.
    if all_sent:
        store.mark_seen(msg_id)
    return statuses[0] if statuses else None


def _any_ids(text):
    from .ids import find_neo_ids, find_ref_ids, find_reg_nos
    return bool(find_neo_ids(text) or find_reg_nos(text) or find_ref_ids(text))


# Anything that could steer the Gmail query somewhere other than CDC mail.
# A registered friend must not be able to send "from:bank" and read the
# owner's inbox, so the query is built from a scrubbed phrase, never from
# raw user text.
_QUERY_SAFE = re.compile(r"[^A-Za-z0-9 .&'-]+")

SEARCH_ICON = {"FOUND": "\U0001f389", "ABSENT": "\u26aa\ufe0f",
               "UNPARSED": "\u26a0\ufe0f", "NO_LIST": "\u00b7"}


def sanitize_query(text):
    """Reduce user input to a plain phrase safe to quote into a search."""
    return _QUERY_SAFE.sub(" ", text or "").strip()[:60]


def search_for(svc, store, query, chat_id, limit=8):
    """Evaluate past CDC mail matching `query` against one user's identity.

    Read-only: it never marks mail seen or records events, so running it
    cannot affect what the watcher will notify about later.
    """
    me = identity.current(store, chat_id)
    if not identity.is_usable(me):
        return "Set your Neo ID first with /setneo."

    phrase = sanitize_query(query)
    if len(phrase) < 3:
        return "Give me something to search for, e.g. <code>/search foodhub</code>"

    # Scoped to the CDC sender and to subject lines, with the phrase quoted so
    # nothing in it can be read as a search operator.
    gmail_query = 'from:{} subject:"{}"'.format(config.WATCH_SENDER, phrase)
    msg_ids = gmail.search(svc, gmail_query, max_results=limit)
    if not msg_ids:
        return "No CDC mail with <b>{}</b> in the subject.".format(
            notify._esc(phrase))

    lines = ["<b>{}</b>".format(notify._esc(phrase))]
    for msg_id in msg_ids:
        try:
            mail, parsed, claim_text = load_mail(svc, msg_id)
        except Exception as exc:
            log("search load failed:", exc)
            continue
        verdict = evaluate(parsed.text, me, parsed.warnings, mail.subject,
                           claim_text)
        detail = ""
        if verdict.status == FOUND:
            detail = " — you are on it"
        elif verdict.status == ABSENT:
            detail = " — {} ids, not you".format(verdict.total_ids)
        lines.append("{} {}{}".format(
            SEARCH_ICON.get(verdict.status, "\u00b7"),
            notify._esc(mail.subject[:70]), detail))
    return "\n".join(lines)


def reconcile(svc, store, window="newer_than:3d", dry_run=False, verbose=False):
    """Safety net: catch anything a missed notification or an offline Pi lost."""
    query = "from:{} {}".format(config.WATCH_SENDER, window)
    count = 0
    for msg_id in gmail.search(svc, query):
        if process(svc, store, msg_id, dry_run=dry_run, verbose=verbose) is not None:
            count += 1
    return count


def cmd_doctor(args):
    """The 10-minute check that validates the whole auth design."""
    ok = True
    print("config")
    for key in ("NEO_ID", "REG_NO", "FULL_NAME", "WATCH_SENDER"):
        value = getattr(config, key)
        print("  {:<14} {}".format(key, value or "MISSING"))
        ok &= bool(value)
    print("  {:<14} {}".format("credentials", "found" if config.CREDENTIALS_PATH.exists() else "MISSING"))
    print("  {:<14} {}".format("telegram", "set" if config.TELEGRAM_BOT_TOKEN else "not set"))
    print("  {:<14} {}".format("NIM key", "set" if config.NIM_API_KEY else "not set (regex only)"))

    print("\ngmail")
    try:
        svc = gmail.service()
        profile = svc.users().getProfile(userId="me").execute()
        print("  authorised as", profile["emailAddress"])
        hits = gmail.search(svc, "from:{}".format(config.WATCH_SENDER), max_results=5)
        print("  CDC mail reachable:", len(hits), "recent message(s)")
        if not hits:
            print("  (no mail from that sender yet -- check WATCH_SENDER)")
    except gmail.NeedsReauth as exc:
        ok = False
        print("  NOT AUTHORISED. Run: python -m cdcwatch.main login")
        print("  or open:", exc.auth_url)
    except Exception as exc:
        ok = False
        print("  FAILED:", exc)
    return 0 if ok else 1


def cmd_login(args):
    creds = gmail.login_interactive()
    print("token written to", config.TOKEN_PATH)
    print("refresh token present:", bool(creds.refresh_token))
    if not creds.refresh_token:
        print("WARNING: no refresh token -- revoke access and retry with prompt=consent")
    return 0


def cmd_backfill(args):
    svc = gmail.service()
    store = Store(config.DB_PATH)
    n = reconcile(svc, store, "newer_than:{}d".format(args.days),
                  dry_run=args.dry_run, verbose=args.verbose)
    log("processed", n, "message(s)")
    return 0


def cmd_timeline(args):
    rows = Store(config.DB_PATH).timeline(args.company)
    for row in rows:
        print("{:<24} {:<18} {:<9} {}".format(
            (row["company"] or "?")[:24], (row["round_label"] or "?")[:18],
            row["status"], (row["subject"] or "")[:50]))
    return 0


def baseline(svc, store, window="newer_than:30d"):
    """Record existing mail as seen WITHOUT notifying.

    A first start should not replay a month of shortlists onto your phone.
    """
    marked = 0
    for msg_id in gmail.search(svc, "from:{} {}".format(config.WATCH_SENDER, window)):
        if not store.seen(msg_id):
            store.mark_seen(msg_id)
            marked += 1
    return marked


def cmd_catchup(args):
    svc = gmail.service()
    store = Store(config.DB_PATH)
    n = baseline(svc, store, "newer_than:{}d".format(args.days))
    log("marked", n, "existing message(s) as seen -- no alerts sent")
    return 0


def heartbeat(store, days=7):
    """Tell every user the watcher is alive, and what it did this week.

    The point is the absence: if this stops arriving, the watcher is down.
    A silent bot is otherwise indistinguishable from a quiet week, which is
    the failure mode that actually costs someone a placement.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sent = 0
    for user in store.list_users():
        chat_id = user["chat_id"]
        if not identity.is_usable(identity.current(store, chat_id)):
            continue
        counts = store.status_counts_since(chat_id, since)
        checked = sum(counts.values())
        lines = ["\u2705 <b>Still watching.</b>"]
        if checked:
            bits = ["{} CDC mail checked".format(checked)]
            if counts.get(FOUND):
                bits.append("<b>{} you were on</b>".format(counts[FOUND]))
            if counts.get("UNPARSED"):
                bits.append("{} needing a manual look".format(counts["UNPARSED"]))
            lines.append("Past {} days: {}.".format(days, " · ".join(bits)))
        else:
            lines.append("Past {} days: no CDC mail at all.".format(days))
        lines.append("\n<i>If a week goes by with no message like this, "
                     "assume the watcher is down.</i>")
        try:
            notify.send("\n".join(lines), chat_id=chat_id, silent=True)
            sent += 1
        except Exception as exc:
            log("heartbeat to {} failed: {}".format(chat_id, exc))
    return sent


def cmd_heartbeat(args):
    store = Store(config.DB_PATH)
    identity.ensure_owner(store)
    n = heartbeat(store, days=args.days)
    log("heartbeat sent to", n, "user(s)")
    return 0


def cmd_botsetup(args):
    """Push the bot's name, descriptions and command menus to telegram."""
    if not config.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set in .env")
        return 2
    try:
        for item in botmeta.apply_all():
            print("set", item)
    except notify.TelegramError as exc:
        print("failed:", exc)
        return 1
    print("\nthe profile photo and the picture above the description have no")
    print("bot api method -- set those in @BotFather with /setuserpic and")
    print("/setdescription -> edit description picture.")
    return 0


def cmd_telegram(args):
    """Verify the bot token and find your chat id."""
    import requests

    if not config.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set in .env")
        print("Create a bot with @BotFather, then paste the token there.")
        return 2

    url = notify.API.format(token=config.TELEGRAM_BOT_TOKEN, method="getMe")
    info = requests.get(url, timeout=20).json()
    if not info.get("ok"):
        print("token rejected by Telegram:", info.get("description"))
        return 2
    bot = info["result"]
    print("bot ok: @{} ({})".format(bot.get("username"), bot.get("first_name")))

    if config.TELEGRAM_CHAT_ID:
        try:
            notify.send("✅ cdc-watch is connected.")
            print("sent a test message to chat", config.TELEGRAM_CHAT_ID)
            return 0
        except notify.TelegramError as exc:
            print("\ncould not send:", exc)
            print("looking up the correct chat id instead...")

    url = notify.API.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates")
    updates = requests.get(url, timeout=20).json().get("result", [])
    chats = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat.get("username") or chat.get("first_name") or "?"
    if not chats:
        print("\nNo messages yet. Open Telegram, send your bot any message,")
        print("then run this again.")
        return 1
    print("\nFound your chat id. Put this in .env (the number, not a name):\n")
    for chat_id, who in chats.items():
        print("  TELEGRAM_CHAT_ID={}   # {}".format(chat_id, who))
    return 0


def cmd_renew(args):
    """Daily cron job: re-arm the 7-day watch and sweep for anything missed.

    Both halves matter. An expired watch stops delivering with no error, and
    a push notification dropped while the Pi was rebooting is invisible.
    """
    try:
        svc = gmail.service()
    except gmail.NeedsReauth as exc:
        notify.send_reauth(exc.auth_url)
        log("re-auth link sent to Telegram")
        return 1

    if config.MODE == "push" and config.GCP_PROJECT:
        store = Store(config.DB_PATH)
        label_id = store.get(LABEL_KEY) or gmail.ensure_label(svc)
        store.set(LABEL_KEY, label_id)
        watch = gmail.start_watch(svc, label_id)
        store.set(HISTORY_KEY, watch["historyId"])
        log("watch renewed, expires", watch.get("expiration"))

    n = reconcile(svc, Store(config.DB_PATH), "newer_than:3d")
    log("reconcile processed", n, "missed message(s)")
    return 0


def cmd_run(args):
    store = Store(config.DB_PATH)
    identity.ensure_owner(store)
    try:
        svc = gmail.service()
    except gmail.NeedsReauth as exc:
        notify.send_reauth(exc.auth_url)
        log("re-auth link sent to Telegram")
        return 1

    # Only with a fully valid config. A half-configured bot would still
    # long-poll getUpdates, silently eating the very message you send it to
    # discover your chat id.
    if notify.is_configured():
        telegram_bot.Bot(
            store,
            on_check=lambda: reconcile(svc, store, "newer_than:7d"),
            on_search=lambda q, cid: search_for(svc, store, q, cid),
        ).start()
        log("telegram command bot listening")
    elif config.TELEGRAM_BOT_TOKEN:
        log("telegram not fully configured; run: python -m cdcwatch.main telegram")
    else:
        log("telegram not configured; alerts will only print here")

    if config.MODE == "push":
        return _run_push(svc, store)
    return _run_poll(svc, store)


def _run_poll(svc, store):
    log("poll mode, every", config.POLL_INTERVAL, "s, sender:", config.WATCH_SENDER)
    if not store.get("baselined"):
        # Fresh database: establish "everything before now is old news" so the
        # first start is quiet and only genuinely new mail alerts.
        n = baseline(svc, store)
        store.set("baselined", "1")
        log("first run -- marked", n, "existing message(s) as seen, no alerts")
    while True:
        try:
            reconcile(svc, store, "newer_than:2d")
        except gmail.NeedsReauth as exc:
            notify.send_reauth(exc.auth_url)
            log("re-auth needed; pausing")
            return 1
        except Exception as exc:
            log("poll error:", exc)
        time.sleep(config.POLL_INTERVAL)


def _run_push(svc, store):
    from google.cloud import pubsub_v1

    label_id = store.get(LABEL_KEY) or gmail.ensure_label(svc)
    store.set(LABEL_KEY, label_id)
    watch = gmail.start_watch(svc, label_id)
    store.set(HISTORY_KEY, watch["historyId"])
    log("watch armed until", watch.get("expiration"), "- renew daily")

    subscriber = pubsub_v1.SubscriberClient()
    path = subscriber.subscription_path(config.GCP_PROJECT, config.PUBSUB_SUBSCRIPTION)

    def callback(message):
        message.ack()  # the historyId cursor, not this message, is our source of truth
        try:
            payload = json.loads(message.data.decode())
            log("push: historyId", payload.get("historyId"))
            cursor = store.get(HISTORY_KEY)
            ids, new_cursor = gmail.history_since(svc, cursor, label_id)
            if ids is None:
                log("history cursor expired, resyncing by query")
                reconcile(svc, store)
                return
            for msg_id in ids:
                process(svc, store, msg_id)
            if new_cursor:
                store.set(HISTORY_KEY, new_cursor)
        except Exception as exc:
            log("push handler error:", exc)

    future = subscriber.subscribe(path, callback=callback)
    log("push mode, listening on", path)
    try:
        future.result()
    except KeyboardInterrupt:
        future.cancel()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cdcwatch")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("login").set_defaults(fn=cmd_login)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("renew").set_defaults(fn=cmd_renew)
    sub.add_parser("telegram").set_defaults(fn=cmd_telegram)
    sub.add_parser("botsetup", help="push bot name, description and commands"
                   ).set_defaults(fn=cmd_botsetup)
    h = sub.add_parser("heartbeat", help="tell users the watcher is alive")
    h.add_argument("--days", type=int, default=7)
    h.set_defaults(fn=cmd_heartbeat)
    c = sub.add_parser("catchup", help="mark existing mail as seen, no alerts")
    c.add_argument("--days", type=int, default=30)
    c.set_defaults(fn=cmd_catchup)
    b = sub.add_parser("backfill")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--verbose", action="store_true",
                   help="also show mails that carry no list at all")
    b.set_defaults(fn=cmd_backfill)
    t = sub.add_parser("timeline")
    t.add_argument("--company")
    t.set_defaults(fn=cmd_timeline)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except gmail.MissingCredentials as exc:
        print("setup incomplete:", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
