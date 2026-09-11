# Setup

Everything runs locally against your own Gmail account and your own Telegram
bot. Nothing is hosted by anyone else.

## 1. Install

```bash
git clone <your fork> cdc-watch
cd cdc-watch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Python 3.9 or newer.

## 2. Google Cloud

Do this first — it decides whether the rest is possible at all.

1. Create a project at <https://console.cloud.google.com>, **under a personal
   Google account** rather than a university one. University Workspace
   policies otherwise apply to the project itself.
2. Enable the **Gmail API**.
3. OAuth consent screen → **External** → add your university address as a
   test user.
4. **Publish the app** (publishing status → *In production*). Left in
   *Testing*, Google expires refresh tokens after 7 days and the watcher goes
   quiet with no error. Unverified is fine for one user — click through the
   "Google hasn't verified this app" screen.
5. Credentials → **OAuth client ID → Desktop app** → download the JSON and
   save it as `credentials.json` in the project root.

Then authorise and check:

```bash
.venv/bin/python -m cdcwatch.main login
.venv/bin/python -m cdcwatch.main doctor
```

`doctor` should print your address and find recent CDC mail.

> **If your university blocks third-party OAuth apps**, this step fails and no
> code change helps. Set a Gmail filter that auto-forwards CDC mail to a
> personal Gmail, and point `WATCH_SENDER` at the forwarding address instead.

## 3. Telegram

1. Message [@BotFather](https://t.me/botfather) → `/newbot`. Copy the token
   into `.env` as `TELEGRAM_BOT_TOKEN`.
2. **Open your new bot and send it a message.** A bot cannot start a
   conversation, so until you do this it has nowhere to send anything.
3. Find your chat id:

   ```bash
   .venv/bin/python -m cdcwatch.main telegram
   ```

   It prints the `TELEGRAM_CHAT_ID=` line to paste into `.env`. This must be a
   **number** — yours, not the bot's username. Run it again for a test message.

4. Push the bot's name, description and command menu:

   ```bash
   .venv/bin/python -m cdcwatch.main botsetup
   ```

   The profile photo and the picture above the description have no Bot API
   method — set those in BotFather with `/setuserpic` and
   `/setdescription` → *edit description picture*.

> Don't run `run` while doing Telegram setup. Its command thread long-polls
> `getUpdates`, which consumes updates — including the message you send to
> discover your chat id.

## 4. Configure

In `.env`:

```
NEO_ID=B5R7O9J8
REG_NO=23BCE7625
FULL_NAME=Your Name
WATCH_SENDER=students.cdc2027@vitap.ac.in
INVITE_CODE=              # blank keeps the bot private to you
```

`NIM_API_KEY` is optional. It only fills in the company name on the subject
line; the round is detected by regex either way, and it never gates an alert.

## 5. Check it against your real mail

```bash
.venv/bin/python -m cdcwatch.main backfill --days 60 --dry-run
```

This replays real CDC mail through the whole pipeline and prints what *would*
have been sent, touching neither Telegram nor the database. Find a drive you
know you cleared and confirm it shows as `FOUND`. That is the only honest test
that matching works for you.

Then silence the backlog and start:

```bash
.venv/bin/python -m cdcwatch.main catchup
.venv/bin/python -m cdcwatch.main run
```

## Commands

```
doctor      verify config and auth
login       first-time google consent
telegram    verify bot token, find chat id
botsetup    push bot name, description, command menu
backfill    replay old mail (--days N --dry-run)
catchup     mark existing mail as seen, no alerts
run         the watcher
renew       daily: re-arm watch, sweep for misses
heartbeat   weekly: tell users the watcher is alive
timeline    per-company round history
```

## Running modes

`MODE=poll` (default) needs no setup beyond the Gmail API and checks every
`POLL_INTERVAL` seconds. Start here.

`MODE=push` uses a Pub/Sub **pull** subscription for near-instant delivery.
Pull matters: the Pi opens an outbound connection, so there is no public
endpoint, no certificate and no port forwarding.

```bash
gcloud pubsub topics create cdc-watch
gcloud pubsub topics add-iam-policy-binding cdc-watch \
  --member=serviceAccount:gmail-api-push@system.gserviceaccount.com \
  --role=roles/pubsub.publisher
gcloud pubsub subscriptions create cdc-watch-sub --topic=cdc-watch
```

Add a Gmail filter (`from:<cdc address>` → label `CDC`) so push only fires for
CDC mail, then set `GCP_PROJECT` and `MODE=push`.

## On a Raspberry Pi

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl enable --now cdc-watch.service cdc-watch-renew.timer \
  cdc-watch-heartbeat.timer
```

Edit the unit files if your user or path differ from `pi` and
`/home/pi/cdc-watch`. `token.json` copies across, so you don't re-authorise on
the Pi.

The weekly heartbeat sends every user a "still watching" message. Its value
is the absence of one: a silent bot otherwise looks exactly like a quiet week,
so a dead Pi goes unnoticed until someone misses a shortlist.

The daily timer is not optional in push mode: `users.watch()` expires after 7
days and **stops delivering with no error**. The same job sweeps the last 3
days by query, covering anything missed while the Pi was offline.

## Sharing with friends

Set `INVITE_CODE` in `.env`. A friend sends `/start <code>` once, then
`/setneo <their id>`. Until they set an ID they are registered but not matched
and receive nothing.

Each chat carries its own identity — a mail is fetched and flattened once,
then evaluated separately per user, so nobody sees anyone else's verdict.
Delivery is tracked per user per mail, so a send that fails to one person
retries for that person alone.

The gate matters: the bot token is a bearer credential, and without a code
anyone who found the bot could register and use your Pi and your mailbox.

## Tests

```bash
.venv/bin/python tests/test_pipeline.py
```

Runs the full fetch → extract → match → notify path against a fake Gmail
service. No network or credentials needed.
