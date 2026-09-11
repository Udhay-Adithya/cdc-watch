"""Telegram command handling, including the conversational prompt flow."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdcwatch import config, identity, telegram_bot
from cdcwatch.store import Store

OWNER = "8805933078"
FRIEND = "555000777"
CODE = "test-code"


def text_of(reply):
    return reply.text if isinstance(reply, telegram_bot.Reply) else reply


def ask_of(reply):
    return reply.ask if isinstance(reply, telegram_bot.Reply) else None


class BotTest(unittest.TestCase):
    def setUp(self):
        config.INVITE_CODE = CODE
        config.TELEGRAM_CHAT_ID = OWNER
        handle, self.db = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = Store(self.db)
        self.store.add_user(OWNER, is_owner=True)
        self.bot = telegram_bot.Bot(self.store, on_check=lambda: 0,
                                    on_search=lambda q, c: "searched " + q)

    def tearDown(self):
        os.unlink(self.db)

    def say(self, text, chat_id=OWNER):
        return self.bot.handle(text, chat_id)

    # --- conversational input --------------------------------------------
    def test_bare_command_asks_for_the_value(self):
        reply = self.say("/setneo")
        self.assertIn("Neo ID", text_of(reply))
        self.assertEqual(ask_of(reply), "B5R7O9J8")

        reply = self.say("k1v9r8u3")
        self.assertIn("set to", text_of(reply))
        self.assertEqual(
            identity.current(self.store, OWNER)["neo_id"], "K1V9R8U3")

    def test_command_with_argument_still_works(self):
        self.say("/setneo B5R7O9J8")
        self.assertEqual(
            identity.current(self.store, OWNER)["neo_id"], "B5R7O9J8")

    def test_invalid_answer_asks_again(self):
        self.say("/setneo")
        reply = self.say("not an id")
        self.assertIn("❌", text_of(reply))
        self.assertEqual(ask_of(reply), "B5R7O9J8")   # still waiting
        self.say("b5r7o9j8")
        self.assertEqual(
            identity.current(self.store, OWNER)["neo_id"], "B5R7O9J8")

    def test_a_command_abandons_a_pending_prompt(self):
        # A friend, not the owner: the owner falls back to .env, which would
        # make "unchanged" impossible to assert.
        self.store.add_user(FRIEND)
        self.say("/setneo", FRIEND)
        self.say("/whoami", FRIEND)
        # The next plain message must not be swallowed as the Neo ID.
        reply = self.say("k1v9r8u3", FRIEND)
        self.assertIn("Unknown command", text_of(reply))
        self.assertEqual(identity.current(self.store, FRIEND)["neo_id"], "")

    def test_cancel(self):
        self.say("/setneo")
        self.assertIn("Cancelled", text_of(self.say("/cancel")))
        self.assertIn("Nothing pending", text_of(self.say("/cancel")))

    def test_pending_expires(self):
        self.say("/setneo")
        # Backdate the prompt past its TTL.
        self.store.set(telegram_bot.PENDING_PREFIX + OWNER, "neo_id|0")
        reply = self.say("k1v9r8u3")
        self.assertIn("Unknown command", text_of(reply))

    # --- registration -----------------------------------------------------
    def test_registration_is_fully_conversational(self):
        reply = self.say("/start", FRIEND)
        self.assertEqual(ask_of(reply), "invite code")

        reply = self.say(CODE, FRIEND)
        self.assertIn("Registered", text_of(reply))
        self.assertEqual(ask_of(reply), "B5R7O9J8")   # asks for the id at once

        self.say("q6p6o2d3", FRIEND)
        self.assertEqual(
            identity.current(self.store, FRIEND)["neo_id"], "Q6P6O2D3")

    def test_wrong_invite_code_is_refused(self):
        self.say("/start", FRIEND)
        self.assertIn("Wrong invite code", text_of(self.say("nope", FRIEND)))
        self.assertIsNone(self.store.get_user(FRIEND))

    def test_unregistered_cannot_use_commands(self):
        self.assertIn("/start", text_of(self.say("/whoami", FRIEND)))

    # --- authorisation ----------------------------------------------------
    def test_owner_only_commands_are_hidden(self):
        self.store.add_user(FRIEND)
        self.assertIn("Unknown command", text_of(self.say("/users", FRIEND)))
        self.assertIn("Unknown command", text_of(self.say("/kick", FRIEND)))
        self.assertIn("Registered", text_of(self.say("/users", OWNER)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
