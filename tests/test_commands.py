"""The command listener: who it answers, what it skips, and that it survives failures."""

import asyncio
import logging

from bot.services.commands import CommandListener, Document
from bot.services.notifications import NotifierError

OWNER = "42"


def msg(update_id: int, text: str, chat: str = OWNER, *, is_bot: bool = False) -> dict:
    return {"update_id": update_id, "message": {"text": text, "chat": {"id": int(chat)}, "from": {"is_bot": is_bot}}}


class FakePoller:
    """Hands out scripted batches; once they run out it blocks like a long poll with no news."""

    def __init__(self, *batches):
        self.batches = list(batches)
        self.calls: list[tuple[int | None, int]] = []
        self.closed = False

    async def poll_updates(self, offset, timeout):
        self.calls.append((offset, timeout))
        if self.batches:
            batch = self.batches.pop(0)
            if isinstance(batch, Exception):
                raise batch
            return batch
        await asyncio.sleep(3600)  # a long poll with nothing new

    async def close(self):
        self.closed = True


class FakeSender:
    def __init__(self, fail=None):
        self.sent: list[tuple[str, bytes, str | None]] = []
        self.fail = fail
        self.closed = False

    async def send_document(self, filename, content, caption=None):
        if self.fail:
            raise self.fail
        self.sent.append((filename, content, caption))

    async def close(self):
        self.closed = True


async def drive(poller, *, handlers=None, until=None, mode="TESTNET", chat=OWNER, sender=None):
    """Run the listener until `until(replies)` is true (or 2 s), then stop it."""
    replies: list[str] = []
    listener = CommandListener(
        poller=poller, chat_id=chat, reply=replies.append, handlers=handlers or {}, mode=mode,
        backoff_step=0.01, sender=sender,
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(listener.run(stop))
    for _ in range(200):
        if until and until(replies):
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2)  # a prompt, clean stop even in the middle of a long poll
    return replies


async def status_ok(args):
    return "STATUS TEXT"


async def test_the_owner_gets_a_status_reply():
    poller = FakePoller([], [msg(1, "/status")])
    replies = await drive(poller, handlers={"status": status_ok}, until=lambda r: r)
    assert replies == ["STATUS TEXT"]


async def test_the_backlog_is_confirmed_but_never_acted_on():
    # A command sent while the bot was down must not run after the restart.
    poller = FakePoller([msg(7, "/status"), msg(8, "/status")], [msg(9, "/status")])
    replies = await drive(poller, handlers={"status": status_ok}, until=lambda r: r)
    assert replies == ["STATUS TEXT"]  # only the new one (id 9)
    assert poller.calls[0] == (-1, 0)  # the skip
    assert poller.calls[1][0] == 9  # then everything up to id 8 is confirmed


async def test_other_chats_get_no_answer_and_only_their_id_is_logged(caplog):
    caplog.set_level(logging.WARNING)
    poller = FakePoller([], [msg(1, "/status secret words", chat="999"), msg(2, "/status", chat="999"), msg(3, "/status")])
    replies = await drive(poller, handlers={"status": status_ok}, until=lambda r: r)
    assert replies == ["STATUS TEXT"]  # one reply, to the owner's message only
    assert caplog.text.count("ignored a command from chat 999") == 1  # once per chat, not per message
    assert "secret words" not in caplog.text


async def test_messages_that_are_not_commands_and_other_bots_are_ignored():
    poller = FakePoller([], [msg(1, "hello"), msg(2, "/status", is_bot=True), {"update_id": 3}, msg(4, "/status")])
    replies = await drive(poller, handlers={"status": status_ok}, until=lambda r: r)
    assert replies == ["STATUS TEXT"]


async def test_help_start_and_unknown_commands():
    poller = FakePoller([], [msg(1, "/help"), msg(2, "/start"), msg(3, "/pause"), msg(4, "/status@tidewatch_japc_bot")])
    replies = await drive(poller, handlers={"status": status_ok}, until=lambda r: len(r) == 4)
    assert "/status" in replies[0] and "read-only" in replies[0] and "[TESTNET]" in replies[0]
    assert "/csv" not in replies[0] and "/report" not in replies[0]  # only commands that are set up
    assert replies[1] == replies[0]  # /start is /help
    assert "Unknown command" in replies[2]  # nothing that changes state exists
    assert replies[3] == "STATUS TEXT"  # "@botname" is accepted


async def test_a_failing_report_is_answered_and_the_loop_survives(caplog):
    async def boom(args):
        raise RuntimeError("secret internals")

    caplog.set_level(logging.WARNING)
    poller = FakePoller([], [msg(1, "/status")], [msg(2, "/help")])
    replies = await drive(poller, handlers={"status": boom}, until=lambda r: len(r) == 2)
    assert "could not be built" in replies[0] and "secret internals" not in replies[0]
    assert "/status failed (RuntimeError)" in caplog.text and "secret internals" not in caplog.text
    assert "Commands" in replies[1]  # still listening


async def test_polling_errors_back_off_and_recover_without_leaking(caplog):
    caplog.set_level(logging.WARNING)
    poller = FakePoller(
        NotifierError("Telegram getUpdates failed after 1 attempts: network error (ClientConnectionError)"),
        [],
        NotifierError("Telegram refused getUpdates: 409 Conflict: terminated by other getUpdates request"),
        [msg(1, "/status")],
    )
    replies = await drive(poller, handlers={"status": status_ok}, until=lambda r: r)
    assert replies == ["STATUS TEXT"]
    assert "another process is polling this bot token" in caplog.text  # the 409 hint


async def test_an_unexpected_error_logs_only_its_class(caplog):
    caplog.set_level(logging.WARNING)
    poller = FakePoller([], ValueError("https://api.telegram.org/bot123456:SECRETSECRETSECRETSECRET/getUpdates"), [msg(1, "/help")])
    await drive(poller, until=lambda r: r)
    assert "ValueError" in caplog.text and "SECRET" not in caplog.text


async def test_the_backlog_skip_is_retried_until_it_works():
    poller = FakePoller(NotifierError("boom"), [msg(5, "/status")], [msg(6, "/status")])
    replies = await drive(poller, handlers={"status": status_ok}, until=lambda r: r)
    assert replies == ["STATUS TEXT"] and poller.calls[1] == (-1, 0)  # skipped before acting


async def test_stopping_ends_a_long_poll_at_once_and_closes_the_poller():
    poller = FakePoller([])  # then blocks for an hour
    await drive(poller)
    assert poller.closed


async def test_the_words_after_the_command_reach_the_handler():
    seen = []

    async def echo(args):
        seen.append(args)
        return "ok"

    poller = FakePoller([], [msg(1, "/report 2026-10"), msg(2, "/report"), msg(3, "/report@bot  2026   x")])
    await drive(poller, handlers={"report": echo}, until=lambda r: len(r) == 3)
    assert seen == [["2026-10"], [], ["2026", "x"]]


async def test_a_document_reply_is_sent_as_a_file_with_an_escaped_caption():
    async def report(args):
        return Document("ventas_2026-10.txt", b"a\tb\n", "2 <filas> & mas")

    sender = FakeSender()
    poller = FakePoller([], [msg(1, "/report")])
    replies = await drive(poller, handlers={"report": report}, sender=sender, until=lambda r: False)
    assert sender.sent == [("ventas_2026-10.txt", b"a\tb\n", "2 &lt;filas&gt; &amp; mas")]
    assert replies == [] and sender.closed  # no text reply, and the sender is closed on stop


async def test_a_file_that_cannot_be_sent_is_explained_without_the_token():
    async def report(args):
        return Document("x.txt", b"x")

    sender = FakeSender(fail=NotifierError("Telegram refused sendDocument: 400 Bad Request: chat not found"))
    poller = FakePoller([], [msg(1, "/report"), msg(2, "/report")])
    replies = await drive(poller, handlers={"report": report}, sender=sender, until=lambda r: len(r) == 2)
    assert all("could not be sent" in r and "chat not found" in r for r in replies)  # and the loop went on


async def test_help_lists_the_report_commands_when_they_exist():
    poller = FakePoller([], [msg(1, "/help")])
    handlers = {"status": status_ok, "csv": status_ok, "report": status_ok}
    (reply,) = await drive(poller, handlers=handlers, until=lambda r: r)
    assert "/csv [year|all]" in reply and "/report [year|yyyy-mm]" in reply and "Ecuador" in reply
