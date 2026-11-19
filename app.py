# -*- coding: utf-8 -*-
"""
app.py — Free Fire Leaderboard (FINAL)
======================================
Region handling:
  • EU   → payload "SG"  (SG cluster covers EU)
  • CIS  → payload "SG"  (SG cluster covers CIS)
  • IND  → payload "IND" + "IN", needs genuine India-cluster account
  • BR/NA/US/SAC → client.us.freefiremobile.com
  • Everything else → clientbp.ppmainecoonghj.com
"""

import json
import os
import re
import time
import uuid
import threading
import logging
import urllib3
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from flask import Flask, jsonify, render_template, request, make_response

try:
    from google_play_scraper import app as play_scraper
    _GPS_OK = True
except Exception:
    _GPS_OK = False

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ff")

# ==================== CONFIG ====================
ACCOUNTS_FILE = "accounts.json"
AES_KEY = bytes([89, 103, 38, 116, 99, 37, 68, 69, 117, 104, 54, 37, 90, 99, 94, 56])
AES_IV  = bytes([54, 111, 121, 90, 68, 114, 50, 50, 69, 51, 121, 99, 104, 106, 77, 37])
HEX_KEY = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"

LOGIN_HOST = "loginbp.ppmainecoonghj.com"
CLIENT_HOST = "clientbp.ppmainecoonghj.com"
FALLBACK_LOGIN_URL = f"https://{LOGIN_HOST}"
FALLBACK_GAME_SERVER = f"https://{CLIENT_HOST}"

FALLBACK_RELEASE = "OB55"
FALLBACK_GAME_VERSION = "1.132.17"

MIN_BR_SEASON = 17
MIN_CS_SEASON = 5
CURRENT_SEASON = int(os.environ.get("FF_CURRENT_SEASON", "54"))
CURRENT_CS_SEASON = int(os.environ.get("FF_CURRENT_CS_SEASON", "40"))
LEADERBOARD_COUNT = 200

# Player-info request (GetPlayerPersonalShow). callSignSrc 7 = "viewing a profile", which is what the
# real game client sends. Set FF_INFO_CALLSIGN=0 to send only the UID like the old version did.
INFO_CALLSIGN = int(os.environ.get("FF_INFO_CALLSIGN", "7"))
INFO_UNITY_VERSION = os.environ.get("FF_INFO_UNITY_VERSION", "2018.4.12f1")

TIMEOUT_AUTH = 6
TIMEOUT_MAJOR = 8
TIMEOUT_PROFILE = 10
TIMEOUT_LB = 12
TIMEOUT_VERCONFIG = 6

CURRENT_TTL = 5 * 60
PAST_TTL = 24 * 60 * 60
EMPTY_TTL = 5 * 60           # empty result sirf 5 min yaad rakho
SESSION_TTL = 20 * 60
PROFILE_TTL = 60
VERSION_TTL = 60 * 60
LOGIN_COOLDOWN = 30          # first failure; doubles on every further failure (see _set_cooldown)
LOGIN_COOLDOWN_MAX = 600
MIN_LOGIN_GAP = 2            # gap between two logins of the SAME region
GLOBAL_LOGIN_GAP = float(os.environ.get("FF_LOGIN_GAP", "2.5"))   # gap between ANY two logins (burst guard)
LOGIN_ATTEMPTS = 2           # fresh token + MajorLogin tries before a region is put on cooldown

CDN_BASE = "https://cdn.jsdelivr.net/gh/ShahGCreator/icon@main/PNG/{}.png"

REGION_LANG = {
    "ME": "ar", "IND": "hi", "ID": "id", "VN": "vi", "TH": "th", "BD": "bn",
    "PK": "ur", "TW": "zh", "RU": "ru", "SAC": "es", "BR": "pt", "NA": "pt",
    "SG": "pt", "US": "pt", "EU": "en", "CIS": "ru",
}

# =====================================================================
# LEADERBOARD PAYLOAD REGION MAP
# EU   → SG   (EU served by Singapore cluster)
# CIS  → SG   (CIS served by Singapore cluster)
# Source: Free Fire Community API docs — "SG Region covers SG, ID, ME,
#         VN, TH, CIS, EU, TW, MY, PK, BD"
# =====================================================================
REGION_PAYLOAD_MAP = {
    "EU":  "SG",
    "CIS": "SG",
}

# IND ke alternate payload codes (India-only region)
REGION_ALT_PAYLOAD_CODES = {
    "IND": ["IND", "IN"],
}

# =====================================================================
# GAME SERVER CANDIDATES
# Sirf woh servers jo DNS resolve karte hain — dead URLs hata diye.
# =====================================================================
REGION_GAME_SERVER_CANDIDATES = {
    "IND": [
        "https://client.ind.freefiremobile.com",
        "https://clientbp.ppmainecoonghj.com",
    ],
    "NA":  ["https://client.us.freefiremobile.com",
            "https://clientbp.ppmainecoonghj.com"],
    "SAC": ["https://client.us.freefiremobile.com",
            "https://clientbp.ppmainecoonghj.com"],
    "US":  ["https://client.us.freefiremobile.com",
            "https://clientbp.ppmainecoonghj.com"],
    "BR":  ["https://client.us.freefiremobile.com",
            "https://clientbp.ppmainecoonghj.com"],
    "DEFAULT": ["https://clientbp.ppmainecoonghj.com"],
}

# Regions jinhe frontend pe disable kar sakte ho jab tak proper account na ho
REQUIRES_SPECIAL_ACCOUNT = {
    "IND": "India-cluster guest account (Free Fire India app) required",
}

app = Flask(__name__)

# ==================== HTTP SESSION ====================
_http = requests.Session()
_http.verify = False
_http.headers.update({"Connection": "keep-alive"})
_adapter = HTTPAdapter(
    pool_connections=30, pool_maxsize=80,
    max_retries=Retry(total=1, backoff_factor=0.4,
                      status_forcelist=[500, 502, 503, 504],
                      allowed_methods=["GET", "POST"]),
)
_http.mount("https://", _adapter)
_http.mount("http://", _adapter)

# ==================== PROTOBUF ====================
def encode_varint(n: int) -> bytes:
    out = bytearray()
    if n < 0: n &= 0xFFFFFFFFFFFFFFFF
    while True:
        b = n & 0x7F; n >>= 7
        if n: b |= 0x80
        out.append(b)
        if not n: break
    return bytes(out)

def _pb_varint(fn, val): return encode_varint((fn << 3) | 0) + encode_varint(val)

def _pb_length(fn, val):
    enc = val.encode('utf-8') if isinstance(val, str) else val
    return encode_varint((fn << 3) | 2) + encode_varint(len(enc)) + enc

def create_proto(fields) -> bytes:
    packet = bytearray()
    for field, value in fields.items():
        if isinstance(field, str): field = int(field)
        if isinstance(value, dict):
            packet.extend(_pb_length(field, bytes(create_proto(value))))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict): packet.extend(_pb_length(field, bytes(create_proto(item))))
                elif isinstance(item, str): packet.extend(_pb_length(field, item))
                elif isinstance(item, (bytes, bytearray)): packet.extend(_pb_length(field, bytes(item)))
                elif isinstance(item, bool): packet.extend(_pb_varint(field, 1 if item else 0))
                elif isinstance(item, int): packet.extend(_pb_varint(field, item))
        elif isinstance(value, bool): packet.extend(_pb_varint(field, 1 if value else 0))
        elif isinstance(value, int): packet.extend(_pb_varint(field, value))
        elif isinstance(value, str): packet.extend(_pb_length(field, value))
        elif isinstance(value, (bytes, bytearray)): packet.extend(_pb_length(field, bytes(value)))
    return bytes(packet)

def encrypt_aes(hex_data: str, key=AES_KEY, iv=AES_IV) -> str:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pad(bytes.fromhex(hex_data), AES.block_size)).hex()

def decode_varint(data, offset):
    result = 0; shift = 0
    while offset < len(data):
        byte = data[offset]
        result |= (byte & 0x7F) << shift
        offset += 1
        if not (byte & 0x80): return result, offset
        shift += 7
    return None, offset

def decode_protobuf(data):
    result = {}; offset = 0
    while offset < len(data):
        header, offset = decode_varint(data, offset)
        if header is None: break
        fn = header >> 3; wt = header & 0x7
        key = str(fn)
        if wt == 0:
            v, offset = decode_varint(data, offset)
            if v is not None:
                if key in result:
                    if not isinstance(result[key], list): result[key] = [result[key]]
                    result[key].append(v)
                else: result[key] = v
        elif wt == 2:
            length, offset = decode_varint(data, offset)
            if length is None: break
            value = data[offset:offset+length]; offset += length
            try:
                s = value.decode('utf-8')
                parsed = s if s else value.hex()
            except UnicodeDecodeError:
                nested = decode_protobuf(value)
                parsed = nested if nested else value.hex()
            if key in result:
                if not isinstance(result[key], list): result[key] = [result[key]]
                result[key].append(parsed)
            else: result[key] = parsed
        elif wt == 1: offset += 8
        elif wt == 5: offset += 4
        else: break
    return result

# ==================== VERSION CONFIG ====================
_version_cache = {}
_version_lock = threading.Lock()

def _fetch_app_version():
    if not _GPS_OK:
        return FALLBACK_GAME_VERSION
    try:
        return play_scraper('com.dts.freefireth').get('version') or FALLBACK_GAME_VERSION
    except Exception:
        return FALLBACK_GAME_VERSION

def version_config(region="BD", force=False):
    region = str(region or "BD").upper()
    now = time.time()
    if not force:
        with _version_lock:
            hit = _version_cache.get(region)
            if hit and now - hit[5] < VERSION_TTL:
                return hit

    app_version = _fetch_app_version()
    lang = REGION_LANG.get(region, "en")
    url = (f"https://version.ggwhitehawk.com/live/ver.php?version={app_version}"
           f"&lang={lang}&device=android&channel=android&appstore=googleplay"
           f"&region={region}&whitelist_version=1.3.0&whitelist_sp_version=1.0.0")
    try:
        r = _http.get(url, timeout=TIMEOUT_VERCONFIG)
        j = r.json()
        server_url = (j.get("server_url") or FALLBACK_LOGIN_URL).replace(
            "ggpolarbear.com", "ppmainecoonghj.com")
        remote_version = j.get("remote_version") or app_version
        release_version = j.get("latest_release_version") or FALLBACK_RELEASE
        gops = [u.strip() for u in (j.get("gop_url") or "").split(";") if u.strip()]
        gop_url = gops[0] if gops else "https://ffmconnect.ppmainecoonghj.com"

        result = (release_version, remote_version, server_url, gop_url, app_version, now)
        with _version_lock:
            _version_cache[region] = result
        log.info("[VER:%s] release=%s game=%s login=%s",
                 region, release_version, remote_version, server_url)
        return result
    except Exception as e:
        log.warning("[VER:%s] failed (%s) — fallback", region, e)
        result = (FALLBACK_RELEASE, FALLBACK_GAME_VERSION, FALLBACK_LOGIN_URL,
                  "https://ffmconnect.ppmainecoonghj.com", app_version, now)
        with _version_lock:
            _version_cache[region] = result
        return result

# ==================== AUTH ====================
def get_access_token(uid, password, region=""):
    url = "https://100067.connect.garena.com/oauth/guest/token/grant"
    hdrs = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "GarenaMSDK/4.0.44(SM-E135F ;Android 14;en;GB;)",
        "Connection": "Keep-Alive",
    }
    data = {"uid": str(uid), "password": password, "response_type": "token",
            "client_type": "2", "client_secret": HEX_KEY, "client_id": "100067"}
    why = ""
    for attempt in range(3):
        try:
            r = _http.post(url, headers=hdrs, data=data, timeout=TIMEOUT_AUTH)
            if r.status_code == 200:
                j = r.json()
                if j.get("open_id") and j.get("access_token"):
                    return j["open_id"], j["access_token"], j.get("platform", 4)
                why = f"HTTP 200 without token: keys={sorted(j)[:8]} error={str(j.get('error') or j.get('error_description') or '')[:80]}"
            else:
                why = f"HTTP {r.status_code} {r.text[:120]!r}"
        except Exception as e:
            why = f"exception {type(e).__name__}: {str(e)[:100]}"
            log.warning("get_access_token attempt %d: %s", attempt + 1, e)
        time.sleep(0.5 * (attempt + 1))
    log.warning("Token grant %s: %s", region, why)
    _login_errors[region] = f"guest token grant failed: {why}"
    return None

# The real game client puts its own public IP into the login request. A fixed foreign IP next to a
# datacenter connection looks fake, so use the server's real egress IP (cached). FF_EGRESS_IP overrides.
_egress = {"ip": None, "t": 0}
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
def _egress_ip():
    env = os.environ.get("FF_EGRESS_IP", "").strip()
    if _IP_RE.match(env): return env
    if _egress["ip"] and time.time() - _egress["t"] < 3600: return _egress["ip"]
    try:
        ip = requests.get("https://api.ipify.org", timeout=3).text.strip()
        if _IP_RE.match(ip):
            _egress.update(ip=ip, t=time.time())
            log.info("Egress IP = %s", ip)
            return ip
    except Exception as e:
        log.warning("egress ip lookup failed: %s", e)
    _egress.update(ip="104.28.197.151", t=time.time() - 3000)   # old constant, retry lookup in ~10 min
    return _egress["ip"]

def get_payload(access_token, open_id, platform, game_version, region="PK", lang_code=None):
    device_id = f"Google|{uuid.uuid4()}"
    reg_code = (region or "PK").upper()
    lang = lang_code or REGION_LANG.get(reg_code, "en")

    major_login = {
        "3": str(datetime.now())[:-7], "4": "free fire", "5": 1,
        "7": str(game_version),
        "8": "Android OS 14 / API-34 (UP1A.231005.007/E135FXXSEEZE2)",
        "9": "Handheld", "11": "CarrierDataNetwork",
        "12": 1672, "13": 750, "14": "338",
        "15": "ARMv7 VFPv3 NEON | 2002 | 8",
        "16": 3702, "17": "Mali-G52",
        "18": "OpenGL ES 3.2 v1.r38p1-01bet0-mbs2v41_0.6d20ec041e51b2f2d25dfc265586ebe8",
        "19": device_id, "20": _egress_ip(), "21": lang,
        "22": f"{open_id}", "23": f"{platform}", "24": "Handheld",
        "25": "samsung SM-E135F", "26": reg_code,
        "29": f"{access_token}", "30": 1, "42": "Cellular",
        "57": "7428b253defc164018c604a1ebbfebdf",
        "60": 52037, "61": 5355, "62": 3723, "63": 3,
        "64": 5483, "65": 52037, "66": 5483, "67": 52037,
        "73": 2,
        "74": "/data/app/~~yACsV8QOk9OexgajWQP30A==/com.dts.freefireth-Bi_wPVrrhYM2nYncc29q7Q==/lib/arm",
        "76": 1,
        "77": "b8e0cd5e295eee42f5860d3c86e483dd|/data/app/~~yACsV8QOk9OexgajWQP30A==/com.dts.freefireth-Bi_wPVrrhYM2nYncc29q7Q==/base.apk",
        "78": 3, "79": 1, "81": "32", "83": "2019121229",
        "86": "OpenGLES2", "87": 8191, "88": int(platform),
        "92": 16091, "93": "android",
        "94": "KqsHT3Yeqo3RLRl8efNiL3aL/1SHwlSrtK0DwNAqdkNBQz70TeVy+icaIwuR3UK4b65Uf3IG5xBFEv0uu+jQZI+rOYCmNCsXjJT4QcZaYgoaaUGG",
        "95": 111107,
        "96": "{\"cur_rate\":null,\"support_etc2\":false}",
        "97": 1, "98": 1,
        "99": f"{platform}", "100": f"{platform}",
        "102": "4503454457550c0766",
        "104": 53792, "105": 1,
        "106": "https://dl-bs.ggpolarbear.com/live/ABHotUpdates/|https://core-bs.ggpolarbear.com/live/ABHotUpdates/|6b2078db9d22dd98f8e9386a39af8462",
        "107": "c8e41b7a93f02d56e1a94c7b8203f5d1",
    }
    return bytes.fromhex(encrypt_aes(create_proto(major_login).hex()))

_login_errors = {}            # region -> last MajorLogin failure description (shown on /healthz)

def _describe_resp(content):
    """Short, secret-free description of a server reply, so a failed login can be diagnosed from logs."""
    try:
        d = decode_protobuf(content) or {}
    except Exception:
        d = {}
    parts = []
    for k, v in list(d.items())[:12]:
        if isinstance(v, int):
            parts.append(f"{k}={v}")
        elif isinstance(v, str) and len(v) <= 60 and not v.startswith("eyJ"):
            parts.append(f"{k}={v!r}")
        else:
            parts.append(f"{k}=<{type(v).__name__}>")
    txt = ""
    head = content[:80]
    if head and all(32 <= b < 127 or b in (9, 10, 13) for b in head):
        txt = f" text={head.decode('ascii')!r}"
    return f"len={len(content)} head={content[:20].hex()} fields[{', '.join(parts)}]{txt}"

def major_login(payload, login_url, release_version, region=""):
    url = f"{login_url.rstrip('/')}/MajorLogin"
    req_headers = {
        "Accept": "*/*", "Authorization": "Bearer ",
        "Content-Type": "application/x-www-form-urlencoded",
        "ReleaseVersion": f"{release_version}",
        "User-Agent": "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
        "X-GA": "v1 1", "X-GA-SV": str(int(time.time())),
        "X-Unity-Version": "2018.4.12f1",
    }
    try:
        r = _http.post(url, headers=req_headers, data=payload, timeout=TIMEOUT_MAJOR)
        if r.status_code != 200:
            why = f"HTTP {r.status_code} {_describe_resp(r.content)}"
            log.warning("MajorLogin %s: %s", region, why)
            _login_errors[region] = why
            return None
        content = r.content
        best = None; best_score = -1
        for offset in [0, 64, 48, 32, 80, 16, 128, 8, 96, 60, 56, 40, 24, 20, 12, 4]:
            if offset >= len(content): continue
            cand = decode_protobuf(content[offset:])
            if not cand: continue
            has_jwt = "8" in cand
            has_url = "10" in cand
            score = len(cand) + (100 if has_jwt else 0) + (50 if has_url else 0)
            if score > best_score:
                best = cand; best_score = score
            if has_jwt and has_url: break
        if best and "8" in best:
            _login_errors.pop(region, None)
            return {str(k): {"data": v} for k, v in best.items()}
        why = f"HTTP 200 but no JWT in reply: {_describe_resp(content)}"
        log.warning("MajorLogin %s: %s", region, why)
        _login_errors[region] = why
        return None
    except Exception as e:
        why = f"exception {type(e).__name__}: {str(e)[:150]}"
        log.warning("MajorLogin %s: %s", region, why)
        _login_errors[region] = why
        return None

# ==================== ACCOUNTS ====================
_accounts_cache = {"t": 0, "data": []}
_accounts_lock = threading.Lock()

def load_accounts():
    with _accounts_lock:
        if time.time() - _accounts_cache["t"] < 30 and _accounts_cache["data"]:
            return _accounts_cache["data"]
        try:
            raw_env = os.environ.get("FF_ACCOUNTS_JSON", "").strip()
            if raw_env:
                data = json.loads(raw_env)
            else:
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            if not isinstance(data, list): data = [data]
            _accounts_cache["data"] = data
            _accounts_cache["t"] = time.time()
            log.info("Loaded %d accounts", len(data))
            return data
        except Exception as e:
            log.error("accounts.json / FF_ACCOUNTS_JSON load failed: %s", e)
            return []

_account_rotor = {}
_rotor_lock = threading.Lock()

def next_account(region):
    region = str(region or "").upper()
    accounts = load_accounts()
    pool = [a for a in accounts if str(a.get("region", "")).upper() == region]
    if not pool:
        log.warning("No account for region %s", region)
        return None
    if len(pool) == 1:
        return pool[0]
    with _rotor_lock:
        idx = _account_rotor.get(region, 0) % len(pool)
        _account_rotor[region] = idx + 1
    return pool[idx]

def default_region():
    accounts = load_accounts()
    if not accounts: return "PK"
    for a in accounts:
        if str(a.get("region", "")).upper() == "PK":
            return "PK"
    return str(accounts[0].get("region", "PK")).upper()

# ==================== SESSION CACHE ====================
_session_cache = {}           # region -> (jwt, game_server_or_None, ts)
_session_lock = threading.Lock()
_auth_locks = {}
_auth_locks_guard = threading.Lock()

_login_cooldown = {}
_last_login_at = {}
_cooldown_lock = threading.Lock()

def _auth_lock(region):
    with _auth_locks_guard:
        return _auth_locks.setdefault(region, threading.Lock())

def _cooldown_state(region):
    with _cooldown_lock:
        now = time.time()
        cd_left = max(0, _login_cooldown.get(region, 0) - now)
        gap_left = max(0, MIN_LOGIN_GAP - (now - _last_login_at.get(region, 0)))
        return cd_left, gap_left

_login_fail_streak = {}
_global_login_lock = threading.Lock()
_global_last_login = [0.0]

def _set_cooldown(region):
    """Cooldown doubles with every failed login in a row (30s, 60s, 120s ... max 10 min)."""
    with _cooldown_lock:
        n = _login_fail_streak.get(region, 0)
        _login_fail_streak[region] = n + 1
        _login_cooldown[region] = time.time() + min(LOGIN_COOLDOWN * (2 ** n), LOGIN_COOLDOWN_MAX)

def _clear_fail_streak(region):
    with _cooldown_lock:
        _login_fail_streak.pop(region, None)
        _login_cooldown.pop(region, None)

def _global_login_gate():
    """All regions log in from ONE server IP. Never send a burst: keep a small gap between any two logins."""
    with _global_login_lock:
        wait = GLOBAL_LOGIN_GAP - (time.time() - _global_last_login[0])
        if wait > 0:
            time.sleep(wait)
        _global_last_login[0] = time.time()

def _mark_login(region):
    with _cooldown_lock:
        _last_login_at[region] = time.time()

def _extract_game_server(res, region=""):
    """JWT field 10 se game server lo — lekin region ke candidates hain to None."""
    v = res.get("10")
    if isinstance(v, dict) and "data" in v:
        url = v["data"]
        if isinstance(url, str) and url.startswith("http"):
            url = url.replace("ggpolarbear.com", "ppmainecoonghj.com").rstrip("/")
            if url == FALLBACK_GAME_SERVER and region in REGION_GAME_SERVER_CANDIDATES:
                return None
            return url
    if region in REGION_GAME_SERVER_CANDIDATES:
        return None
    return FALLBACK_GAME_SERVER

def get_session(region, force=False):
    region = str(region).upper()

    if not force:
        with _session_lock:
            hit = _session_cache.get(region)
            if hit and time.time() - hit[2] < SESSION_TTL:
                return hit[0], hit[1], None

    cd_left, _ = _cooldown_state(region)
    if cd_left > 0:
        with _session_lock:
            hit = _session_cache.get(region)
            if hit: return hit[0], hit[1], None
        return None, None, f"Login cooldown for {region} ({int(cd_left)}s)"

    with _auth_lock(region):
        if not force:
            with _session_lock:
                hit = _session_cache.get(region)
                if hit and time.time() - hit[2] < SESSION_TTL:
                    return hit[0], hit[1], None

        cd_left, gap_left = _cooldown_state(region)
        if cd_left > 0:
            with _session_lock:
                hit = _session_cache.get(region)
                if hit: return hit[0], hit[1], None
            return None, None, f"Login cooldown for {region} ({int(cd_left)}s)"

        if gap_left > 0:
            time.sleep(gap_left)

        acc = next_account(region)
        if not acc:
            return None, None, f"No account for region {region}"

        release_v, game_v, login_url, gop_url, app_ver, _ = version_config(region)
        log.info("Login: %s uid=%s release=%s game=%s",
                 region, acc["uid"], release_v, game_v)
        _mark_login(region)

        res = None
        token_ok = False
        for attempt in range(1, LOGIN_ATTEMPTS + 1):
            _global_login_gate()
            tg = get_access_token(acc["uid"], acc["password"], region=region)
            if not tg:
                log.warning("get_access_token FAILED for %s uid=%s (try %d/%d)",
                            region, acc["uid"], attempt, LOGIN_ATTEMPTS)
            else:
                token_ok = True
                open_id, access_token, platform = tg
                res = major_login(
                    get_payload(access_token, open_id, platform, game_v, region=region),
                    login_url, release_v, region=region
                )
                if res and "8" in res:
                    break
                log.warning("MajorLogin FAILED for %s uid=%s (try %d/%d)",
                            region, acc["uid"], attempt, LOGIN_ATTEMPTS)
            if token_ok and sum(1 for a in load_accounts()
                                if str(a.get("region", "")).upper() == region) < 2:
                break          # Garena answered, just not with a JWT: repeating the same request only adds load
            if attempt < LOGIN_ATTEMPTS:
                time.sleep(3 * attempt)
                # try the next account of this region, if there is one
                acc = next_account(region) or acc
        if not res or "8" not in res:
            _set_cooldown(region)
            return None, None, "MajorLogin failed" if token_ok else "Auth failed"

        jwt = res["8"]["data"] if isinstance(res["8"], dict) else res["8"]
        game_server = _extract_game_server(res, region)

        _clear_fail_streak(region)
        with _session_lock:
            _session_cache[region] = (jwt, game_server, time.time())
        log.info("✅ Session: %s uid=%s game_server=%s",
                 region, acc["uid"], game_server)
        return jwt, game_server, None

# ==================== PLAYER INFO (GetPlayerPersonalShow) ====================
# Layout taken from the FreeFire-Api project's PlayerPersonalShow.proto, written out by hand so no
# protobuf library is needed. Each entry is: field number -> (name, type).
#   int / bool / str     plain values
#   rep_int              list of numbers (packed or not)
#   msg:<Name>           nested message      rep_msg:<Name>   list of nested messages
_PS = {
    "Response": {
        1: ("basic", "msg:Basic"), 2: ("profile", "msg:Profile"), 3: ("leaderboard_pos", "int"),
        6: ("clan", "msg:Clan"), 7: ("captain", "msg:Basic"), 8: ("pet", "msg:Pet"),
        9: ("social", "msg:Social"), 10: ("diamond", "msg:Diamond"), 11: ("credit", "msg:Credit"),
    },
    "Basic": {
        1: ("account_id", "int"), 3: ("nickname", "str"), 5: ("region", "str"),
        6: ("level", "int"), 7: ("exp", "int"),
        11: ("banner_id", "int"), 12: ("head_pic", "int"), 13: ("clan_name", "str"),
        14: ("rank", "int"), 15: ("ranking_points", "int"), 17: ("has_elite_pass", "bool"),
        18: ("badge_count", "int"), 19: ("badge_id", "int"), 20: ("season_id", "int"),
        21: ("liked", "int"), 24: ("last_login_at", "int"),
        30: ("cs_rank", "int"), 31: ("cs_ranking_points", "int"), 32: ("weapon_skins", "rep_int"),
        35: ("max_rank", "int"), 36: ("cs_max_rank", "int"), 37: ("max_ranking_points", "int"),
        39: ("peak_rank_pos", "int"), 40: ("cs_peak_rank_pos", "int"),
        44: ("created_at", "int"), 46: ("selected_item_slots", "rep_int"),
        48: ("title", "int"), 50: ("release_version", "str"),
        54: ("clan_id", "int"), 55: ("clan_badge_id", "int"),
        74: ("prime", "msg:Prime"), 75: ("cs_peak_points", "int"), 78: ("avatar_frame", "int"),
        82: ("social", "msg:Social"),
    },
    "Prime": {2: ("prime_level", "int")},
    "Profile": {
        1: ("avatar_id", "int"), 3: ("clothes", "rep_int"), 4: ("equipped_skills", "rep_int"),
        6: ("pve_primary_weapon", "int"),
    },
    "Clan": {
        1: ("id", "int"), 2: ("name", "str"), 3: ("captain_id", "int"), 4: ("level", "int"),
        5: ("capacity", "int"), 6: ("members", "int"), 7: ("honor_point", "int"),
    },
    "Pet": {
        1: ("id", "int"), 2: ("name", "str"), 3: ("level", "int"), 4: ("exp", "int"),
        6: ("skin_id", "int"), 9: ("selected_skill_id", "int"),
    },
    "Social": {
        2: ("gender", "int"), 3: ("language", "int"), 4: ("time_online", "int"), 5: ("time_active", "int"),
        6: ("battle_tags", "rep_int"), 7: ("social_tags", "rep_int"), 8: ("mode_prefer", "int"),
        9: ("signature", "str"), 10: ("rank_show", "int"),
    },
    "SpecialFriend": {1: ("duo_info", "msg:Duo")},
    "Duo": {1: ("partner_uid", "int"), 3: ("score", "int"), 4: ("creation_timestamp", "int"),
            5: ("days_active", "int"), 6: ("status", "int")},
    "Diamond": {1: ("cost", "int")},
    "Credit": {1: ("score", "int"), 2: ("is_init", "bool")},
}

_GENDER = {1: "Male", 2: "Female"}
_LANGUAGE = {1: "English", 2: "Chinese (Simplified)", 3: "Chinese (Traditional)", 4: "Thai", 5: "Vietnamese",
             6: "Indonesian", 7: "Portuguese", 8: "Spanish", 9: "Russian", 10: "Korean", 11: "French",
             12: "German", 13: "Turkish", 14: "Hindi", 15: "Japanese", 16: "Romanian", 17: "Arabic",
             18: "Burmese", 19: "Urdu", 20: "Bengali", 21: "Malay"}
_TIME_ONLINE = {1: "Online on weekdays", 2: "Online on weekends"}
_TIME_ACTIVE = {1: "Active in the morning", 2: "Active in the afternoon", 3: "Active at night"}
_MODE_PREFER = {1: "Battle Royale", 2: "Clash Squad", 3: "Entertainment"}
_BATTLE_TAGS = {1101: "Domination", 1102: "Uncrown", 1103: "Best partner", 1104: "Sniper", 1105: "Melee",
                1106: "Peacemaker", 1107: "Ambush", 1108: "Shortstop", 1109: "Rampage", 1110: "Leader"}
_SOCIAL_TAGS = {2101: "Fashion", 2102: "Social", 2103: "Veteran", 2104: "Newbie", 2105: "Plays for the win",
                2106: "Plays for fun", 2107: "Voice on", 2108: "Voice off"}

def _walk_fields(data):
    """Yield (field_number, wire_type, value) from raw protobuf bytes."""
    offset = 0
    while offset < len(data):
        header, offset = decode_varint(data, offset)
        if header is None: return
        fn, wt = header >> 3, header & 0x7
        if wt == 0:
            val, offset = decode_varint(data, offset)
            if val is None: return
            yield fn, wt, val
        elif wt == 2:
            length, offset = decode_varint(data, offset)
            if length is None or offset + length > len(data): return
            yield fn, wt, data[offset:offset + length]
            offset += length
        elif wt == 1: offset += 8
        elif wt == 5: offset += 4
        else: return

def _pb_to_dict(data, msg_name):
    """Decode bytes into a dict using the _PS schema. Unknown fields are ignored."""
    schema = _PS[msg_name]
    out = {}
    for fn, wt, val in _walk_fields(data):
        spec = schema.get(fn)
        if not spec: continue
        name, kind = spec
        if kind == "int" and wt == 0:
            out[name] = val - (1 << 64) if val >= (1 << 63) else val
        elif kind == "bool" and wt == 0:
            out[name] = bool(val)
        elif kind == "str" and wt == 2:
            out[name] = val.decode("utf-8", "replace")
        elif kind == "rep_int":
            lst = out.setdefault(name, [])
            if wt == 0:
                lst.append(val)
            elif wt == 2:                      # packed list
                pos = 0
                while pos < len(val):
                    num, pos = decode_varint(val, pos)
                    if num is None: break
                    lst.append(num)
        elif kind.startswith("msg:") and wt == 2:
            out[name] = _pb_to_dict(val, kind[4:])
    return out

def _fmt_ts(ts, with_time=False):
    try:
        ts = int(ts or 0)
        if ts <= 0: return ""
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""

def _clean_item_ids(ids):
    out = []
    for i in ids or []:
        if 100000000 <= i <= 999999999 and i not in out:
            out.append(i)
    return out

def parse_player_info(raw):
    """Raw GetPlayerPersonalShow bytes -> readable dict (None if the response is not a player)."""
    info = _pb_to_dict(raw, "Response")
    basic = info.get("basic") or {}
    if not basic.get("nickname") and not basic.get("account_id"):
        return None
    return info

def build_profile(info, target_uid, region):
    b = info.get("basic", {})
    prof = info.get("profile", {})
    clan = info.get("clan", {})
    social = info.get("social") or b.get("social") or {}
    pet = info.get("pet", {})
    credit = info.get("credit", {})

    # Same avatar/banner handling as before, so pictures look exactly as they did.
    avatar_id = b.get("banner_id") or 102000007
    banner_id = b.get("head_pic") or 902000172
    if str(avatar_id).startswith("902"):
        avatar_id, banner_id = banner_id, avatar_id

    clan_name = clan.get("name") or b.get("clan_name") or "No Clan"
    out = {
        "nickname": b.get("nickname", "Unknown"), "uid": target_uid,
        "region": region.upper(), "server_region": b.get("region", ""),
        "level": b.get("level", 0), "exp": b.get("exp", 0),
        "br_rank": b.get("rank", 0), "br_points": b.get("ranking_points", 0),
        "cs_rank": b.get("cs_rank", 0), "cs_points": b.get("cs_ranking_points", 0),
        "max_rank": b.get("max_rank", 0), "cs_max_rank": b.get("cs_max_rank", 0),
        "max_points": b.get("max_ranking_points", 0), "cs_peak_points": b.get("cs_peak_points", 0),
        "likes": b.get("liked", 0),
        "created_at": _fmt_ts(b.get("created_at")) or "Unknown",
        "last_login": _fmt_ts(b.get("last_login_at"), with_time=True),
        "avatar_id": avatar_id, "banner_id": banner_id,
        "clan_name": clan_name, "clan_id": clan.get("id") or b.get("clan_id") or "",
        "title": b.get("title", 0), "avatar_frame": b.get("avatar_frame", 0),
        "badge_count": b.get("badge_count", 0), "has_elite_pass": bool(b.get("has_elite_pass")),
        "prime_level": (b.get("prime") or {}).get("prime_level", 0),
        "release_version": b.get("release_version", ""),
        "leaderboard_pos": info.get("leaderboard_pos", 0),
        "credit_score": credit.get("score", 0),
        "diamond_cost": (info.get("diamond") or {}).get("cost", 0),
        "signature": social.get("signature", ""),
        "gender": _GENDER.get(social.get("gender"), ""),
        "language": _LANGUAGE.get(social.get("language"), ""),
        "mode_prefer": _MODE_PREFER.get(social.get("mode_prefer"), ""),
        "time_online": _TIME_ONLINE.get(social.get("time_online"), ""),
        "time_active": _TIME_ACTIVE.get(social.get("time_active"), ""),
        "battle_tags": [_BATTLE_TAGS[t] for t in social.get("battle_tags", []) if t in _BATTLE_TAGS],
        "social_tags": [_SOCIAL_TAGS[t] for t in social.get("social_tags", []) if t in _SOCIAL_TAGS],
        "outfit_ids": _clean_item_ids(prof.get("clothes")),
        "weapon_skin_ids": _clean_item_ids(b.get("weapon_skins")),
        "skill_ids": prof.get("equipped_skills", []),
    }
    if clan:
        out["clan"] = {"level": clan.get("level", 0), "members": clan.get("members", 0),
                       "capacity": clan.get("capacity", 0), "honor": clan.get("honor_point", 0),
                       "captain_id": clan.get("captain_id", "")}
    if pet.get("name") or pet.get("id"):
        out["pet"] = {"name": pet.get("name", ""), "level": pet.get("level", 0), "exp": pet.get("exp", 0)}
    return out

def search_player_profile(target_uid, region, jwt_token, game_server):
    if not game_server:
        game_server = FALLBACK_GAME_SERVER
    url = f"{game_server.rstrip('/')}/GetPlayerPersonalShow"
    release_v = version_config(region)[0]
    headers = {
        "User-Agent": f"UnityPlayer/{INFO_UNITY_VERSION} (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
        "Accept": "*/*", "Accept-Encoding": "deflate, gzip",
        "X-GA-SV": str(int(time.time())),
        "Authorization": f"Bearer {jwt_token}",
        "X-GA": "v1 1", "ReleaseVersion": release_v,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Unity-Version": INFO_UNITY_VERSION,
    }
    bodies = []
    if INFO_CALLSIGN:
        bodies.append({"1": int(target_uid), "2": INFO_CALLSIGN})
    bodies.append({"1": int(target_uid)})          # old style request, used if the new one is refused
    try:
        r = None
        for fields in bodies:
            payload = bytes.fromhex(encrypt_aes(create_proto(fields).hex()))
            r = _http.post(url, headers=headers, data=payload, timeout=TIMEOUT_PROFILE)
            if r.status_code == 200:
                break
        if r.status_code != 200:
            return {"error": f"API error: HTTP {r.status_code}"}
        info = parse_player_info(r.content)
        if not info:
            return {"error": "Player not found in this region"}
        return build_profile(info, target_uid, region)
    except Exception as e:
        return {"error": str(e)}

# ==================== PROFILE CACHE ====================
_profile_cache = {}
_profile_cache_lock = threading.Lock()

def lookup_profile(uid, region):
    uid = str(uid).strip()
    if not uid.isdigit(): return {"error": "UID must be numeric"}
    jwt, game_server, err = get_session(region)
    if err: return {"error": err}
    details = search_player_profile(uid, region, jwt, game_server)
    if "error" in details and any(c in details["error"] for c in ("401", "403")):
        jwt, game_server, err = get_session(region, force=True)
        if err: return {"error": err}
        details = search_player_profile(uid, region, jwt, game_server)
    return details

def lookup_profile_cached(uid, region):
    key = (str(uid), str(region).upper())
    now = time.time()
    with _profile_cache_lock:
        hit = _profile_cache.get(key)
        if hit and now - hit[1] < PROFILE_TTL:
            return hit[0]
    result = lookup_profile(uid, region)
    if "error" not in result:
        with _profile_cache_lock:
            _profile_cache[key] = (result, time.time())
    return result

# ==================== LEADERBOARD ====================
def clean_name(name):
    if not isinstance(name, str): return "Unknown"
    cleaned = re.sub(r'[\u3164\u3163\u1160\u115f\u0000-\u001F\u007F-\u009F]', '', name)
    return cleaned.strip() if cleaned.strip() else "Unknown"

def season_payload(season, region="PK", mode="BR", count=LEADERBOARD_COUNT,
                   payload_region=None):
    """payload_region overrides region (EU→SG, CIS→SG, IND→IN etc)."""
    mode = (mode or "BR").upper()
    reg = payload_region or REGION_PAYLOAD_MAP.get(region.upper(), region.upper())
    if mode == "CS":
        fields = {"1": 13, "2": 15, "3": int(season), "6": int(count),
                  "7": 1, "8": reg}
    else:
        fields = {"1": 1, "3": int(season), "6": int(count),
                  "7": 1, "8": reg}
    return bytes.fromhex(encrypt_aes(create_proto(fields).hex()))

def _parse_leaderboard(raw_bytes, mode="BR"):
    mode = (mode or "BR").upper()
    raw_data = decode_protobuf(raw_bytes)
    entries = raw_data.get("1", [])
    if isinstance(entries, dict): entries = [entries]
    formatted_players = []
    if isinstance(entries, list):
        for p in entries:
            try:
                uid = p.get("1")
                if uid is None: continue
                rank = p.get("4", 0)
                inner = p.get("3", {})
                if not isinstance(inner, dict): continue
                profile = inner.get("1", {})
                if not isinstance(profile, dict): profile = {}

                name = profile.get("3", "Unknown")
                level = profile.get("6", 0)
                stats_str = (inner.get("2", {}).get("5", "{}")
                             if isinstance(inner.get("2"), dict) else "{}")
                try:
                    stats_dict = json.loads(stats_str) if isinstance(stats_str, str) else {}
                except Exception:
                    stats_dict = {}

                if mode == "CS":
                    points = profile.get("31")
                    if not points: points = inner.get("4")
                    if not points and "rating_points" in stats_dict:
                        try: points = int(stats_dict["rating_points"])
                        except Exception: points = 0
                    if not points:
                        points = (inner.get("2", {}).get("4", 0)
                                  if isinstance(inner.get("2"), dict) else 0)
                    points = int(points or 0)
                else:
                    points = int(inner.get("4", 0) or 0)

                avatar_id = profile.get("11", 0)
                banner_id = profile.get("12", 0)
                if str(avatar_id).startswith("902"):
                    avatar_id, banner_id = banner_id, avatar_id

                formatted_players.append({
                    "rank": rank, "name": clean_name(name), "uid": uid,
                    "points": points, "level": level,
                    "avatar_url": CDN_BASE.format(avatar_id) if avatar_id else "",
                    "banner_url": CDN_BASE.format(banner_id) if banner_id else "",
                    "stats": stats_dict,
                })
            except Exception:
                continue
    return formatted_players

def _try_server_for_lb(server_url, jwt, body, region, mode, season, release_v):
    headers = {
        "User-Agent": "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
        "Accept": "*/*", "Accept-Encoding": "deflate, gzip",
        "X-GA-SV": str(int(time.time())),
        "Authorization": f"Bearer {jwt}",
        "X-GA": "v1 1", "ReleaseVersion": release_v,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Unity-Version": "2018.4.12f1",
    }
    last_err = None
    for attempt in range(2):
        try:
            r = _http.post(f"{server_url}/Leaderboard",
                           headers=headers, data=body, timeout=TIMEOUT_LB)
            log.info("LB %s %s S%d [%s] → HTTP %d",
                     region, mode, season, server_url, r.status_code)
            if r.status_code != 200:
                return r.status_code, []
            players = _parse_leaderboard(r.content, mode=mode)
            return r.status_code, players
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as e:
            last_err = e
            if attempt == 0:
                log.warning("LB conn reset [%s %s %s] — retrying: %s",
                            region, mode, server_url, str(e)[:120])
                time.sleep(0.4)
                continue
        except Exception as e:
            log.warning("LB exception [%s %s %s]: %s", region, mode, server_url, e)
            return None, []
    log.warning("LB gave up [%s %s %s]: %s", region, mode, server_url, last_err)
    return None, []

def fetch_and_format_data(season=None, region=None, mode="BR"):
    mode = (mode or "BR").upper()
    cur_season = CURRENT_CS_SEASON if mode == "CS" else CURRENT_SEASON
    season = int(season or cur_season)
    region = str(region or default_region()).upper()

    release_v = version_config(region)[0]
    jwt, cached_server, err = get_session(region)
    if err:
        return {"status": "error", "error": err}

    candidates = list(REGION_GAME_SERVER_CANDIDATES.get(
        region, REGION_GAME_SERVER_CANDIDATES["DEFAULT"]))
    if cached_server and cached_server not in candidates:
        candidates.insert(0, cached_server)

    payload_codes = REGION_ALT_PAYLOAD_CODES.get(region, [None])

    best_players = []
    best_server = None
    last_status = None
    attempts_log = []

    for pc in payload_codes:
        body = season_payload(season, region, mode=mode, payload_region=pc)
        effective_code = pc or REGION_PAYLOAD_MAP.get(region, region)
        for server_url in candidates:
            status, players = _try_server_for_lb(
                server_url, jwt, body, region, mode, season, release_v)
            last_status = status
            attempts_log.append(f"{effective_code}@{server_url}={status}")

            if status in (401, 403):
                log.warning("JWT rejected for %s %s @ %s, refreshing...",
                            region, mode, server_url)
                jwt, cached_server, err = get_session(region, force=True)
                if err:
                    return {"status": "error", "error": err}
                status, players = _try_server_for_lb(
                    server_url, jwt, body, region, mode, season, release_v)
                last_status = status
                attempts_log.append(f"{effective_code}@{server_url}={status}(retry)")

            if status == 200 and players:
                log.info("✅ LB OK: %s %s S%d → %d players [%s] (payload=%s)",
                         region, mode, season, len(players), server_url, effective_code)
                best_players = players
                best_server = server_url
                with _session_lock:
                    old = _session_cache.get(region)
                    if old:
                        _session_cache[region] = (old[0], server_url, old[2])
                break

            if status == 200 and not players:
                log.warning("LB %s %s S%d → empty @ %s (payload=%s)",
                            region, mode, season, server_url, effective_code)

        if best_players:
            break

    if not best_players:
        hint = REQUIRES_SPECIAL_ACCOUNT.get(region)
        if last_status == 200:
            err_msg = (f"No leaderboard data for {mode} season {season} ({region}).")
            if hint:
                err_msg += f" {hint}."
            else:
                err_msg += " The account may be on a different server cluster."
            return {"status": "error", "empty": True, "error": err_msg,
                    "attempts": attempts_log}
        return {"status": "error",
                "error": f"LB failed: HTTP {last_status}",
                "attempts": attempts_log}

    return {"status": "success", "mode": mode, "season": season, "region": region,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "players": best_players, "server": best_server}

# ==================== LB CACHE ====================
_lb_cache = {}
_lb_empty = {}
_lb_locks = {}
_lb_guard = threading.Lock()

def _is_fresh(entry, season, mode="BR"):
    cur = CURRENT_CS_SEASON if mode == "CS" else CURRENT_SEASON
    ttl = CURRENT_TTL if season == cur else PAST_TTL
    return bool(entry) and time.time() - entry[1] < ttl

_region_last_req = {}     # region -> last time a visitor asked for it

def get_leaderboard(season, region, mode="BR", force=False):
    mode = (mode or "BR").upper()
    if not force:
        _region_last_req[region] = time.time()
    key = (mode, region, season)
    with _lb_guard:
        hit = _lb_cache.get(key)
        bad = _lb_empty.get(key)
        lock = _lb_locks.setdefault(key, threading.Lock())
    if not force:
        if _is_fresh(hit, season, mode=mode): return hit[0]
        if bad and time.time() - bad[1] < EMPTY_TTL: return bad[0]
    with lock:
        with _lb_guard:
            hit = _lb_cache.get(key)
        if not force and _is_fresh(hit, season, mode=mode):
            return hit[0]
        data = fetch_and_format_data(season, region, mode=mode)
        with _lb_guard:
            if data.get("status") == "success":
                _lb_cache[key] = (data, time.time())
                _lb_empty.pop(key, None)
            elif data.get("empty"):
                _lb_empty[key] = (data, time.time())
        if data.get("status") != "success" and hit:
            return hit[0]
        return data

# ==================== CLEANUP ====================
def _cache_cleanup():
    while True:
        time.sleep(1800)
        now = time.time()
        with _lb_guard:
            for k in list(_lb_cache):
                _, saved = _lb_cache[k]
                mode, region, season = k
                cur = CURRENT_CS_SEASON if mode == "CS" else CURRENT_SEASON
                ttl = CURRENT_TTL if season == cur else PAST_TTL
                if now - saved > ttl * 2:
                    _lb_cache.pop(k, None)
            for k in list(_lb_empty):
                _, saved = _lb_empty[k]
                if now - saved > EMPTY_TTL * 2:
                    _lb_empty.pop(k, None)
        with _session_lock:
            for r, (_, _, saved) in list(_session_cache.items()):
                if now - saved > SESSION_TTL * 3:
                    _session_cache.pop(r, None)
        with _profile_cache_lock:
            for k, (_, saved) in list(_profile_cache.items()):
                if now - saved > PROFILE_TTL * 2:
                    _profile_cache.pop(k, None)
        with _cooldown_lock:
            for r, until in list(_login_cooldown.items()):
                if until < now:
                    _login_cooldown.pop(r, None)
        with _version_lock:
            for r, entry in list(_version_cache.items()):
                if now - entry[5] > VERSION_TTL * 2:
                    _version_cache.pop(r, None)

# ==================== WEB ====================
_updater_error = None
REGION_RE = re.compile(r"^[A-Za-z]{2,4}$")

def _is_login_error(msg):
    m = str(msg or "")
    return m.startswith(("MajorLogin failed", "Auth failed", "Login cooldown", "No account"))

def _warm_region(region):
    out = []
    for mode, cur in [("BR", CURRENT_SEASON), ("CS", CURRENT_CS_SEASON)]:
        try:
            data = get_leaderboard(cur, region, mode=mode, force=True)
            ok = data.get("status") == "success"
            info = len(data.get("players", [])) if ok else data.get("error")
            out.append((mode, cur, region, ok, info, data.get("server", "")))
            # Login itself is broken -> trying the second mode would only repeat the same failure.
            if not ok and _is_login_error(info):
                out.append(("CS" if mode == "BR" else mode, CURRENT_CS_SEASON, region, False, info, ""))
                break
        except Exception as e:
            out.append((mode, cur, region, False, str(e), ""))
    return out

_region_next_run = {}     # region -> when the updater should try it again
_region_fail_streak = {}

WARM_MODE = os.environ.get("FF_WARM", "auto").strip().upper()   # auto | all | PK,BD,...
WARM_IDLE = 6 * 3600

def _warm_targets():
    """Logging in to all 14 regions at every boot is a burst Garena throttles. By default only the
    default region and regions a visitor opened in the last 6 h are kept warm; the rest log in on demand."""
    have = {str(a.get("region", "PK")).upper() for a in load_accounts()}
    if WARM_MODE == "ALL":
        return have
    if WARM_MODE not in ("", "AUTO"):
        return {r.strip() for r in WARM_MODE.split(",") if r.strip()} & have
    now = time.time()
    out = {default_region()} & have
    out |= {r for r, t in _region_last_req.items() if now - t < WARM_IDLE and r in have}
    return out

def _region_order(regions):
    """Default region (the one visitors see first) is warmed first."""
    first = default_region()
    return sorted(regions, key=lambda r: (r != first, r))

def background_updater():
    """Every region is refreshed every CURRENT_TTL. A region that fails is retried with a growing delay
    (1, 2, 4 ... 15 min) instead of hammering Garena, and one broken region never blocks the others."""
    global _updater_error
    time.sleep(3)
    while True:
        now = time.time()
        regions = [r for r in _region_order(_warm_targets())
                   if _region_next_run.get(r, 0) <= now]
        if regions:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(_warm_region, r): r for r in regions}
                for fut in as_completed(futures):
                    region = futures[fut]
                    results = fut.result()
                    all_ok = True
                    for mode, cur, reg, ok, info, server in results:
                        if ok:
                            _updater_error = None
                            log.info("✅ Updater OK: %s S%d (%s) → %d players [%s]",
                                     mode, cur, reg, info, server)
                        else:
                            all_ok = False
                            _updater_error = info
                            log.warning("Updater FAIL: %s S%d (%s) → %s", mode, cur, reg, info)
                    if all_ok:
                        _region_fail_streak.pop(region, None)
                        _region_next_run[region] = time.time() + CURRENT_TTL
                    else:
                        n = _region_fail_streak.get(region, 0) + 1
                        _region_fail_streak[region] = n
                        delay = min(60 * (2 ** (n - 1)), 900)
                        _region_next_run[region] = time.time() + delay
                        log.info("Updater: %s retry in %ds (failed %d in a row)", region, delay, n)
        time.sleep(15)

@app.route("/")
def home():
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

def _accounts_per_region():
    out = {}
    for a in load_accounts():
        r = str(a.get("region", "")).upper()
        out[r] = out.get(r, 0) + 1
    return out

@app.route("/healthz")
def healthz():
    return jsonify({
        "ok": True,
        "domains": {"login": LOGIN_HOST, "client": CLIENT_HOST},
        "updater_error": _updater_error,
        "sessions": {r: {"game_server": v[1], "age": int(time.time() - v[2])}
                     for r, v in _session_cache.items()},
        "cooldowns": {r: int(t - time.time())
                      for r, t in _login_cooldown.items() if t > time.time()},
        "login_errors": dict(_login_errors),
        "accounts_per_region": _accounts_per_region(),
        "requires_special_account": REQUIRES_SPECIAL_ACCOUNT,
    })

@app.route("/api/stats")
def api_stats():
    return jsonify({
        "ok": True,
        "regions": sorted({str(a.get("region", "PK")).upper() for a in load_accounts()}),
        "lb_cache": len(_lb_cache),
        "sessions": len(_session_cache),
        "cooldowns": {r: int(t - time.time())
                      for r, t in _login_cooldown.items() if t > time.time()},
    })

@app.route("/api/leaderboard")
def api_leaderboard():
    mode = request.args.get("mode", "BR").upper()
    if mode not in ("BR", "CS"): mode = "BR"
    cur_season = CURRENT_CS_SEASON if mode == "CS" else CURRENT_SEASON
    min_season = MIN_CS_SEASON if mode == "CS" else MIN_BR_SEASON
    season = request.args.get("season", type=int) or cur_season
    region = (request.args.get("region") or default_region()).upper()
    base = {
        "mode": mode, "current_season": cur_season, "min_season": min_season,
        "min_br_season": MIN_BR_SEASON, "min_cs_season": MIN_CS_SEASON,
        "current_br_season": CURRENT_SEASON, "current_cs_season": CURRENT_CS_SEASON,
        "season": season, "region": region,
    }
    if not min_season <= season <= cur_season:
        return jsonify({**base, "status": "error",
                        "error": f"Season must be between {min_season} and {cur_season}",
                        "players": []}), 400
    if not REGION_RE.match(region):
        return jsonify({**base, "status": "error",
                        "error": "Invalid region", "players": []}), 400

    data = get_leaderboard(season, region, mode=mode)
    return jsonify({**data, **base, "players": data.get("players", [])})

# ==================== DYNAMIC DUO ====================
# Everything here uses the project's own accounts and sessions. Nothing is sent to a third-party API,
# and no account is hard-coded: the login for the player's region comes from accounts.json.
DUO_TTL = 2 * 60
_duo_cache = {}
_duo_cache_lock = threading.Lock()

def _duo_level(score):
    for limit, level in ((101, 1), (301, 2), (501, 3), (801, 4), (1201, 5)):
        if score < limit:
            return level
    return 6

def parse_duo(raw):
    """Response of GetSpecialFriendList. It is usually AES-encrypted; plain bytes are accepted too."""
    candidates = []
    if raw and len(raw) % 16 == 0:
        try:
            candidates.append(unpad(AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(raw), 16))
        except Exception:
            pass
    candidates.append(raw)
    for data in candidates:
        try:
            duo = _pb_to_dict(data, "SpecialFriend").get("duo_info")
        except Exception:
            continue
        if duo and duo.get("partner_uid"):
            return duo
    return None

def _post_duo(uid, region, force=False):
    jwt, game_server, err = get_session(region, force=force)
    if err:
        return None, err
    url = f"{(game_server or FALLBACK_GAME_SERVER).rstrip('/')}/GetSpecialFriendList"
    body = bytes.fromhex(encrypt_aes(create_proto({"1": int(uid)}).hex()))
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11)",
        "X-GA": "v1 1",
        "ReleaseVersion": version_config(region)[0],      # always the current OB release
        "Connection": "Keep-Alive",
    }
    try:
        return _http.post(url, headers=headers, data=body, timeout=TIMEOUT_PROFILE), None
    except Exception as e:
        return None, f"Duo request failed: {e}"

def lookup_duo(uid, region):
    uid = str(uid).strip()
    r, err = _post_duo(uid, region)
    if r is not None and r.status_code in (401, 403):
        r, err = _post_duo(uid, region, force=True)
    if err:
        return {"error": err}
    if r.status_code == 500:
        return {"error": "Server error 500: invalid UID or private profile"}
    if r.status_code != 200:
        return {"error": f"Duo request failed: HTTP {r.status_code}"}

    duo = parse_duo(r.content)
    if not duo:
        return {"has_duo": False, "uid": uid}

    score = int(duo.get("score", 0))
    partner_uid = str(duo["partner_uid"])
    out = {
        "has_duo": True, "uid": uid, "partner_uid": partner_uid,
        "score": score, "level": _duo_level(score),
        "days_active": int(duo.get("days_active", 0)),
        "created_at": _fmt_ts(duo.get("creation_timestamp"), with_time=True),
        "status": "Active" if duo.get("status") == 2 else "Inactive",
    }
    # Partner name and pictures come from our own player lookup (same region first, then the others).
    try:
        partner = lookup_profile_auto(partner_uid, hint=region)
    except Exception as e:
        partner = {"error": str(e)}
    if _profile_ok(partner):
        out.update({
            "partner_nickname": partner.get("nickname", ""),
            "partner_region": partner.get("region", ""),
            "partner_level": partner.get("level", 0),
            "partner_avatar_id": partner.get("avatar_id"),
            "partner_banner_id": partner.get("banner_id"),
        })
    else:
        out["partner_nickname"] = ""
    return out

def lookup_duo_cached(uid, region):
    key = (str(uid), str(region).upper())
    now = time.time()
    with _duo_cache_lock:
        hit = _duo_cache.get(key)
        if hit and now - hit[1] < DUO_TTL:
            return hit[0]
    result = lookup_duo(uid, region)
    if "error" not in result:
        with _duo_cache_lock:
            _duo_cache[key] = (result, time.time())
    return result

# ==================== REGION AUTO-DETECT ====================
REGION_HINT_TTL = 60 * 60
_region_hint = {}            # uid -> (region, saved_at)
_region_hint_lock = threading.Lock()

def _account_regions():
    return sorted({str(a.get("region", "PK")).upper() for a in load_accounts()})

def _profile_ok(d):
    return isinstance(d, dict) and "error" not in d and bool(d.get("nickname"))

def _finish_auto(d, found_region):
    """Mark the profile with the region the player really belongs to."""
    reported = str(d.get("server_region") or "").upper()
    d["searched_via"] = found_region
    d["region"] = reported if REGION_RE.match(reported) else found_region
    d["detected"] = True
    with _region_hint_lock:
        _region_hint[str(d.get("uid"))] = (d["region"] if d["region"] in _account_regions() else found_region, time.time())
    return d

def lookup_profile_auto(uid, hint=None):
    """Finds a player without knowing the region: remembered region first, then every region we have an account for."""
    uid = str(uid).strip()
    if not uid.isdigit():
        return {"error": "UID must be numeric"}
    regions = _account_regions()
    if not regions:
        return {"error": "No game accounts configured (accounts.json)"}

    order = []
    with _region_hint_lock:
        hit = _region_hint.get(uid)
    if hit and time.time() - hit[1] < REGION_HINT_TTL:
        order.append(hit[0])
    if hint:
        order.append(str(hint).upper())
    order.append(default_region())
    order += regions
    order = [r for i, r in enumerate(order) if r in regions and r not in order[:i]]

    errors = {}
    # 1) most likely region on its own (instant for a repeat search)
    first = order[0]
    d = lookup_profile_cached(uid, first)
    if _profile_ok(d):
        return _finish_auto(dict(d), first)
    errors[first] = d.get("error", "Unknown error") if isinstance(d, dict) else "Unknown error"

    # 2) everything else in parallel, first hit wins
    rest = order[1:]
    if rest:
        pool = ThreadPoolExecutor(max_workers=min(4, len(rest)))
        try:
            futures = {pool.submit(lookup_profile_cached, uid, r): r for r in rest}
            for fut in as_completed(futures):
                r = futures[fut]
                try:
                    d = fut.result()
                except Exception as e:
                    d = {"error": str(e)}
                if _profile_ok(d):
                    return _finish_auto(dict(d), r)
                errors[r] = d.get("error", "Unknown error")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    not_found = [r for r, e in errors.items() if "not found" in e.lower()]
    if not_found:
        msg = f"Player {uid} was not found. Checked: {', '.join(sorted(errors))}."
    else:
        first_err = next(iter(errors.values()), "Unknown error")
        msg = f"Could not search any region ({first_err})."
    return {"error": msg, "checked": sorted(errors)}

@app.route("/api/search", methods=["GET"])
def api_search():
    uid = request.args.get("uid")
    region = (request.args.get("region") or "AUTO").upper()
    if not uid:
        return jsonify({"error": "UID is required"}), 400
    if not uid.isdigit() or not (5 <= len(uid) <= 16):
        return jsonify({"error": "UID must be 5-16 digits"}), 400
    if region != "AUTO" and not REGION_RE.match(region):
        return jsonify({"error": "Invalid region"}), 400
    try:
        if region == "AUTO":
            return jsonify(lookup_profile_auto(uid))
        return jsonify(lookup_profile_cached(uid, region))
    except Exception as e:
        log.exception("search crashed")
        return jsonify({"error": str(e)}), 500

@app.route("/api/duo/<uid>")
def api_duo(uid):
    region = (request.args.get("region") or "AUTO").upper()
    if not uid.isdigit() or not (5 <= len(uid) <= 16):
        return jsonify({"error": "UID must be 5-16 digits"}), 400
    if region != "AUTO" and not REGION_RE.match(region):
        return jsonify({"error": "Invalid region"}), 400
    regions = _account_regions()
    if not regions:
        return jsonify({"error": "No game accounts configured (accounts.json)"})
    if region not in regions:
        # AUTO, or a region we have no account for: use where this UID was found, else the default region
        with _region_hint_lock:
            hit = _region_hint.get(uid)
        region = hit[0] if hit and hit[0] in regions else default_region()
    try:
        return jsonify(lookup_duo_cached(uid, region))
    except Exception as e:
        log.exception("duo crashed")
        return jsonify({"error": str(e)}), 500

@app.route("/api/raw/<uid>")
def api_raw(uid):
    """Debug only (set FF_DEBUG=1): every field the game sends for a player, undecoded names, so new fields can be mapped."""
    if os.environ.get("FF_DEBUG") != "1":
        return jsonify({"error": "disabled"}), 404
    region = (request.args.get("region") or default_region()).upper()
    if not uid.isdigit() or not REGION_RE.match(region):
        return jsonify({"error": "bad uid or region"}), 400
    jwt, game_server, err = get_session(region)
    if err:
        return jsonify({"error": err})
    url = f"{(game_server or FALLBACK_GAME_SERVER).rstrip('/')}/GetPlayerPersonalShow"
    body = bytes.fromhex(encrypt_aes(create_proto({"1": int(uid)}).hex()))
    headers = {
        "User-Agent": "UnityPlayer/2018.4.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
        "Accept": "*/*", "Accept-Encoding": "deflate, gzip",
        "X-GA-SV": str(int(time.time())), "Authorization": f"Bearer {jwt}",
        "X-GA": "v1 1", "ReleaseVersion": version_config(region)[0],
        "Content-Type": "application/x-www-form-urlencoded", "X-Unity-Version": "2018.4.12f1",
    }
    r = _http.post(url, headers=headers, data=body, timeout=TIMEOUT_PROFILE)
    if r.status_code != 200:
        return jsonify({"error": f"HTTP {r.status_code}"})
    return jsonify(decode_protobuf(r.content))

@app.route("/api/player_details/<uid>")
def api_player_details(uid):
    region = (request.args.get("region") or default_region()).upper()
    if not uid.isdigit():
        return jsonify({"error": "UID must be numeric"}), 400
    if not REGION_RE.match(region):
        return jsonify({"error": "Invalid region"}), 400
    details = lookup_profile_cached(uid, region)
    if "error" in details:
        return jsonify({"error": details["error"]})
    return jsonify({
        "uid": uid,
        "avatar_id": details.get("avatar_id"),
        "banner_id": details.get("banner_id"),
        "outfit_ids": details.get("outfit_ids", []),
        "weapon_skin_ids": details.get("weapon_skin_ids", []),
    })

@app.route("/api/diag")
def api_diag():
    mode = request.args.get("mode", "BR").upper()
    if mode not in ("BR", "CS"): mode = "BR"
    cur = CURRENT_CS_SEASON if mode == "CS" else CURRENT_SEASON
    results = {}
    regions = sorted({str(a.get("region", "PK")).upper() for a in load_accounts()})
    for region in regions:
        entry = {}
        try:
            jwt, cached_server, err = get_session(region, force=True)
            if err:
                entry = {"login": "fail", "error": err}
            else:
                entry["login"] = "ok"
                entry["jwt_server"] = cached_server
                release_v = version_config(region)[0]

                candidates = list(REGION_GAME_SERVER_CANDIDATES.get(
                    region, REGION_GAME_SERVER_CANDIDATES["DEFAULT"]))
                if cached_server and cached_server not in candidates:
                    candidates.insert(0, cached_server)

                payload_codes = REGION_ALT_PAYLOAD_CODES.get(region, [None])
                server_results = {}

                for pc in payload_codes:
                    body = season_payload(cur, region, mode=mode, payload_region=pc)
                    label = pc or REGION_PAYLOAD_MAP.get(region, region)
                    for srv in candidates:
                        status, players = _try_server_for_lb(
                            srv, jwt, body, region, mode, cur, release_v)
                        server_results[f"{label}@{srv}"] = {
                            "http": status, "players": len(players)}
                        if status == 200 and players:
                            break
                    if any(v["players"] > 0 for v in server_results.values()):
                        break

                entry["servers"] = server_results
                if REQUIRES_SPECIAL_ACCOUNT.get(region):
                    entry["note"] = REQUIRES_SPECIAL_ACCOUNT[region]
        except Exception as e:
            entry["error"] = str(e)
        results[region] = entry
        time.sleep(MIN_LOGIN_GAP)
    return jsonify({"mode": mode, "season": cur, "results": results})

threading.Thread(target=background_updater, daemon=True).start()
threading.Thread(target=_cache_cleanup, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True, use_reloader=False)