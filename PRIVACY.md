# Privacy Policy

**cdc-watch** (`@vitap_cdc_watch_bot`)

Last updated: 11 September 2026

This is a personal, self-hosted project. It runs on a Raspberry Pi at home,
not on a cloud service, and it is not operated by or affiliated with VIT-AP.

## What the bot reads

The bot reads placement mail from **one mailbox — the operator's** — using
Google's official Gmail API, restricted to mail from the CDC sender address.

**You do not grant the bot access to your email.** Registering does not
connect your Google account, and the bot cannot read your inbox.

## What is stored about you

When you register, the following is saved to a local SQLite file on the
operator's Raspberry Pi:

- your Telegram chat ID (a number Telegram assigns; the bot cannot see your
  phone number)
- whichever identifiers you choose to set: Neo ID, registration number, name,
  and any drive-specific IDs you add
- for each CDC mail evaluated for you: the subject line, the company and round
  if detected, the verdict, and how many IDs the list contained

## What is not stored

- email bodies and attachments — these are read in memory and discarded
- anything about students other than you
- your Telegram phone number, username or contacts

## Who can see it

Only the operator, who has access to the machine. Data is never sold, shared
with third parties, or used for anything besides sending you your own
shortlist alerts. Other registered users cannot see your identifiers or your
results.

## Third parties

- **Telegram** — delivers messages; their privacy policy applies to the chat.
- **Google (Gmail API)** — the mailbox being read belongs to the operator.
- **NVIDIA NIM** — *optional*. If enabled, subject lines only are sent to
  identify the company name. No student identifiers, mail bodies or
  attachments are ever sent. It is off by default.

## Deleting your data

Send **`/stop`** to the bot. Your registration, your identifiers and your
entire alert history are deleted immediately and permanently. No copy is kept.

The operator can also remove any user with `/kick`.

## Retention

Data is kept only while you are registered. There is no backup or archive
beyond the single local database file.

## Accuracy, and what this bot is not

This is a convenience tool, not an official source. It can miss a list,
misread one, or fail while the Pi is offline. An alert saying you are not on a
list is **not** a rejection notice — lists are sometimes partial, and some
drives use identifiers the bot doesn't know about.

**Always confirm anything that matters in the official portal.** Do not rely
on this bot alone for a placement decision.

## Changes

Any change will be reflected in this file, with the date above updated.

## Contact

Questions or deletion requests: message the bot operator on Telegram, or open
an issue on the repository.
