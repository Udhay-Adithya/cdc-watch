"""End-to-end test of process() against a fake Gmail service.

Exercises the real fetch -> extract -> match -> notify path using the actual
CDC spreadsheet, with only the network boundaries faked.
"""
import base64
import io
import os
import random
import string
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import Workbook

from cdcwatch import classify, config, identity, main, notify
from cdcwatch.matcher import ABSENT, FOUND, NO_LIST, UNPARSED
from cdcwatch.store import Store

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "fixtures", "sample_shortlist.xlsx")
ME = {"neo_id": "B5R7O9J8", "reg_no": "23BCE7625", "name": "Udhay Adithya J"}
CHAT = "8805933078"


def b64(data):
    return base64.urlsafe_b64encode(data).decode()


def sheet(ids):
    wb = Workbook()
    ws = wb.active
    ws.append(["Neo ID "])
    for i in ids:
        ws.append([i])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def rand_id():
    return "".join(random.choice(string.ascii_uppercase) + random.choice(string.digits)
                   for _ in range(4))


class FakeGmail:
    """Mimics the chained googleapiclient surface that gmail.fetch() touches."""

    def __init__(self, subject, body="", attachments=()):
        self.msg = {
            "id": "msg1", "threadId": "t1",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": "students.cdc2027@vitap.ac.in"},
                    {"name": "Date", "value": "Thu, 11 Sep 2026 10:00:00 +0530"},
                ],
                "mimeType": "multipart/mixed",
                "parts": [{"mimeType": "text/plain", "body": {"data": b64(body.encode())}}]
                + [
                    {"filename": fn, "mimeType": "application/octet-stream",
                     "body": {"attachmentId": fn}}
                    for fn, _ in attachments
                ],
            },
        }
        self.blobs = {fn: data for fn, data in attachments}

    # chained no-op accessors
    def __init_thread__(self, sibling):
        self.sibling = sibling

    def users(self):
        return self
    def messages(self):
        return self
    def attachments(self):
        return self
    def threads(self):
        return _Threads(self)

    def get(self, userId=None, id=None, format=None, messageId=None):
        if messageId is not None:          # attachments().get()
            return _Exec({"data": b64(self.blobs[id])})
        return _Exec(self.msg)


class _Threads:
    """threads().get() returning this message plus an older one that carries
    the sheet -- the real "Re: ... please find the attached list" shape."""

    def __init__(self, fake):
        self.fake = fake

    def get(self, userId=None, id=None, format=None):
        parent = getattr(self.fake, "parent", None)
        messages = [self.fake.msg]
        if parent:
            messages.append(parent)
        return _Exec({"messages": messages})


class _Exec:
    def __init__(self, value):
        self.value = value
    def execute(self):
        return self.value


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.docs = []
        notify.send = lambda text, **kw: self.sent.append(text)
        notify.send_document = lambda fn, data, **kw: self.docs.append(fn)
        classify.classify = lambda subject, timeout=25: classify.heuristic(subject)
        config.ME = ME
        handle, self.db = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = Store(self.db)
        self.store.add_user(CHAT, is_owner=True)
        for field, value in (("neo_id", ME["neo_id"]), ("reg_no", ME["reg_no"]),
                             ("name", ME["name"])):
            identity.set_field(self.store, CHAT, field, value)

    def tearDown(self):
        os.unlink(self.db)

    def test_real_list_match(self):
        with open(SAMPLE, "rb") as fh:
            data = fh.read()
        svc = FakeGmail("Fwd: RE: Shortlist - Round 2 - Zoho (Batch 3)",
                        "Please find attached.", [("list.xlsx", data)])
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, FOUND)
        self.assertEqual(self.store.timeline()[0]["matched_on"], "neo_id")
        self.assertEqual(self.store.timeline()[0]["total_ids"], 498)
        self.assertIn("YOU ARE SHORTLISTED", self.sent[0])
        self.assertIn("Round 2", self.sent[0])
        self.assertEqual(self.docs, ["list.xlsx"])      # source list pushed too
        row = self.store.timeline()[0]
        self.assertEqual(row["round_label"], "Round 2")

    def test_absent_is_not_a_rejection(self):
        svc = FakeGmail("Shortlist Amazon OA", "", [("l.xlsx", sheet([rand_id() for _ in range(300)]))])
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, ABSENT)
        self.assertIn("Not in this list", self.sent[0])
        self.assertIn("Not a rejection", self.sent[0])
        self.assertEqual(self.docs, [])                  # no attachment spam on a miss

    def test_unparseable_alerts_loudly(self):
        """Claims a roster, carries only an unreadable file -> must be loud."""
        svc = FakeGmail("Elgi Online test shortlist",
                        "Please find the attached shortlisted candidates list.",
                        [("list.pdf", b"%PDF-1.4 junk")])
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, UNPARSED)
        self.assertIn("Needs a manual check", self.sent[0])
        self.assertIn("list.pdf", self.sent[0])

    def test_jd_attachment_is_silent(self):
        """A JD PDF on a registration mail is not a failed shortlist parse."""
        svc = FakeGmail("Amazon - Super Dream Internship - 2027 Batch",
                        "Please find the form filled from our end. Eligibility: B.Tech 2027.",
                        [("SDE_JD_2027.pdf", b"%PDF-1.4 junk")])
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, NO_LIST)
        self.assertEqual(self.sent, [])

    def test_future_tense_shortlist_is_silent(self):
        """"Shortlisted students will be having interviews" is not a list."""
        svc = FakeGmail("Sandisk Online Test is scheduled on 15th September",
                        "Shortlisted students will be having physical interviews "
                        "at VIT Vellore on 22-09-2026.")
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, NO_LIST)
        self.assertEqual(self.sent, [])

    def test_reference_id_match(self):
        """TCS publishes against its own reference numbers, not Neo IDs."""
        identity.add_extra_id(self.store, CHAT, "CT20264998331")
        svc = FakeGmail("Congratulations!! TCS PNQT Selection List 2027 Batch",
                        "Please find below TCS selections along with role offered.\n"
                        "REFERENCE ID APPROVED OFFER\n"
                        "DT20268185049 DIGITAL\nCT20264998331 DIGITAL\n"
                        "CT20264974289 DIGITAL")
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, FOUND)
        self.assertEqual(self.store.timeline()[0]["matched_on"], "reference id")
        self.assertIn("YOU ARE SHORTLISTED", self.sent[0])

    def test_idempotent(self):
        svc = FakeGmail("Shortlist Zoho", "B5R7O9J8 " + " ".join(rand_id() for _ in range(50)))
        main.process(svc, self.store, "msg1")
        main.process(svc, self.store, "msg1")
        self.assertEqual(len(self.sent), 1)              # redelivery must not re-notify

    def test_body_only_list(self):
        others = " ".join(rand_id() for _ in range(40))
        svc = FakeGmail("Qualified students - Round 1 - Infosys",
                        "Congratulations to:\nB5R7O9J8\n" + others)
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, FOUND)
        self.assertIn("Round 1", self.sent[0])


    def test_single_name_selection_list(self):
        """A real CDC list can name ONE student; that is still a list."""
        svc = FakeGmail("Congratulations !! Divum Regular Internship Selection List!!",
                        "Neo ID:\nK1V9R8U3\n\nNote: eligible for 2x CTC")
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, ABSENT)
        self.assertEqual(self.store.timeline()[0]["total_ids"], 1)
        self.assertIn("Not in this list", self.sent[0])

    def test_single_name_list_that_is_me(self):
        svc = FakeGmail("Congratulations !! Divum Selection List!!",
                        "Neo ID:\nB5R7O9J8")
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, FOUND)

    def test_announcement_is_silent(self):
        """"Shortlist will be shared by 11 AM" must not buzz the phone."""
        svc = FakeGmail("Sandisk Online Test is scheduled on 15th September",
                        "Shortlisted will shared by 15-09-26 11 AM. "
                        "All students must attend in respective campus venues.")
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, NO_LIST)
        self.assertEqual(self.sent, [])

    def test_claims_list_but_none_found_is_loud(self):
        svc = FakeGmail("Re: Cognizant Technical Assessment",
                        "Please find the shortlisted student list. "
                        "Students must attend in campus labs only.")
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, UNPARSED)
        self.assertIn("Needs a manual check", self.sent[0])

    def test_attachment_on_parent_message(self):
        """Reply says "please find the attached list"; sheet is on the parent."""
        svc = FakeGmail("Re: Elgi Online test is scheduled on 10-09-2026",
                        "Please find the attached shortlisted candidates list.")
        parent = FakeGmail("Elgi Online test", "", [("elgi.xlsx", sheet(
            [rand_id() for _ in range(40)] + ["B5R7O9J8"]))])
        parent.msg["id"] = "msg0"          # distinct id, or it reads as "self"
        svc.parent = parent.msg
        svc.blobs.update(parent.blobs)
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, FOUND)
        self.assertIn("YOU ARE SHORTLISTED", self.sent[0])


    def test_quoted_parent_claim_does_not_alert(self):
        """A reply quoting "find the attached list" is not itself a list mail."""
        svc = FakeGmail("Re: Report Immediately : Kinaxis Pre-placement talk",
                        "KIND ATTENTION!!\n"
                        "All candidates shortlisted are informed to report to PRP 717.\n"
                        ">> Find the below shortlisted candidates list for all roles\n"
                        ">> Please find the students lists.")
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, NO_LIST)
        self.assertEqual(self.sent, [])


    def test_superset_id_match(self):
        """Cognizant lists are keyed by Superset ID and name, not Neo ID."""
        identity.add_extra_id(self.store, CHAT, "8254250")
        rows = "\n".join("82545{:02d} Student {} B.Tech Eligible".format(i, i)
                          for i in range(30))
        svc = FakeGmail("Cognizant Technical Assessment is scheduled",
                        "Please find the students lists.\n"
                        "Superset ID Name Program Eligibility\n"
                        "8254250 Aravindhan B B.Tech Eligible\n" + rows)
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, FOUND)
        self.assertEqual(self.store.timeline()[0]["matched_on"], "reference id")

    def test_unknown_id_format_is_actionable(self):
        """A table we can read but not key on should say what to do."""
        rows = "\n".join("82545{:02d} Student {} B.Tech Eligible".format(i, i)
                          for i in range(30))
        svc = FakeGmail("Cognizant Technical Assessment is scheduled",
                        "Please find the students lists.\n"
                        "Superset ID Name Program Eligibility\n" + rows)
        status = main.process(svc, self.store, "msg1")
        self.assertEqual(status, UNPARSED)
        self.assertIn("/addid", self.sent[0])


    def test_two_users_get_their_own_verdicts(self):
        """One mail, one fetch, a different answer per registered user."""
        friend = "555000111"
        self.store.add_user(friend)
        identity.set_field(self.store, friend, "neo_id", "K1V9R8U3")

        sent = []
        notify.send = lambda text, **kw: sent.append((kw.get("chat_id"), text))

        others = [rand_id() for _ in range(80)]
        svc = FakeGmail("Congratulations!! Zoho selection list 2027",
                        "", [("l.xlsx", sheet(others + ["B5R7O9J8"]))])
        main.process(svc, self.store, "msg1")

        by_chat = {chat: text for chat, text in sent}
        self.assertIn("YOU ARE SHORTLISTED", by_chat[CHAT])
        self.assertIn("Not in this list", by_chat[friend])

    def test_user_without_an_id_is_skipped(self):
        """Registered but not configured -> not matched, not notified."""
        self.store.add_user("999000111")
        sent = []
        notify.send = lambda text, **kw: sent.append(kw.get("chat_id"))
        svc = FakeGmail("Zoho selection list",
                        "", [("l.xlsx", sheet([rand_id() for _ in range(40)]))])
        main.process(svc, self.store, "msg1")
        self.assertEqual(sent, [CHAT])

    def test_failed_send_does_not_renotify_everyone(self):
        """A send failure retries only the user who missed out."""
        friend = "555000222"
        self.store.add_user(friend)
        identity.set_field(self.store, friend, "neo_id", "K1V9R8U3")

        delivered = []
        friend_should_fail = [True]

        def flaky(text, **kw):
            if kw.get("chat_id") == friend and friend_should_fail[0]:
                friend_should_fail[0] = False
                raise RuntimeError("network blip")
            delivered.append(kw.get("chat_id"))

        notify.send = flaky
        svc = FakeGmail("Zoho selection list",
                        "", [("l.xlsx", sheet([rand_id() for _ in range(40)]))])
        main.process(svc, self.store, "msg1")
        self.assertEqual(delivered, [CHAT])
        self.assertFalse(self.store.seen("msg1"))   # unfinished -> will retry

        main.process(svc, self.store, "msg1")
        self.assertEqual(delivered, [CHAT, friend])  # owner not told twice
        self.assertTrue(self.store.seen("msg1"))


    def test_stop_erases_everything_for_that_user(self):
        friend = "555000333"
        self.store.add_user(friend)
        identity.set_field(self.store, friend, "neo_id", "K1V9R8U3")
        svc = FakeGmail("Zoho selection list",
                        "", [("l.xlsx", sheet([rand_id() for _ in range(40)]))])
        main.process(svc, self.store, "msg1")
        self.assertTrue(self.store.timeline(chat_id=friend))

        self.store.remove_user(friend)
        self.assertIsNone(self.store.get_user(friend))
        self.assertEqual(self.store.timeline(chat_id=friend), [])
        self.assertFalse(self.store.was_delivered("msg1", friend))
        self.assertTrue(self.store.timeline(chat_id=CHAT))   # owner untouched


    def test_heartbeat_reaches_only_configured_users(self):
        ready = "555000444"
        self.store.add_user(ready)
        identity.set_field(self.store, ready, "neo_id", "K1V9R8U3")
        self.store.add_user("555000555")          # registered, no id set

        sent = []
        notify.send = lambda text, **kw: sent.append(kw.get("chat_id"))
        self.assertEqual(main.heartbeat(self.store), 2)
        self.assertEqual(sorted(sent), sorted([CHAT, ready]))

    def test_heartbeat_counts_this_users_events(self):
        for status, n in (("ABSENT", 5), ("FOUND", 2), ("NO_LIST", 9)):
            for i in range(n):
                self.store.add_event(msg_id="m{}{}".format(status, i),
                                     chat_id=CHAT, status=status, subject="x")
        sent = []
        notify.send = lambda text, **kw: sent.append(text)
        main.heartbeat(self.store)
        self.assertIn("16 CDC mail checked", sent[0])
        self.assertIn("2 you were on", sent[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
