import time

from conftest import AuthedClient


def _register(client, email="user@example.com", password="correct horse battery"):
    return client.post("/auth/register", json={"email": email, "password": password})


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_login_and_me(client):
    response = _register(client)
    assert response.status_code == 201
    body = response.json()
    assert body["username"] and body["token"] and body["user_id"]

    me = client.get("/auth/me", headers=_headers(body["token"])).json()
    assert me["username"] == body["username"]
    assert me["is_demo"] is False

    # Login round-trip issues a fresh, valid token for the same account.
    login = client.post("/auth/login", json={"email": "user@example.com", "password": "correct horse battery"})
    assert login.status_code == 200
    assert login.json()["user_id"] == body["user_id"]


def test_registration_validation(client):
    assert _register(client, email="not-an-email").status_code == 422
    assert _register(client, password="short").status_code == 422
    assert _register(client).status_code == 201
    assert _register(client).status_code == 422  # duplicate email
    # Username collisions from similar emails resolve with a suffix, not an error.
    first = client.post("/auth/register", json={"email": "alex@x.io", "password": "long enough pass"})
    second = client.post("/auth/register", json={"email": "alex.2@x.io", "password": "long enough pass"})
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["username"] != second.json()["username"]


def test_wrong_password_rejected(client):
    _register(client)
    response = client.post("/auth/login", json={"email": "user@example.com", "password": "wrong password!"})
    assert response.status_code == 401


def test_every_watchlist_endpoint_requires_a_token(client):
    """No endpoint may be reachable without a valid bearer token. Previously an
    unauthenticated write created its own passwordless account and returned 201."""
    assert client.get("/watchlist").status_code == 401
    assert client.post("/watchlist/symbols", json={"symbol": "INFY"}).status_code == 401
    assert client.delete("/watchlist/symbols/INFY").status_code == 401
    assert client.post("/watchlist/seen").status_code == 401
    assert client.post("/poll", json={}).status_code == 401
    assert client.get("/auth/me").status_code == 401


def test_rejected_tokens(client):
    body = _register(client).json()
    from app import auth

    # Garbage, tampered signature, expired, and unknown-subject tokens all fail.
    for token in ("", "not.a.token", f"{body['user_id']}.9999999999.deadbeef"):
        assert client.get("/watchlist", headers=_headers(token)).status_code == 401
    expired = auth.make_token(body["user_id"], now=int(time.time()) - auth.TOKEN_LIFETIME_SECONDS - 10)
    assert client.get("/watchlist", headers=_headers(expired)).status_code == 401
    # A correctly signed token for an account that no longer exists is not a session.
    assert client.get("/watchlist", headers=_headers(auth.make_token(999_999))).status_code == 401
    # The genuine token still works.
    assert client.get("/watchlist", headers=_headers(body["token"])).status_code == 200


def test_ledgers_are_isolated_per_account(client):
    alice = AuthedClient(client)
    alice.as_new_user(email="alice@example.com")
    bob = AuthedClient(client)
    bob.as_new_user(email="bob@example.com")

    assert alice.add("RELIANCE").status_code == 201
    assert {i["symbol"] for i in alice.items()} == {"RELIANCE"}
    # Bob cannot see, and cannot delete, anything of Alice's.
    assert bob.items() == []
    assert bob.delete("/watchlist/symbols/RELIANCE").status_code == 404
    assert {i["symbol"] for i in alice.items()} == {"RELIANCE"}
    # Marking seen is scoped too: Bob's call must not touch Alice's rows.
    assert bob.post("/watchlist/seen").json()["marked"] == 0


def test_identity_cannot_be_claimed_by_registering_a_name(client):
    """Usernames are display names, not identity. Registering an email whose
    local part matches an existing account must never grant that account."""
    first = client.post("/auth/register", json={"email": "demo@example.com", "password": "long enough pass"}).json()
    second = client.post("/auth/register", json={"email": "demo@other.com", "password": "long enough pass"}).json()
    assert first["user_id"] != second["user_id"]
    assert first["username"] != second["username"]
    # The seeded demo account is untouched by either registration.
    demo = client.post("/auth/demo", json={"username": "demo"}).json()
    assert demo["user_id"] not in (first["user_id"], second["user_id"])


def test_registered_users_are_not_enumerated(client):
    _register(client, email="private@example.com")
    users = client.get("/users").json()["users"]
    assert set(users) == {"demo", "aarav", "priya", "rohan"}


def test_demo_accounts_need_a_token_but_no_password(client):
    # Open by design, yet still authenticated: /auth/demo mints a real token.
    session = client.post("/auth/demo", json={"username": "demo"})
    assert session.status_code == 200
    token = session.json()["token"]
    assert client.get("/watchlist", headers=_headers(token)).status_code == 200
    assert client.post("/watchlist/seen", headers=_headers(token)).status_code == 200
    assert client.get("/auth/me", headers=_headers(token)).json()["is_demo"] is True
    # Unknown and non-demo accounts cannot be opened this way.
    assert client.post("/auth/demo", json={"username": "nope"}).status_code == 404
    _register(client, email="real@example.com")
    assert client.post("/auth/demo", json={"username": "real"}).status_code == 404


def test_demo_login_does_not_accept_registered_accounts(client):
    """A registered account must not be openable through the passwordless path."""
    username = _register(client, email="victim@example.com").json()["username"]
    assert client.post("/auth/demo", json={"username": username}).status_code == 404


def test_poll_is_scoped_to_the_callers_symbols(client):
    alice = AuthedClient(client)
    alice.as_new_user(email="alice@example.com")
    alice.add("RELIANCE")
    bob = AuthedClient(client)
    bob.as_new_user(email="bob@example.com")

    # Bob may not drive provider traffic for a symbol he does not watch.
    assert bob.post("/poll", json={"symbol": "RELIANCE"}).status_code == 404
    assert alice.post("/poll", json={"symbol": "RELIANCE"}).status_code == 200
    # A bare poll only covers the caller's own watchlist.
    assert bob.post("/poll", json={}).json()["updates"] == []


def test_google_login_requires_configuration(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    assert client.get("/auth/config").json()["google_client_id"] is None
    assert client.post("/auth/google", json={"credential": "anything"}).status_code == 401


def test_google_login_links_and_reuses_accounts(client, monkeypatch):
    """A verified Google email links to the existing account rather than
    creating a second ledger, and repeat sign-ins reuse the same account."""
    from app import auth

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    existing = _register(client, email="user@example.com").json()

    monkeypatch.setattr(auth, "verify_google_id_token",
                        lambda credential: {"sub": "google-123", "email": "user@example.com", "name": "User"})
    linked = client.post("/auth/google", json={"credential": "fake"})
    assert linked.status_code == 200
    assert linked.json()["user_id"] == existing["user_id"]  # linked, not duplicated

    # Second sign-in resolves by Google subject to the same account.
    again = client.post("/auth/google", json={"credential": "fake"})
    assert again.json()["user_id"] == existing["user_id"]

    # A new Google user gets their own account, and the token works.
    monkeypatch.setattr(auth, "verify_google_id_token",
                        lambda credential: {"sub": "google-456", "email": "fresh@example.com", "name": "Fresh"})
    fresh = client.post("/auth/google", json={"credential": "fake"}).json()
    assert fresh["user_id"] != existing["user_id"]
    assert client.get("/watchlist", headers=_headers(fresh["token"])).status_code == 200


def test_google_rejects_unverified_email(client, monkeypatch):
    """google-auth validates signature and audience; we additionally refuse an
    unverified address, which anyone could otherwise claim."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    from app import auth
    import app.service as service

    captured = {}

    def fake_verify(credential, request, client_id):
        captured["client_id"] = client_id
        return {"iss": "accounts.google.com", "sub": "s", "email": "x@example.com", "email_verified": False}

    class _Module:
        verify_oauth2_token = staticmethod(fake_verify)

    monkeypatch.setitem(__import__("sys").modules, "google.oauth2.id_token", _Module)
    assert client.post("/auth/google", json={"credential": "fake"}).status_code == 401
    assert captured["client_id"] == "test-client-id"  # audience is checked against our client ID
    assert service.user_by_id(999_999) is None


def test_deleting_a_user_cascades(client):
    """foreign_keys=ON must be set per connection, or these rows are orphaned."""
    alice = AuthedClient(client)
    alice.as_new_user(email="alice@example.com")
    alice.add("RELIANCE")
    alice.post("/watchlist/seen")

    from app.db import connection
    with connection() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (alice.user_id,))
    with connection() as conn:
        items = conn.execute("SELECT COUNT(*) AS n FROM watchlist_items WHERE user_id=?", (alice.user_id,)).fetchone()["n"]
        state = conn.execute("SELECT COUNT(*) AS n FROM user_symbol_state WHERE user_id=?", (alice.user_id,)).fetchone()["n"]
    assert items == 0 and state == 0


def test_login_is_throttled_after_repeated_failures(client):
    _register(client)
    wrong = {"email": "user@example.com", "password": "wrong password!"}
    right = {"email": "user@example.com", "password": "correct horse battery"}
    for _ in range(8):
        assert client.post("/auth/login", json=wrong).status_code == 401

    blocked = client.post("/auth/login", json=wrong)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    # A correct password cannot bypass an active lockout either.
    assert client.post("/auth/login", json=right).status_code == 429

    # Another account is unaffected by this bucket.
    _register(client, email="other@example.com")
    other = client.post("/auth/login", json={"email": "other@example.com",
                                             "password": "correct horse battery"})
    assert other.status_code == 200
