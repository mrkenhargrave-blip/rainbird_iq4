#!/usr/bin/env python3
"""
Rain Bird IQ4 - WR2 AppSync sensor test (curl_cffi)

Standalone check for the new getDeviceStateTable AppSync query added in
api.py/coordinator.py/binary_sensor.py — exercises the exact same request
the integration now makes, without needing a running Home Assistant at all.

Logs in, finds the controller's deviceUUID, queries
SK="Event#RainSensorState", and prints what comes back. Useful both to
confirm the query still works and to see what a DRY sensor reports (an
explicit state:0 vs. no stored event at all) — that's still unconfirmed
as of this writing, see get_rain_sensor_state()'s docstring in api.py.

Standalone: does not import Home Assistant. Only needs curl_cffi.
Read-only: issues GET/POST requests that only read data, changes nothing
on the account.

Usage:   python3 test_wr2_appsync.py <email> [--channel web|app]
         (you'll be prompted for the password; passing it as an argument
          also works but leaves it in your shell history)
Requires: pip install curl_cffi
"""
import argparse
import base64
import getpass
import hashlib
import json
import re
import secrets
import sys
from urllib.parse import parse_qs, quote, urljoin, urlparse

try:
    from curl_cffi import requests as cf
except ImportError:
    print("Missing dependency. Install it with: pip install curl_cffi")
    sys.exit(1)

AUTH_BASE = "https://iq4server.rainbird.com/coreidentityserver"
API_BASE = "https://iq4server.rainbird.com/coreapi/api"
APPSYNC_URL = "https://m3iuhu3l3zbjpkctbnh2of4chm.appsync-api.us-west-2.amazonaws.com/graphql"

# Web/IQ channel
CLIENT_ID_WEB = "C5A6F324-3CD3-4B22-9F78-B4835BA55D25"

# Mobile app channel (Authorization Code + PKCE) -- mirrors const.py exactly
APP_CLIENT_ID = "5B0FA4CD-8248-4BEB-B89A-F0AF8A254DB5"
APP_CLIENT_SECRET = "537C58B6-DCCF-4718-BFE6-CCD0D3FCDC07"
APP_REDIRECT_URI = "com.rainbird.mobile://auth"
APP_SCOPE = "coreAPI.read coreAPI.write openid profile offline_access"
_MAX_REDIRECTS = 10

DEVICE_STATE_QUERY = (
    "query getDeviceStateTable($PK: String, $SK: String) {\n"
    "  getDeviceStateTable(PK: $PK, SK: $SK) {\n"
    "    SK\n"
    "    Data\n"
    "    __typename\n"
    "  }\n"
    "}\n"
)


def _make_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE (code_verifier, code_challenge) pair using S256. Mirrors auth.py."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def fetch_token_web(session: cf.Session, username: str, password: str) -> str:
    """Web/IQ channel login -- mirrors auth.py's fetch_token()."""
    state = secrets.token_hex(8).upper()
    nonce = secrets.token_hex(8).upper()
    return_url_raw = (
        "/coreidentityserver/connect/authorize/callback"
        f"?client_id={CLIENT_ID_WEB}"
        "&redirect_uri=https%3A%2F%2Fiq4.rainbird.com%2Fauth.html"
        "&response_type=id_token%20token"
        "&scope=coreAPI.read%20coreAPI.write%20openid%20profile"
        f"&state={state}&nonce={nonce}"
    )
    return_url_encoded = quote(return_url_raw, safe="")
    login_url = f"{AUTH_BASE}/Account/Login?ReturnUrl={return_url_encoded}"

    r1 = session.get(login_url)
    if r1.status_code != 200:
        raise RuntimeError(f"Login page failed: HTTP {r1.status_code}")

    match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
    if not match:
        raise RuntimeError("CSRF token not found in login page")
    csrf = match.group(1)

    r2 = session.post(
        login_url,
        data={
            "Username": username,
            "Password": password,
            "ReturnUrl": return_url_raw,
            "__RequestVerificationToken": csrf,
        },
        allow_redirects=True,
    )

    access_token = None
    for text in (r2.url, r2.text):
        m = re.search(r"access_token=([^&\"]+)", text or "")
        if m:
            access_token = m.group(1)
            break
    if not access_token:
        raise RuntimeError("Authentication failed. Check your username and password.")
    return access_token


def fetch_token_app(session: cf.Session, username: str, password: str) -> str:
    """Mobile-app channel login (Authorization Code + PKCE) -- mirrors auth.py's
    fetch_token_isapp(). Not subject to the web-channel's IQ-Access-tier cap."""
    state = secrets.token_hex(8).upper()
    nonce = secrets.token_hex(8).upper()
    code_verifier, code_challenge = _make_pkce_pair()

    return_url_raw = (
        "/coreidentityserver/connect/authorize/callback"
        f"?client_id={APP_CLIENT_ID}"
        f"&redirect_uri={quote(APP_REDIRECT_URI, safe='')}"
        "&response_type=code"
        f"&code_challenge={code_challenge}"
        "&code_challenge_method=S256"
        f"&scope={quote(APP_SCOPE, safe='')}"
        f"&state={state}&nonce={nonce}"
    )
    login_url = f"{AUTH_BASE}/Account/Login?ReturnUrl={quote(return_url_raw, safe='')}"

    r1 = session.get(login_url)
    if r1.status_code != 200:
        raise RuntimeError(f"Login page failed: HTTP {r1.status_code}")

    match = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r1.text)
    if not match:
        raise RuntimeError("CSRF token not found in login page")
    csrf = match.group(1)

    resp = session.post(
        login_url,
        data={
            "Username": username,
            "Password": password,
            "ReturnUrl": return_url_raw,
            "__RequestVerificationToken": csrf,
        },
        allow_redirects=False,
    )

    if resp.status_code == 200 and not (
        resp.headers.get("location") or resp.headers.get("Location")
    ):
        raise RuntimeError(
            "Login rejected (server returned the login page instead of "
            "redirecting) -- check the username/password, or try --channel web."
        )

    code = None
    current_url = login_url
    for _ in range(_MAX_REDIRECTS):
        location = resp.headers.get("location") or resp.headers.get("Location")
        if not location:
            raise RuntimeError(f"No redirect while logging in (HTTP {resp.status_code}).")
        absolute = urljoin(current_url, location)
        parsed = urlparse(absolute)
        found = parse_qs(parsed.query).get("code", [None])[0]
        if found:
            code = found
            break
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError(f"Reached final redirect but found no authorization code: {absolute}")
        current_url = absolute
        resp = session.get(current_url, allow_redirects=False)

    if not code:
        raise RuntimeError("Too many redirects while logging in -- no code found.")

    basic = base64.b64encode(f"{APP_CLIENT_ID}:{APP_CLIENT_SECRET}".encode()).decode()
    token_resp = session.post(
        f"{AUTH_BASE}/connect/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "*/*",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": APP_REDIRECT_URI,
        },
    )
    if token_resp.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed: HTTP {token_resp.status_code}, body: {token_resp.text[:200]}"
        )

    token = token_resp.json().get("access_token")
    if not token:
        raise RuntimeError("Token exchange succeeded but no access_token returned.")
    return token


def get_satellite_list(session: cf.Session, token: str) -> list:
    r = session.get(
        f"{API_BASE}/Satellite/GetSatelliteList",
        params={"includeInvisibleToCurrentUser": "false"},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json() or []


def get_satellite(session: cf.Session, token: str, satellite_id: int) -> dict | None:
    r = session.get(
        f"{API_BASE}/Satellite/GetSatellite",
        params={"satelliteId": satellite_id},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30,
    )
    if r.status_code != 200:
        return None
    return r.json()


def get_device_state(session: cf.Session, token: str, device_uuid: str, sort_key: str) -> tuple[int, dict]:
    """POST the same getDeviceStateTable query api.py now sends.

    Note the auth header here is the RAW token, no "Bearer " prefix --
    that's what the web portal itself sends to AppSync, and a prefixed
    token gets rejected.
    """
    body = {
        "operationName": "getDeviceStateTable",
        "variables": {"PK": device_uuid, "SK": sort_key},
        "query": DEVICE_STATE_QUERY,
    }
    r = session.post(
        APPSYNC_URL,
        json=body,
        headers={"Content-Type": "application/json", "Authorization": token},
        timeout=30,
    )
    try:
        return r.status_code, r.json()
    except Exception as e:
        return r.status_code, {"_parse_error": str(e), "_raw": r.text[:500]}


def main():
    parser = argparse.ArgumentParser(
        description="Test the new AppSync WR2 rain-sensor query against your account."
    )
    parser.add_argument("email")
    parser.add_argument(
        "password", nargs="?", default=None,
        help="Optional. Leave it out and you'll be prompted instead, which "
             "keeps your password out of the shell history and out of ps.",
    )
    parser.add_argument(
        "--channel", choices=["web", "app"], default="app",
        help="Authentication channel (default: app, since that's the one "
             "confirmed to reach GetSatellite without a 403 on tier-0 accounts).",
    )
    parser.add_argument(
        "--satellite", type=int, default=None,
        help="Satellite id to inspect. Defaults to the first one on the account.",
    )
    parser.add_argument(
        "--sk", default="Event#RainSensorState",
        help="AppSync sort key to query (default: Event#RainSensorState). "
             "Try other Event# values here if you want to probe for more.",
    )
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass(f"Rain Bird password for {args.email}: ")
    if not password:
        print("No password given.")
        return 1

    print(f"\nStep 1: authenticating (channel={args.channel})...")
    session = cf.Session(impersonate="chrome")
    try:
        if args.channel == "app":
            token = fetch_token_app(session, args.email, password)
        else:
            token = fetch_token_web(session, args.email, password)
    except RuntimeError as err:
        print(f"  FAILED: {err}")
        return 1
    print("  ok")

    print("\nStep 2: listing satellites...")
    satellites = get_satellite_list(session, token)
    if not satellites:
        print("  No satellites returned; cannot continue.")
        return 1
    satellite_id = args.satellite or satellites[0].get("id")
    chosen = next((s for s in satellites if s.get("id") == satellite_id), None)
    device_uuid = (chosen or {}).get("deviceUUID")
    print(f"  using satelliteId {satellite_id}, deviceUUID={device_uuid!r}")

    if not device_uuid:
        print("\nStep 3: deviceUUID missing from GetSatelliteList, trying GetSatellite...")
        detail = get_satellite(session, token, satellite_id)
        if detail:
            device_uuid = (detail.get("asset") or {}).get("uuid")
        print(f"  deviceUUID={device_uuid!r}")

    if not device_uuid:
        print("\nNo deviceUUID found anywhere -- cannot query AppSync. Stopping.")
        return 1

    print(f"\nStep 4: querying AppSync getDeviceStateTable(PK={device_uuid!r}, SK={args.sk!r})...")
    status, payload = get_device_state(session, token, device_uuid, args.sk)
    print(f"  HTTP {status}")
    print(json.dumps(payload, indent=2))

    if status == 200 and not payload.get("errors"):
        item = (payload.get("data") or {}).get("getDeviceStateTable")
        if item is None:
            print(f"\nResult: no stored event for SK={args.sk!r}.")
            print("  This is the case we haven't confirmed the meaning of yet --")
            print("  is this what a DRY sensor reports, or would a dry sensor")
            print("  give an explicit Data:{\"state\":0} instead? If it's currently")
            print("  dry when you run this, this result answers that question.")
        else:
            data_str = item.get("Data")
            print(f"\nResult: SK={item.get('SK')!r}, Data={data_str!r}")
            try:
                parsed = json.loads(data_str) if data_str else None
                print(f"  parsed state = {parsed.get('state') if parsed else None!r}")
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
