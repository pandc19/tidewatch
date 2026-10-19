"""TelegramNotifier against a local aiohttp server that imitates the Bot API (real HTTP)."""

import asyncio
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.services.notifications import NotifierError, TelegramNotifier, chats_from_updates

TOKEN = "123456789:AAH-secret_token_value_0123456789"
CHAT = "42"


class FakeTelegram:
    """Answers with the scripted responses in order (the last one repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses) or [(200, {"ok": True, "result": {"message_id": 1}})]
        self.requests: list[dict] = []

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append({"path": request.path, "body": body})
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        status, payload = self.responses[index]
        if callable(payload):
            return await payload(request)
        return web.json_response(payload, status=status)


@pytest.fixture
async def serve():
    servers: list[TestServer] = []
    notifiers: list[TelegramNotifier] = []

    async def start(*responses, **options) -> tuple[FakeTelegram, TelegramNotifier, list[float]]:
        fake = FakeTelegram(*responses)
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", fake.handle)
        server = TestServer(app)
        await server.start_server(access_log=None)  # the fake's own access log would print the URL
        servers.append(server)
        slept: list[float] = []

        async def sleep(seconds):
            slept.append(seconds)

        options.setdefault("min_interval", 0)
        notifier = TelegramNotifier(
            TOKEN, CHAT, base_url=str(server.make_url("")), sleep=sleep, **options
        )
        notifiers.append(notifier)
        return fake, notifier, slept

    yield start
    for notifier in notifiers:
        await notifier.close()
    for server in servers:
        await server.close()


OK = (200, {"ok": True, "result": {"message_id": 1}})


async def test_a_message_is_posted_with_the_exact_fields(serve):
    fake, notifier, _ = await serve(OK)

    await notifier.send("<b>hi</b> & bye")

    (req,) = fake.requests
    assert req["path"] == f"/bot{TOKEN}/sendMessage"
    assert req["body"] == {
        "chat_id": CHAT,
        "text": "<b>hi</b> & bye",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


async def test_a_429_waits_retry_after_then_succeeds(serve):
    limited = (429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                     "parameters": {"retry_after": 3}})
    fake, notifier, slept = await serve(limited, OK)

    await notifier.send("x")

    assert len(fake.requests) == 2
    assert slept == [3.0]


async def test_retry_after_is_capped(serve):
    limited = (429, {"ok": False, "parameters": {"retry_after": 3600}})
    _, notifier, slept = await serve(limited, OK, max_retry_after=30)

    await notifier.send("x")

    assert slept == [30.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_client_errors_are_not_retried_and_say_why(serve, status):
    fake, notifier, slept = await serve((status, {"ok": False, "error_code": status,
                                                  "description": "Bad Request: chat not found"}))

    with pytest.raises(NotifierError) as err:
        await notifier.send("x")

    assert len(fake.requests) == 1 and slept == []
    assert str(status) in str(err.value) and "chat not found" in str(err.value)
    assert TOKEN not in str(err.value)


async def test_a_bad_token_hints_at_the_setting(serve):
    _, notifier, _ = await serve((401, {"ok": False, "description": "Unauthorized"}))

    with pytest.raises(NotifierError, match="TELEGRAM_BOT_TOKEN"):
        await notifier.send("x")


async def test_a_server_error_is_retried_with_backoff_then_succeeds(serve):
    fake, notifier, slept = await serve((502, {"ok": False}), (500, {"ok": False}), OK, backoff=1.0)

    await notifier.send("x")

    assert len(fake.requests) == 3
    assert slept == [1.0, 2.0]


async def test_persistent_server_errors_give_up_after_the_attempt_limit(serve):
    fake, notifier, _ = await serve((500, {"ok": False}), attempts=3)

    with pytest.raises(NotifierError, match="after 3 attempts"):
        await notifier.send("x")

    assert len(fake.requests) == 3


async def test_a_timeout_is_retried_and_reported_without_the_url(serve):
    async def hang(request):
        await asyncio.sleep(5)
        return web.json_response({"ok": True})

    fake, notifier, _ = await serve((200, hang), timeout=0.1, attempts=2)

    with pytest.raises(NotifierError) as err:
        await notifier.send("x")

    assert len(fake.requests) == 2
    assert "TimeoutError" in str(err.value) and TOKEN not in str(err.value)


async def test_a_refused_connection_is_a_clean_error():
    async def sleep(_):
        return None

    notifier = TelegramNotifier(TOKEN, CHAT, base_url="http://127.0.0.1:1", attempts=2,
                                min_interval=0, sleep=sleep)
    try:
        with pytest.raises(NotifierError) as err:
            await notifier.send("x")
    finally:
        await notifier.close()

    assert TOKEN not in str(err.value) and "network error" in str(err.value)
    assert err.value.__cause__ is None and err.value.__context__ is None  # no chained aiohttp error


async def test_the_token_never_appears_in_logs_or_errors(serve, caplog):
    caplog.set_level(logging.DEBUG)
    echo = (400, {"ok": False, "description": f"bad request to /bot{TOKEN}/sendMessage"})
    _, notifier, _ = await serve((500, {"ok": False}), echo)

    with pytest.raises(NotifierError) as err:
        await notifier.send("x")

    assert TOKEN not in str(err.value) and TOKEN not in repr(err.value)
    assert TOKEN not in caplog.text
    assert "AAH-secret" not in caplog.text


async def test_sends_are_paced(serve):
    fake, notifier, slept = await serve(OK, min_interval=1.0)

    await notifier.send("a")
    await notifier.send("b")

    assert len(fake.requests) == 2
    assert len(slept) == 1 and 0 < slept[0] <= 1.0  # the second send waited out the interval


async def test_get_updates_returns_the_result_list(serve):
    updates = [{"update_id": 1, "message": {"chat": {"id": 7, "first_name": "Ana", "username": "ana"}}}]
    fake, notifier, _ = await serve((200, {"ok": True, "result": updates}))

    assert await notifier.get_updates() == updates
    assert fake.requests[0]["path"].endswith("/getUpdates")


def test_chats_from_updates_lists_ids_and_names_once():
    updates = [
        {"message": {"chat": {"id": 7, "first_name": "Ana", "last_name": "Lopez", "username": "ana"}}},
        {"message": {"chat": {"id": 7, "first_name": "Ana"}}},
        {"channel_post": {"chat": {"id": -100, "title": "Alerts"}}},
        {"edited_message": {}},
    ]

    assert chats_from_updates(updates) == {7: "Ana Lopez (@ana)", -100: "Alerts"}


async def test_poll_updates_sends_the_long_poll_fields_and_returns_the_list(serve):
    updates = [{"update_id": 9, "message": {"text": "/status"}}]
    fake, notifier, _ = await serve((200, {"ok": True, "result": updates}))

    assert await notifier.poll_updates(10, 25) == updates

    (request,) = fake.requests
    assert request["path"] == f"/bot{TOKEN}/getUpdates"
    assert request["body"] == {"offset": 10, "timeout": 25, "limit": 20, "allowed_updates": ["message"]}


async def test_poll_updates_without_an_offset_omits_it_and_a_conflict_is_not_retried(serve):
    fake, notifier, _ = await serve((409, {"ok": False, "description": "Conflict: terminated by other getUpdates request"}))

    with pytest.raises(NotifierError) as err:
        await notifier.poll_updates(None, 0)

    assert "409" in str(err.value) and TOKEN not in str(err.value)
    assert "offset" not in fake.requests[0]["body"] and len(fake.requests) == 1
