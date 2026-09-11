# cdc-watch

A personal Telegram bot that watches VIT-AP CDC placement mail and tells me
the moment my Neo ID lands on a shortlist.

I built this for myself. It runs on a Raspberry Pi 4 at home, against my own
university mailbox.

## What it does

The CDC sends a lot of mail, and the shortlists that matter are buried in it —
sometimes in the body, sometimes in an attached sheet, sometimes only in a
reply to an earlier thread. This reads all of it and pushes a verdict to
Telegram:

- 🎉 **on the list** — with the source sheet attached
- ⚪️ **not on this one** — quietly, and never phrased as a rejection
- ⚠️ **the list could not be read** — so I know to check by hand
- announcements and job descriptions stay silent

Lists are found by ID pattern rather than by column header, so layout changes
don't break it. Drives that use their own identifiers work too — TCS reference
numbers and Cognizant Superset IDs, alongside Neo IDs.

It also keeps a per-company timeline, so `/status` shows how far each drive
got: applied → online test → interview → final list. `/search foodhub` checks
past mail for a drive on demand, and a weekly heartbeat means a dead Pi is
noticeable rather than silent.

## Running it yourself

See [SETUP.md](SETUP.md). It needs a Google Cloud project of your own and a
Telegram bot; everything else is a `pip install`.

The watcher can be shared — friends register with an invite code and set their
own Neo ID, and each gets their own verdict from the same mailbox.

⭐ If it's useful to you, a star is appreciated.
