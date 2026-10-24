"""The web side and the runner as two real processes: owner check, password,
sessions, request checks, lockout and passkeys (with a software authenticator)."""

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

from conftest import CONSOLE, calls, load

OWNER = "owner@example.com"
PASSWORD = "correct horse battery staple"   # noqa: S105 (test password)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base, origin):
        self.base, self.origin = base, origin
        self.jar = CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, path, body=None, login=OWNER, header=True, origin=None):
        h = {"Content-Type": "application/json", "Origin": origin or self.origin}
        if login:
            h["Tailscale-User-Login"] = login
        if header:
            h["X-Console-Action"] = "1"
        req = urllib.request.Request(self.base + path, headers=h, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with self.op.open(req, timeout=20) as r:
                raw = r.read()
                return r.status, json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


@pytest.fixture
def server(site):
    pw = load("agent-console").hash_password(PASSWORD)
    site["password"].write_text(pw + "\n")
    (site["web"] / "status.json").write_text('{"generated": 1}')
    port = free_port()
    origin = f"https://host.test:{port}"
    env = dict(os.environ, CONSOLE_PORT=str(port), CONSOLE_BIND="127.0.0.1", CONSOLE_COOKIE_SECURE="0",
               OWNER_LOGIN=OWNER, CONSOLE_ORIGIN=origin, CONSOLE_TERM_PORT=str(free_port()))
    procs = [subprocess.Popen([sys.executable, str(CONSOLE / "agent-console.py"), mode], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for mode in ("runner", "serve")]
    sock = site["run"] / "runner.sock"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            if sock.exists():
                break
        except OSError:
            pass
        time.sleep(0.05)
    yield Client(f"http://127.0.0.1:{port}", origin)
    for p in procs:
        p.terminate()
        p.wait(5)


def test_serves_the_page_and_status_to_anyone_on_the_network(server):
    st, body = server.call("/", login=None)
    assert st == 200 and b"<title>test</title>" in body
    assert server.call("/status.json", login=None)[0] == 200
    assert server.call("/../console.toml", login=None)[0] == 404


def test_runner_socket_is_private(server, site):
    assert (site["run"] / "runner.sock").stat().st_mode & 0o077 == 0     # owner only


def test_actions_need_the_owner_login(server):
    assert server.call("/api/ping", login=None)[0] == 403
    assert server.call("/api/ping", login="someone@else.com")[0] == 403
    assert server.call("/api/unlock", {"password": PASSWORD}, login="someone@else.com")[0] == 403
    st, ping = server.call("/api/ping")
    assert st == 200 and ping["actions"]["restart"] is True and ping["unlocked_until"] is None


def test_password_session_and_lock(server, site):
    assert server.call("/api/action", {"action": "restart", "target": "agent-runner"})[0] == 401
    assert server.call("/api/unlock", {"password": "wrong"})[0] == 401
    st, body = server.call("/api/unlock", {"password": PASSWORD})
    assert st == 200 and body["expires_at"] > time.time() + 1700
    cookie = next(iter(server.jar))
    assert cookie.name == "console" and cookie.has_nonstandard_attr("HttpOnly")
    st, body = server.call("/api/action", {"action": "restart", "target": "agent-runner"})
    assert st == 200, body
    assert "docker restart --time 60 agent-runner" in calls(site)
    assert server.call("/api/ping")[1]["unlocked_until"]
    assert server.call("/api/lock", {})[0] == 200
    assert server.call("/api/action", {"action": "restart", "target": "agent-runner"})[0] == 401
    audit = [json.loads(line) for line in site["log"].read_text().splitlines()]
    assert [a["action"] for a in audit if a.get("via") == "web"] == ["unlock", "unlock"]
    assert [a["exit"] for a in audit if a["action"] == "unlock"] == [1, 0]


def test_cross_site_requests_are_refused(server):
    assert server.call("/api/unlock", {"password": PASSWORD}, header=False)[0] == 403
    assert server.call("/api/unlock", {"password": PASSWORD}, origin="https://evil.test")[0] == 403


def test_runner_rechecks_what_the_page_sends(server, site):
    server.call("/api/unlock", {"password": PASSWORD})
    st, body = server.call("/api/action", {"action": "cron", "target": "nightly-backup", "value": "* * * * * sh -c id"})
    assert st == 400
    assert not any(c.startswith("crontab -") and c != "crontab -l" for c in calls(site))


def test_five_wrong_passwords_lock_unlocking(server):
    for _ in range(5):
        assert server.call("/api/unlock", {"password": "wrong"})[0] == 401
    assert server.call("/api/unlock", {"password": PASSWORD})[0] == 429


# ---- passkeys: a software authenticator (ES256, "none" attestation) ----

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def cbor(v):
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        if n < 256:
            return bytes([major << 5 | 24, n])
        return bytes([major << 5 | 25]) + n.to_bytes(2, "big")
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        return head(3, len(v.encode())) + v.encode()
    if isinstance(v, dict):
        return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(v)


class Authenticator:
    def __init__(self, client):
        self.c = client
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred = os.urandom(16)
        self.rp_hash = hashlib.sha256(b"host.test").digest()

    def client_data(self, kind, challenge, origin=None):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.c.origin}).encode()

    def register(self, origin=None):
        st, opt = self.c.call("/api/passkey/options", {"purpose": "register"})
        if st != 200:
            return st, opt
        n = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}
        ad = self.rp_hash + bytes([0x45]) + bytes(4) + bytes(16) + len(self.cred).to_bytes(2, "big") + self.cred + cbor(cose)
        att = cbor({"fmt": "none", "attStmt": {}, "authData": ad})
        cd = self.client_data("webauthn.create", opt["publicKey"]["challenge"], origin)
        return self.c.call("/api/passkey/register", {"id": b64u(self.cred), "clientDataJSON": b64u(cd),
                                                      "attestationObject": b64u(att), "name": "Test laptop"})

    def assertion(self, flags=0x05, counter=1, origin=None, forge=False):
        st, opt = self.c.call("/api/passkey/options", {"purpose": "login"})
        cd = self.client_data("webauthn.get", opt["publicKey"]["challenge"], origin)
        ad = self.rp_hash + bytes([flags]) + counter.to_bytes(4, "big")
        signer = ec.generate_private_key(ec.SECP256R1()) if forge else self.key
        sig = signer.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred), "clientDataJSON": b64u(cd), "authenticatorData": b64u(ad), "signature": b64u(sig)}


def test_passkey_register_needs_an_unlocked_session(server):
    assert Authenticator(server).register()[0] == 401


def test_passkey_unlock_and_its_failures(server, site):
    auth = Authenticator(server)
    server.call("/api/unlock", {"password": PASSWORD})
    assert auth.register(origin="https://evil.test")[0] == 400
    st, body = auth.register()
    assert st == 200, body
    assert auth.register()[0] == 400                              # the same key twice
    stored = json.loads((site["state"] / "passkeys.json").read_text())
    assert len(stored) == 1 and stored[0]["name"] == "Test laptop"
    server.call("/api/lock", {})

    good = auth.assertion(counter=5)
    assert server.call("/api/passkey/login", good)[0] == 200
    server.call("/api/lock", {})
    assert server.call("/api/passkey/login", good)[0] == 401      # replay: the challenge is single-use
    assert server.call("/api/passkey/login", auth.assertion(counter=3))[0] == 401          # counter went backwards
    assert server.call("/api/passkey/login", auth.assertion(flags=0x01, counter=9))[0] == 401   # no user verification
    assert server.call("/api/passkey/login", auth.assertion(counter=9, origin="https://evil.test"))[0] == 401
    assert server.call("/api/passkey/login", auth.assertion(counter=9, forge=True))[0] == 401
    assert server.call("/api/passkey/login", auth.assertion(counter=9))[0] == 429          # five failures
