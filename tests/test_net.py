"""Secret scrubbing and transport-level retry behaviour."""

from __future__ import annotations

import pytest

from tests.factories import FakeResponse, FakeSession
from wallet_analyzer.net import HttpClient, ProviderError, scrub


def test_query_token_is_redacted_but_host_is_kept() -> None:
    cleaned = scrub("failed: https://mainnet.helius-rpc.com/?api-key=SUPERSECRET")
    assert "SUPERSECRET" not in cleaned
    assert "mainnet.helius-rpc.com" in cleaned


def test_path_token_is_redacted() -> None:
    cleaned = scrub("POST https://abc.solana-mainnet.quiknode.pro/PATHSECRET/ failed")
    assert "PATHSECRET" not in cleaned
    assert "abc.solana-mainnet.quiknode.pro" in cleaned


def test_bare_parameter_is_masked() -> None:
    assert scrub("apikey=ABC123&x=1") == "apikey=***&x=1"
    assert scrub("api_code=ABC123") == "api_code=***"


def test_plain_host_message_is_untouched() -> None:
    message = "HTTP 429 from api.etherscan.io"
    assert scrub(message) == message


def test_provider_error_scrubs_on_construction() -> None:
    error = ProviderError("boom https://rpc.example.com/v2/TOKENVALUE?api_key=OTHER")
    text = str(error)
    assert "TOKENVALUE" not in text
    assert "OTHER" not in text
    assert "rpc.example.com" in text


def test_jwt_is_redacted_even_when_quoted_as_a_url() -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6ImFAYi5jb20ifQ.d00f_MY_ok"

    cleaned = scrub(f"Invalid URL '{token}': No scheme supplied. Perhaps you meant https://{token}/x")

    assert "eyJhbGciOiJIUzI1NiJ9" not in cleaned
    assert "d00f_MY_ok" not in cleaned
    assert "[redacted-token]" in cleaned


def test_uuid_key_is_redacted() -> None:
    uuid = "3f2b8c1e-9a4d-4c7b-8e21-5d6f7a8b9c0d"
    assert uuid not in scrub(f"portal key {uuid} rejected")


# -- retry behaviour -----------------------------------------------------

HOST = "retry-probe.example"


@pytest.fixture(autouse=True)
def slept(monkeypatch) -> list[float]:
    """Record backoff delays instead of sleeping; autouse so no test waits."""

    delays: list[float] = []
    monkeypatch.setattr("wallet_analyzer.net.time.sleep", lambda seconds: delays.append(seconds))
    return delays


def client_with(responses) -> tuple[HttpClient, FakeSession]:
    session = FakeSession(responses)
    return HttpClient(session=session), session


def test_retries_429_then_succeeds() -> None:
    client, session = client_with(
        [
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(200, {"usd": 1.0}),
        ]
    )

    payload = client.get_json("https://api.example/v3/simple/price", host=HOST)

    assert payload == {"usd": 1.0}
    assert session.attempts == 3


def test_backoff_grows_between_attempts(slept) -> None:
    client, session = client_with(
        [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {"ok": True})]
    )

    client.get_json("https://api.example/v3/simple/price", host=HOST)

    assert session.attempts == 2
    # The limiter may also sleep, so only assert that a real backoff happened.
    assert max(slept) >= 0.8


def test_persistent_429_exhausts_attempts_with_status() -> None:
    client, session = client_with([FakeResponse(429, headers={}) for _ in range(4)])

    with pytest.raises(ProviderError) as excinfo:
        client.get_json("https://api.example/v3/simple/price", host=HOST)

    assert excinfo.value.status == 429
    assert session.attempts == 4


def test_hours_long_retry_after_fails_fast_without_sleeping(slept) -> None:
    client, session = client_with(
        [FakeResponse(429, headers={"Retry-After": "85781"})]
    )

    with pytest.raises(ProviderError, match="unavailable for 85781s") as excinfo:
        client.get_json("https://api.example/address/x", host=HOST)

    assert excinfo.value.status == 429
    assert session.attempts == 1
    # Only the shared rate limiter may sleep; no 20s backoff is taken.
    assert all(delay < 20 for delay in slept)


def test_http_200_rate_limit_body_is_retried(slept) -> None:
    limited = {
        "status": "0",
        "message": "NOTOK",
        "result": "Max calls per sec rate limit reached (3/sec)",
    }
    client, session = client_with(
        [FakeResponse(200, limited), FakeResponse(200, {"status": "1", "result": "0x0"})]
    )

    payload = client.get_json("https://api.etherscan.io/v2/api", host=HOST)

    assert payload == {"status": "1", "result": "0x0"}
    assert session.attempts == 2
    assert max(slept) >= 0.8


def test_persistent_http_200_rate_limit_exhausts_with_429_status() -> None:
    limited = {
        "status": "0",
        "message": "NOTOK",
        "result": "Max calls per sec rate limit reached (3/sec)",
    }
    client, session = client_with([FakeResponse(200, limited) for _ in range(4)])

    with pytest.raises(ProviderError) as excinfo:
        client.get_json("https://api.etherscan.io/v2/api", host=HOST)

    assert excinfo.value.status == 429
    assert "rate limited" in str(excinfo.value)
    assert "3/sec" in str(excinfo.value)
    assert session.attempts == 4


def test_http_200_notok_without_throttling_is_not_retried() -> None:
    client, session = client_with(
        [
            FakeResponse(
                200,
                {"status": "0", "message": "NOTOK", "result": "Invalid API Key"},
            )
        ]
    )

    payload = client.get_json("https://api.etherscan.io/v2/api", host=HOST)

    assert payload["result"] == "Invalid API Key"
    assert session.attempts == 1


def test_json_rpc_429_error_body_is_retried() -> None:
    client, session = client_with(
        [
            FakeResponse(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": 429, "message": "Too Many Requests"},
                },
            ),
            FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "result": "0x1"}),
        ]
    )

    payload = client.get_json("https://rpc.example", host=HOST)

    assert payload["result"] == "0x1"
    assert session.attempts == 2


def test_400_is_not_retried_by_default() -> None:
    client, session = client_with([FakeResponse(400, {"error": "bad request"})])

    with pytest.raises(ProviderError) as excinfo:
        client.get_json("https://api.example/v3/simple/price", host=HOST)

    assert excinfo.value.status == 400
    assert session.attempts == 1


def test_400_is_retried_when_opted_in() -> None:
    client, session = client_with(
        [FakeResponse(400, {}), FakeResponse(400, {}), FakeResponse(200, {"usd": "1.0"})]
    )

    payload = client.get_json(
        "https://api.example/v3/simple/price", host=HOST, retry_statuses=(400,)
    )

    assert payload == {"usd": "1.0"}
    assert session.attempts == 3


def test_persistent_400_after_opt_in_raises_with_status() -> None:
    client, session = client_with([FakeResponse(400, {}) for _ in range(4)])

    with pytest.raises(ProviderError) as excinfo:
        client.get_json("https://api.example/v3/simple/price", host=HOST, retry_statuses=(400,))

    assert excinfo.value.status == 400
    assert session.attempts == 4


def test_empty_http_body_is_rejected_as_non_json() -> None:
    client, session = client_with([FakeResponse(200, None)])

    with pytest.raises(ProviderError, match="non-JSON response"):
        client.get_json("https://api.example", host=HOST)

    assert session.attempts == 1
