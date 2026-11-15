"""
TikTok / old YouTube channel -> new YouTube channel dashboard (uses the Zernio API).
Single file, runs on Railway.

Railway variables needed:
  ZERNIO_API_KEY   your Zernio API key
  ADMIN_PASSWORD   password to open the dashboard (any username works)
Optional:
  A Volume mounted at /data so settings, history and cookies survive redeploys.
  ffmpeg on the server for full-quality YouTube downloads.
  YT_COOKIES       cookies.txt contents (only needed if you don't use the dashboard box).

If YouTube blocks the bot, the log says FAILED and the dashboard shows a box where
you paste your cookies.txt. The bot then retries by itself.
"""

import base64
import hmac
import html
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

API = "https://zernio.com/api/v1"
DATA = Path("/data") if Path("/data").is_dir() else Path(__file__).parent / "data"
DATA.mkdir(parents=True, exist_ok=True)
SETTINGS_F, HISTORY_F, COOKIES_F = DATA / "settings.json", DATA / "history.json", DATA / "cookies.txt"
DEFAULTS = {
    "source": "tiktok",       # tiktok | youtube | both
    "handle": "",             # TikTok handle
    "yt_channel": "",         # old YouTube channel: @handle, channel URL or UC... id
    "yt_content": "streams",  # streams (VODs) | videos (uploads) | both
    "order": "oldest",        # newest | oldest | random
    "privacy": "private",
    "limit": 5,
    "auto": False,
    "interval": 24,
}
SOURCES = ("tiktok", "youtube", "both")
YT_CONTENT = ("streams", "videos", "both")
ORDERS = ("newest", "oldest", "random")
PRIVACY = ("private", "unlisted", "public")

AUTH_HINTS = ("sign in", "confirm you", "not a bot", "cookies", "log in", "login", "age-restricted",
              "members-only", "private video", "429", "too many requests", "po token")

LOG = deque(maxlen=300)
STATE = {"running": False, "last": None, "next": None, "need_cookies": False}
JOB_LOCK = threading.Lock()
_yt_cache = {"t": 0, "v": None}


class NeedSignIn(Exception):
    pass


def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    print(line, flush=True)
    LOG.append(line)


def load_settings():
    try:
        return {**DEFAULTS, **json.loads(SETTINGS_F.read_text())}
    except Exception:
        return dict(DEFAULTS)


def load_history():
    try:
        return json.loads(HISTORY_F.read_text())
    except Exception:
        return []


def clean_err(e):
    t = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    t = re.sub(r"^ERROR:\s*", "", t.strip())
    return " ".join(t.split())[:350]


def is_auth_error(msg):
    m = msg.lower()
    return any(h in m for h in AUTH_HINTS)


def need_signin_message(msg):
    if COOKIES_F.exists():
        return ("FAILED: YouTube rejected the saved cookies (they may have expired). "
                "Export fresh cookies and paste them in the Cookies box. Reason: " + msg)
    return ("FAILED: YouTube needs you to sign in. Paste your cookies.txt in the Cookies box "
            "on the dashboard and the bot will retry automatically. Reason: " + msg)


# ---------- Cookies ----------
def cookie_file():
    """Path to a cookies file for yt-dlp: the dashboard-saved one, else YT_COOKIES env."""
    if COOKIES_F.exists() and COOKIES_F.stat().st_size > 20:
        return str(COOKIES_F)
    data = os.getenv("YT_COOKIES")
    if data:
        p = Path(tempfile.gettempdir()) / "yt_cookies.txt"
        p.write_text(data)
        return str(p)
    return None


def cookie_status():
    if not COOKIES_F.exists():
        return "none saved"
    lines = [l for l in COOKIES_F.read_text().splitlines() if l.strip() and not l.startswith("#")]
    return f"saved ({len(lines)} cookies)"


def save_cookies(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    good = [l for l in text.splitlines() if l.count("\t") >= 6 and not l.startswith("#")]
    if not good:
        log("That does not look like a cookies.txt file (Netscape format). Nothing saved.")
        return False
    if not text.startswith("# Netscape HTTP Cookie File") and not text.startswith("# HTTP Cookie File"):
        text = "# Netscape HTTP Cookie File\n" + text
    COOKIES_F.write_text(text + "\n")
    log(f"Cookies saved ({len(good)} cookies).")
    return True


# ---------- Zernio ----------
def api(method, path, **kw):
    key = os.getenv("ZERNIO_API_KEY")
    if not key:
        raise RuntimeError("ZERNIO_API_KEY is not set in Railway variables.")
    for attempt in range(3):
        r = requests.request(method, API + path, headers={"Authorization": f"Bearer {key}"}, timeout=120, **kw)
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"Zernio {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("Zernio rate limit, try again later.")


def find_youtube(cached=False):
    if cached and time.time() - _yt_cache["t"] < 20:
        return _yt_cache["v"]
    acct = None
    for a in api("GET", "/accounts").get("accounts", []):
        if a.get("platform") == "youtube" and a.get("isActive", True):
            acct = a["_id"]
            break
    _yt_cache.update(t=time.time(), v=acct)
    return acct


# ---------- Sources ----------
def list_tiktoks(handle):
    """Returns entries newest first (the order TikTok lists them)."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.tiktok.com/@{handle}", download=False)
    except Exception as e:
        raise RuntimeError(f"Could not read TikTok @{handle}: {clean_err(e)}")
    return [e for e in (info or {}).get("entries", []) if e]


def channel_base(channel):
    """Turn '@name', 'name', a UC... id or a channel URL into a clean channel URL."""
    c = channel.strip()
    if c.startswith("http"):
        c = c.rstrip("/")
        for tail in ("/videos", "/streams", "/shorts", "/featured", "/live"):
            if c.endswith(tail):
                c = c[: -len(tail)]
        return c
    if c.startswith("UC") and len(c) > 20:
        return f"https://www.youtube.com/channel/{c}"
    return f"https://www.youtube.com/@{c.lstrip('@')}"


def list_youtube(channel, content):
    """Returns entries newest first. 'streams' = past livestreams (VODs)."""
    import yt_dlp
    base = channel_base(channel)
    tabs = ["streams", "videos"] if content == "both" else [content]
    urls = [f"{base}/{t}" for t in tabs]
    m = re.search(r"/channel/(UC[\w-]{22})", base)
    if m:  # uploads playlist: includes past streams, sometimes works when the tabs do not
        urls.append("https://www.youtube.com/playlist?list=UU" + m.group(1)[2:])
    seen, out, errors = set(), [], []
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}
    ck = cookie_file()
    if ck:
        opts["cookiefile"] = ck
    for url in urls:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            msg = clean_err(e)
            errors.append(msg)
            log(f"Could not read {url}: {msg}")
            continue
        count = 0
        for e in (info or {}).get("entries", []) or []:
            if not e or not e.get("id") or e["id"] in seen:
                continue
            if e.get("live_status") in ("is_live", "is_upcoming"):
                continue  # skip streams that haven't finished
            seen.add(e["id"])
            e.setdefault("url", f"https://www.youtube.com/watch?v={e['id']}")
            out.append(e)
            count += 1
        log(f"Checked {url}: {count} video(s) listed.")
    if not out and errors:
        auth = [m for m in errors if is_auth_error(m)]
        if auth:
            raise NeedSignIn(auth[0])
        notab = [m for m in errors if "does not have" in m.lower()]
        if len(notab) == len(errors):
            # YouTube shows no video tabs: channel hidden from logged-out visitors, or bot-blocked.
            if not cookie_file():
                raise NeedSignIn(notab[0] + " (the channel may be hidden unless you are signed in)")
            raise RuntimeError(
                f"Even with your cookies YouTube shows no video tabs for {base}. Check the name is right, "
                "that the channel opens with videos while signed in, and that the cookies are fresh "
                "and come from an account that can see it.")
        raise RuntimeError(f"Could not read the old channel ({base}): {errors[0]}")
    return out


def apply_order(entries, order):
    """entries come newest first."""
    entries = list(entries)
    if order == "oldest":
        entries.reverse()
    elif order == "random":
        random.shuffle(entries)
    return entries


def interleave(a, b):
    out = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
    return out


def download(url, is_youtube=False):
    import yt_dlp
    tmp = Path(tempfile.gettempdir()) / "tt"
    tmp.mkdir(exist_ok=True)
    opts = {"quiet": True, "no_warnings": True, "format": "best[ext=mp4]/best",
            "outtmpl": str(tmp / "%(id)s.%(ext)s")}
    if is_youtube:
        if shutil.which("ffmpeg"):
            opts["format"] = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b"
            opts["merge_output_format"] = "mp4"
        ck = cookie_file()
        if ck:
            opts["cookiefile"] = ck
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        if not path.exists():  # merged files can end up as .mp4
            alt = path.with_suffix(".mp4")
            if alt.exists():
                path = alt
        return path, info


def host_video(path):
    p = api("POST", "/media/presign", json={"filename": path.name, "contentType": "video/mp4"})
    with path.open("rb") as f:
        requests.put(p["uploadUrl"], data=f, headers={"Content-Type": "video/mp4"}, timeout=3600).raise_for_status()
    return p["publicUrl"]


def post_youtube(account_id, media_url, title, desc, privacy):
    body = {
        "content": desc[:4900],
        "mediaItems": [{"type": "video", "url": media_url}],
        "platforms": [{"platform": "youtube", "accountId": account_id, "platformSpecificData": {
            "title": (title.strip() or "My video")[:100], "visibility": privacy,
            "categoryId": "22", "madeForKids": False}}],
        "publishNow": True,
    }
    res = api("POST", "/posts", json=body)
    plat = (res.get("post", {}).get("platforms") or [{}])[0]
    return plat.get("platformPostUrl") or "queued on Zernio"


# ---------- Job ----------
def run_job():
    if not JOB_LOCK.acquire(blocking=False):
        return
    STATE["running"] = True
    STATE["need_cookies"] = False
    try:
        s = load_settings()
        log("Run started.")
        acct = find_youtube()
        if not acct:
            log("YouTube is not connected. Click 'Connect YouTube' first.")
            return

        history = load_history()
        done = {h["id"] for h in history}
        use_tt = s["source"] in ("tiktok", "both")
        use_yt = s["source"] in ("youtube", "both")

        handle = s["handle"].strip().lstrip("@")
        if use_tt and not handle:
            log("Enter your TikTok handle in settings first.")
            return
        if use_yt and not s["yt_channel"].strip():
            log("Enter the old YouTube channel in settings first.")
            return

        tt, yt = [], []
        if use_tt:
            tt = [dict(e, _src="tiktok") for e in list_tiktoks(handle) if e.get("id") not in done]
            log(f"Found {len(tt)} new TikTok(s) for @{handle}.")
        if use_yt:
            try:
                yt = [dict(e, _src="youtube") for e in list_youtube(s["yt_channel"], s["yt_content"])
                      if e.get("id") not in done]
            except NeedSignIn as e:
                STATE["need_cookies"] = True
                log(need_signin_message(clean_err(e)))
                if not use_tt:
                    return
            log(f"Found {len(yt)} new video(s) on the old YouTube channel ({s['yt_content']}).")

        if use_tt and use_yt:
            if s["order"] == "random":
                entries = apply_order(tt + yt, "random")
            else:
                entries = interleave(apply_order(tt, s["order"]), apply_order(yt, s["order"]))
        else:
            entries = apply_order(tt or yt, s["order"])

        log(f"Posting up to {s['limit']} ({s['order']} first).")
        for entry in entries[: int(s["limit"])]:
            url = entry.get("url") or entry.get("webpage_url")
            is_yt = entry["_src"] == "youtube"
            try:
                path, info = download(url, is_youtube=is_yt)
                desc = info.get("description") or info.get("title") or ""
                title = (info.get("title") or "") if is_yt else desc.split("\n")[0]
                link = post_youtube(acct, host_video(path), title, desc, s["privacy"])
                history.append({"id": entry["id"], "url": link, "time": time.strftime("%Y-%m-%d %H:%M")})
                HISTORY_F.write_text(json.dumps(history))
                path.unlink(missing_ok=True)
                log(f"Posted: {link}")
            except Exception as e:
                msg = clean_err(e)
                if is_yt and is_auth_error(msg):
                    STATE["need_cookies"] = True
                    log(need_signin_message(msg))
                    break  # every other YouTube download would fail the same way
                log(f"Skipped {url}: {msg}")
        log("Run finished.")
    except Exception as e:
        log(f"Error: {clean_err(e)}")
    finally:
        STATE["running"] = False
        STATE["last"] = time.strftime("%Y-%m-%d %H:%M")
        JOB_LOCK.release()


def scheduler():
    while True:
        time.sleep(30)
        s = load_settings()
        if s["auto"] and not STATE["running"] and STATE["next"] and time.time() >= STATE["next"]:
            STATE["next"] = time.time() + int(s["interval"]) * 3600
            threading.Thread(target=run_job, daemon=True).start()


# ---------- Web ----------
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Video Reposter</title>
<style>
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#1a1c20;--mut:#6b7280;--bd:#e3e5ea;--ac:#e11d48}
@media(prefers-color-scheme:dark){:root{--bg:#111316;--card:#1b1e23;--fg:#e8eaed;--mut:#9aa0a8;--bd:#2c3037}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:20px}h1{font-size:20px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:14px}
label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
input,select,textarea{width:100%;padding:9px;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
textarea{font:12px ui-monospace,monospace;resize:vertical}
.row{display:flex;gap:10px}.row>*{flex:1}
button,.btn{display:inline-block;padding:9px 14px;border:0;border-radius:8px;background:var(--ac);color:#fff;font:inherit;cursor:pointer;text-decoration:none}
button.sec{background:transparent;color:var(--fg);border:1px solid var(--bd)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
pre{background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:10px;height:220px;overflow:auto;font-size:12px;white-space:pre-wrap;margin:8px 0 0}
a{color:var(--ac)}.mut{color:var(--mut);font-size:13px}.chk{display:flex;align-items:center;gap:8px;margin-top:12px}.chk input{width:auto}
.alert{border-color:var(--ac)}
</style></head><body><main>
<h1>Video Reposter</h1>
%%WARN%%
<div class="card"><b>New YouTube channel (where videos get posted)</b><br><span class="dot" style="background:%%YTCOLOR%%"></span>%%YTTEXT%%
<div style="margin-top:10px">%%YTBTN%%</div>
<div class="mut" style="margin-top:8px">Click the button, sign in with Google and accept, and videos will post to that channel.</div></div>
<form class="card" method="post" action="/save"><b>Settings</b>
<label>Take videos from</label><select name="source" id="source">%%SRC%%</select>
<div id="tt-box"><label>TikTok handle (no @)</label><input name="handle" value="%%HANDLE%%" placeholder="yourname"></div>
<div id="yt-box"><label>Old YouTube channel (@handle, channel link or UC... id)</label>
<input name="yt_channel" value="%%YTCHAN%%" placeholder="@oldchannel">
<label>What to take from it</label><select name="yt_content">%%YTCONT%%</select></div>
<label>Post order</label><select name="order">%%ORDER%%</select>
<div class="row"><div><label>Privacy on YouTube</label><select name="privacy">%%PRIV%%</select></div>
<div><label>Videos per run</label><input name="limit" type="number" min="1" max="30" value="%%LIMIT%%"></div></div>
<div class="chk"><input type="checkbox" name="auto" id="auto" %%AUTO%%><label for="auto" style="margin:0;color:var(--fg)">Run automatically every</label>
<input name="interval" type="number" min="1" max="168" value="%%INT%%" style="width:70px"> hours</div>
<div style="margin-top:14px"><button type="submit">Save settings</button></div></form>
<div class="card"><div class="row" style="align-items:center"><b>Activity</b>
<span class="mut" id="st" style="text-align:right">%%STATUS%%</span></div>
<form method="post" action="/run" style="margin:10px 0 0"><button type="submit">Run now</button></form>
<pre id="log">%%LOG%%</pre></div>
<div class="card"><b>YouTube cookies</b> <span class="mut">(%%CKSTATUS%%)</span>
<div class="mut" style="margin-top:6px">Only needed if YouTube blocks the old channel. While signed in to YouTube in your browser, export cookies.txt (for example with the "Get cookies.txt LOCALLY" extension) and paste it here.</div>
<form method="post" action="/cookies"><textarea name="cookies" rows="5" placeholder="# Netscape HTTP Cookie File"></textarea>
<div style="margin-top:10px"><button type="submit">Save cookies and retry</button></div></form>
<form method="post" action="/cookies_clear" style="margin-top:8px"><button class="sec" type="submit">Delete saved cookies</button></form></div>
<div class="card"><b>Posted (%%COUNT%%)</b><div class="mut" style="margin-top:6px">%%HIST%%</div>
<form method="post" action="/reset" onsubmit="return confirm('Forget what was posted? Old videos may be posted again.')" style="margin-top:10px">
<button class="sec" type="submit">Reset history</button></form></div>
</main><script>
function vis(){const v=document.getElementById('source').value;
document.getElementById('tt-box').style.display=(v==='youtube')?'none':'block';
document.getElementById('yt-box').style.display=(v==='tiktok')?'none':'block'}
document.getElementById('source').addEventListener('change',vis);vis();
async function t(){try{const r=await fetch('/log');const d=await r.json();
const l=document.getElementById('log');const b=l.scrollTop+l.clientHeight>=l.scrollHeight-20;
l.textContent=d.log;if(b)l.scrollTop=l.scrollHeight;document.getElementById('st').textContent=d.status}catch(e){}}
setInterval(t,3000);document.getElementById('log').scrollTop=1e9;
</script></body></html>"""


def status_text():
    s = load_settings()
    parts = ["Running now..." if STATE["running"] else "Idle"]
    if STATE["last"]:
        parts.append("last run " + STATE["last"])
    if s["auto"] and STATE["next"]:
        parts.append("next auto run " + time.strftime("%H:%M", time.localtime(STATE["next"])))
    return " · ".join(parts)


def options(choices, current, labels=None):
    labels = labels or {}
    return "".join(
        f'<option value="{c}"{" selected" if current == c else ""}>{html.escape(labels.get(c, c))}</option>'
        for c in choices)


def dashboard():
    s = load_settings()
    warn, yt_ok, yt_text = "", False, "Checking failed"
    try:
        yt_ok = bool(find_youtube(cached=True))
        yt_text = "Connected" if yt_ok else "Not connected"
    except Exception as e:
        warn += f'<div class="card alert">{html.escape(str(e))}</div>'
    if STATE["need_cookies"]:
        warn += ('<div class="card alert"><b>YouTube needs you to sign in.</b> Paste your cookies in the '
                 '"YouTube cookies" box below and the bot will retry automatically.</div>')
    btn = '<a class="btn" href="/connect">%s YouTube</a>' % ("Reconnect" if yt_ok else "Connect")
    hist = load_history()
    h_html = "<br>".join(
        f'{html.escape(h["time"])} · <a href="{html.escape(h["url"])}" target="_blank">{html.escape(h["url"])}</a>'
        if str(h["url"]).startswith("http") else f'{html.escape(h["time"])} · {html.escape(str(h["url"]))}'
        for h in reversed(hist[-15:])) or "Nothing yet."
    rep = {
        "WARN": warn, "YTCOLOR": "#16a34a" if yt_ok else "#dc2626", "YTTEXT": yt_text, "YTBTN": btn,
        "SRC": options(SOURCES, s["source"], {"tiktok": "TikTok", "youtube": "Old YouTube channel",
                                              "both": "Both (mixed)"}),
        "HANDLE": html.escape(s["handle"], quote=True),
        "YTCHAN": html.escape(s["yt_channel"], quote=True),
        "YTCONT": options(YT_CONTENT, s["yt_content"], {"streams": "VODs (past livestreams)",
                                                        "videos": "Regular uploads",
                                                        "both": "VODs + uploads"}),
        "ORDER": options(ORDERS, s["order"], {"newest": "Latest first", "oldest": "Oldest first",
                                              "random": "Random"}),
        "PRIV": options(PRIVACY, s["privacy"]), "LIMIT": str(s["limit"]),
        "AUTO": "checked" if s["auto"] else "", "INT": str(s["interval"]), "STATUS": html.escape(status_text()),
        "LOG": html.escape("\n".join(LOG)), "COUNT": str(len(hist)), "HIST": h_html,
        "CKSTATUS": html.escape(cookie_status()),
    }
    out = PAGE
    for k, v in rep.items():
        out = out.replace(f"%%{k}%%", v)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, url, code=303):
        self.send(code, "", headers={"Location": url})

    def authed(self):
        pw = os.getenv("ADMIN_PASSWORD")
        if not pw:
            self.send(503, "<h3>Set ADMIN_PASSWORD in Railway variables, then redeploy.</h3>")
            return False
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                given = base64.b64decode(hdr[6:]).decode().split(":", 1)[1]
                if hmac.compare_digest(given.encode(), pw.encode()):
                    return True
            except Exception:
                pass
        self.send(401, "Password required", headers={"WWW-Authenticate": 'Basic realm="Video Reposter"'})
        return False

    def do_GET(self):
        if not self.authed():
            return
        path = urlparse(self.path).path
        if path == "/":
            self.send(200, dashboard())
        elif path == "/log":
            self.send(200, json.dumps({"log": "\n".join(LOG), "status": status_text()}), "application/json")
        elif path == "/connect":
            try:
                profiles = api("GET", "/profiles").get("profiles", [])
                if not profiles:
                    raise RuntimeError("No Zernio profile found. Create one in the Zernio dashboard.")
                p = next((x for x in profiles if x.get("isDefault")), profiles[0])
                host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
                url = api("GET", "/connect/youtube", params={"profileId": p["_id"], "redirect_url": f"https://{host}/"})["authUrl"]
                self.redirect(url, 302)
            except Exception as e:
                self.send(500, f"<h3>Could not start connect:</h3><pre>{html.escape(str(e))}</pre><a href='/'>Back</a>")
        else:
            self.send(404, "Not found")

    def do_POST(self):
        if not self.authed():
            return
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self.send(403, "Blocked")
        n = min(int(self.headers.get("Content-Length") or 0), 3_000_000)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode(errors="replace")).items()}
        path = urlparse(self.path).path
        if path == "/save":
            s = load_settings()
            s["source"] = form.get("source") if form.get("source") in SOURCES else "tiktok"
            s["handle"] = form.get("handle", "").strip().lstrip("@")[:60]
            s["yt_channel"] = form.get("yt_channel", "").strip()[:200]
            s["yt_content"] = form.get("yt_content") if form.get("yt_content") in YT_CONTENT else "streams"
            s["order"] = form.get("order") if form.get("order") in ORDERS else "oldest"
            s["privacy"] = form.get("privacy") if form.get("privacy") in PRIVACY else "private"
            s["limit"] = max(1, min(30, int(form.get("limit") or 5)))
            s["interval"] = max(1, min(168, int(form.get("interval") or 24)))
            s["auto"] = "auto" in form
            SETTINGS_F.write_text(json.dumps(s))
            STATE["next"] = time.time() + 60 if s["auto"] else None
            log("Settings saved.")
        elif path == "/run":
            if not STATE["running"]:
                threading.Thread(target=run_job, daemon=True).start()
        elif path == "/cookies":
            if save_cookies(form.get("cookies", "")) and not STATE["running"]:
                log("Retrying with the new cookies...")
                threading.Thread(target=run_job, daemon=True).start()
        elif path == "/cookies_clear":
            COOKIES_F.unlink(missing_ok=True)
            log("Saved cookies deleted.")
        elif path == "/reset":
            HISTORY_F.write_text("[]")
            log("History reset.")
        self.redirect("/")


if __name__ == "__main__":
    s = load_settings()
    if s["auto"]:
        STATE["next"] = time.time() + 60
    threading.Thread(target=scheduler, daemon=True).start()
    port = int(os.getenv("PORT", "8080"))
    try:
        import yt_dlp
        log(f"yt-dlp version {yt_dlp.version.__version__}")
    except Exception as e:
        log(f"yt-dlp is not installed: {e}")
    log(f"Dashboard running on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
