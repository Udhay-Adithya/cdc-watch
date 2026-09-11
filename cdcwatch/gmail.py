"""Gmail auth + message fetching.

Auth notes worth remembering:
  * Publish the OAuth consent screen to "In production". Left in "Testing",
    Google expires refresh tokens after 7 days and the watcher goes quiet.
  * Re-consent always needs a human at Google's screen; we cannot refresh a
    dead refresh token. What we can do is notice fast and hand over a link.
"""
import base64
from dataclasses import dataclass, field

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import config


class NeedsReauth(Exception):
    """Raised when only a human can fix it. Carries the link to hand over."""

    def __init__(self, auth_url):
        super().__init__("Gmail authorisation expired")
        self.auth_url = auth_url


@dataclass
class Mail:
    msg_id: str
    thread_id: str = ""
    subject: str = ""
    sender: str = ""
    received_at: str = ""
    body_text: str = ""
    body_html: str = ""
    attachments: list = field(default_factory=list)  # (filename, bytes)


class MissingCredentials(Exception):
    pass


def _flow():
    if not config.CREDENTIALS_PATH.exists():
        raise MissingCredentials(
            "credentials.json not found at {}. Download the OAuth client from "
            "Google Cloud Console and save it there.".format(config.CREDENTIALS_PATH)
        )
    return InstalledAppFlow.from_client_secrets_file(
        str(config.CREDENTIALS_PATH), config.SCOPES
    )


def login_interactive():
    """First-time consent. Run on a machine with a browser, or over an SSH
    tunnel (`ssh -L 8765:localhost:8765 pi@...`) and open the link locally."""
    creds = _flow().run_local_server(port=8765, prompt="consent", access_type="offline")
    config.TOKEN_PATH.write_text(creds.to_json())
    return creds


def auth_url():
    flow = _flow()
    flow.redirect_uri = "http://localhost:8765/"
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return url


def credentials():
    if not config.TOKEN_PATH.exists():
        raise NeedsReauth(auth_url())
    creds = Credentials.from_authorized_user_file(str(config.TOKEN_PATH), config.SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Revoked, or the 7-day Testing-mode expiry bit us.
            raise NeedsReauth(auth_url())
        config.TOKEN_PATH.write_text(creds.to_json())
        return creds
    raise NeedsReauth(auth_url())


def service():
    return build("gmail", "v1", credentials=credentials(), cache_discovery=False)


# --- label + watch --------------------------------------------------------
def ensure_label(svc, name=None):
    """Return the id of the label CDC mail is filtered into, creating it if
    needed. Push notifications can only be scoped by label, not by sender."""
    name = name or config.GMAIL_LABEL
    existing = svc.users().labels().list(userId="me").execute().get("labels", [])
    for label in existing:
        if label["name"].lower() == name.lower():
            return label["id"]
    created = svc.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow",
              "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def start_watch(svc, label_id):
    """(Re)arm push. Expires after 7 days -- renew daily from cron, because
    an expired watch fails silently."""
    topic = "projects/{}/topics/{}".format(config.GCP_PROJECT, config.PUBSUB_TOPIC)
    return svc.users().watch(
        userId="me",
        body={"topicName": topic, "labelIds": [label_id],
              "labelFilterBehavior": "include"},
    ).execute()


# --- reading --------------------------------------------------------------
def history_since(svc, history_id, label_id):
    """Message ids added since `history_id`. None means the cursor aged out
    and the caller should fall back to a query-based resync."""
    ids, page = [], None
    while True:
        try:
            resp = svc.users().history().list(
                userId="me", startHistoryId=history_id, labelId=label_id,
                historyTypes=["messageAdded"], pageToken=page,
            ).execute()
        except Exception as exc:
            if "404" in str(exc) or "startHistoryId" in str(exc):
                return None, None
            raise
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                ids.append(added["message"]["id"])
        page = resp.get("nextPageToken")
        if not page:
            return ids, resp.get("historyId", history_id)


def search(svc, query, max_results=1000):
    """All message ids matching `query`, following pagination.

    messages.list caps a page at 500 and the CDC sends ~500 mails a month, so
    a single unpaginated call silently drops everything older than the first
    page -- the exact failure this watcher is supposed to prevent.
    """
    ids, page = [], None
    while len(ids) < max_results:
        resp = svc.users().messages().list(
            userId="me", q=query, pageToken=page,
            maxResults=min(500, max_results - len(ids)),
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids[:max_results]


def _walk(svc, msg_id, part, mail):
    mime = part.get("mimeType", "")
    body = part.get("body", {})
    filename = part.get("filename")

    if filename:
        data = body.get("data")
        if not data and body.get("attachmentId"):
            att = svc.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=body["attachmentId"]
            ).execute()
            data = att.get("data")
        if data:
            mail.attachments.append((filename, _b64(data)))
    elif mime == "text/plain" and body.get("data"):
        mail.body_text += _b64(body["data"]).decode("utf-8", "replace")
    elif mime == "text/html" and body.get("data"):
        mail.body_html += _b64(body["data"]).decode("utf-8", "replace")

    for child in part.get("parts", []) or []:
        _walk(svc, msg_id, child, mail)


def _b64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def fetch(svc, msg_id):
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    mail = Mail(
        msg_id=msg_id,
        thread_id=msg.get("threadId", ""),
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        received_at=headers.get("date", ""),
    )
    _walk(svc, msg_id, msg["payload"], mail)
    return mail


def thread_attachments(svc, thread_id, exclude_msg_id=None):
    """Attachments from the other messages in a thread.

    CDC often replies to their own announcement ("Please find the attached
    shortlist") where the sheet is on the parent message, not the reply.
    Without this, those mails look like parse failures.
    """
    thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    found = []
    for msg in thread.get("messages", []):
        if msg["id"] == exclude_msg_id:
            continue
        sibling = Mail(msg_id=msg["id"])
        _walk(svc, msg["id"], msg["payload"], sibling)
        found.extend(sibling.attachments)
    return found
