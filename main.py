"""
TikTok -> YouTube dashboard (uses the Zernio API). Single file, runs on Railway.

Railway variables needed:
  ZERNIO_API_KEY   your Zernio API key
  ADMIN_PASSWORD   password to open the dashboard (any username works)
Optional: a Volume mounted at /data so settings + history survive redeploys.
"""

import base64
import hmac
import html
import json
import os
import tempfile
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

API = "https://zernio.com/api/v1"
DATA = Path("/data") if Path("/data").is_dir() else Path(__file__).parent / "data"
DATA.mkdir(parents=True, exist_ok=True)
SETTINGS_F, HISTORY_F = DATA / "settings.json", DATA / "history.json"
DEFAULTS = {"handle": "", "privacy": "private", "limit": 5, "auto": False, "interval": 24}

LOG = deque(maxlen=300)
STATE = {"running": False, "last": None, "next": None}
JOB_LOCK = threading.Lock()
_yt_cache = {"t": 0, "v": None}


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


# ---------- Job ----------
def list_tiktoks(handle):
    import yt_dlp
    opts = {"quiet": True, "extract_flat": True, "ignoreerrors": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.tiktok.com/@{handle}", download=False)
    return list(reversed([e for e in (info or {}).get("entries", []) if e]))  # oldest first


def download(url):
    import yt_dlp
    tmp = Path(tempfile.gettempdir()) / "tt"
    tmp.mkdir(exist_ok=True)
    opts = {"quiet": True, "format": "best[ext=mp4]/best", "outtmpl": str(tmp / "%(id)s.%(ext)s")}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info)), info


def host_video(path):
    p = api("POST", "/media/presign", json={"filename": path.name, "contentType": "video/mp4"})
    with path.open("rb") as f:
        requests.put(p["uploadUrl"], data=f, headers={"Content-Type": "video/mp4"}, timeout=900).raise_for_status()
    return p["publicUrl"]


def post_youtube(account_id, media_url, title, desc, privacy):
    body = {
        "content": desc[:4900],
        "mediaItems": [{"type": "video", "url": media_url}],
        "platforms": [{"platform": "youtube", "accountId": account_id, "platformSpecificData": {
            "title": (title.strip() or "My TikTok")[:100], "visibility": privacy,
            "categoryId": "22", "madeForKids": False}}],
        "publishNow": True,
    }
    res = api("POST", "/posts", json=body)
    plat = (res.get("post", {}).get("platforms") or [{}])[0]
    return plat.get("platformPostUrl") or "queued on Zernio"


def run_job():
    if not JOB_LOCK.acquire(blocking=False):
        return
    STATE["running"] = True
    try:
        s = load_settings()
        log("Run started.")
        acct = find_youtube()
        if not acct:
            log("YouTube is not connected. Click 'Connect YouTube' first.")
            return
        handle = s["handle"].strip().lstrip("@")
        if not handle:
            log("Enter your TikTok handle in settings first.")
            return
        history = load_history()
        done = {h["id"] for h in history}
        entries = [e for e in list_tiktoks(handle) if e.get("id") not in done]
        log(f"Found {len(entries)} new TikTok(s) for @{handle}. Posting up to {s['limit']}.")
        for entry in entries[: int(s["limit"])]:
            url = entry.get("url") or entry.get("webpage_url")
            try:
                path, info = download(url)
                desc = info.get("description") or info.get("title") or ""
                link = post_youtube(acct, host_video(path), desc.split("\n")[0], desc, s["privacy"])
                history.append({"id": entry["id"], "url": link, "time": time.strftime("%Y-%m-%d %H:%M")})
                HISTORY_F.write_text(json.dumps(history))
                path.unlink(missing_ok=True)
                log(f"Posted: {link}")
            except Exception as e:
                log(f"Skipped {url}: {e}")
        log("Run finished.")
    except Exception as e:
        log(f"Error: {e}")
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
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TikTok to YouTube</title>
<style>
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#1a1c20;--mut:#6b7280;--bd:#e3e5ea;--ac:#e11d48}
@media(prefers-color-scheme:dark){:root{--bg:#111316;--card:#1b1e23;--fg:#e8eaed;--mut:#9aa0a8;--bd:#2c3037}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:20px}h1{font-size:20px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:14px}
label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
input,select{width:100%;padding:9px;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
.row{display:flex;gap:10px}.row>*{flex:1}
button,.btn{display:inline-block;padding:9px 14px;border:0;border-radius:8px;background:var(--ac);color:#fff;font:inherit;cursor:pointer;text-decoration:none}
button.sec{background:transparent;color:var(--fg);border:1px solid var(--bd)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
pre{background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:10px;height:220px;overflow:auto;font-size:12px;white-space:pre-wrap;margin:8px 0 0}
a{color:var(--ac)}.mut{color:var(--mut);font-size:13px}.chk{display:flex;align-items:center;gap:8px;margin-top:12px}.chk input{width:auto}
</style></head><body><main>
<h1>TikTok to YouTube</h1>
%%WARN%%
<div class="card"><b>YouTube</b><br><span class="dot" style="background:%%YTCOLOR%%"></span>%%YTTEXT%%
<div style="margin-top:10px">%%YTBTN%%</div></div>
<form class="card" method="post" action="/save"><b>Settings</b>
<label>TikTok handle (no @)</label><input name="handle" value="%%HANDLE%%" placeholder="yourname">
<div class="row"><div><label>Privacy on YouTube</label><select name="privacy">%%PRIV%%</select></div>
<div><label>Videos per run</label><input name="limit" type="number" min="1" max="30" value="%%LIMIT%%"></div></div>
<div class="chk"><input type="checkbox" name="auto" id="auto" %%AUTO%%><label for="auto" style="margin:0;color:var(--fg)">Run automatically every</label>
<input name="interval" type="number" min="1" max="168" value="%%INT%%" style="width:70px"> hours</div>
<div style="margin-top:14px"><button type="submit">Save settings</button></div></form>
<div class="card"><div class="row" style="align-items:center"><b>Activity</b>
<span class="mut" id="st" style="text-align:right">%%STATUS%%</span></div>
<form method="post" action="/run" style="margin:10px 0 0"><button type="submit">Run now</button></form>
<pre id="log">%%LOG%%</pre></div>
<div class="card"><b>Posted (%%COUNT%%)</b><div class="mut" style="margin-top:6px">%%HIST%%</div>
<form method="post" action="/reset" onsubmit="return confirm('Forget what was posted? Old TikToks may be posted again.')" style="margin-top:10px">
<button class="sec" type="submit">Reset history</button></form></div>
</main><script>
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


def dashboard():
    s = load_settings()
    warn, yt_ok, yt_text = "", False, "Checking failed"
    try:
        yt_ok = bool(find_youtube(cached=True))
        yt_text = "Connected" if yt_ok else "Not connected"
    except Exception as e:
        warn = f'<div class="card" style="border-color:var(--ac)">{html.escape(str(e))}</div>'
    btn = '<a class="btn" href="/connect">%s YouTube</a>' % ("Reconnect" if yt_ok else "Connect")
    hist = load_history()
    h_html = "<br>".join(
        f'{html.escape(h["time"])} · <a href="{html.escape(h["url"])}" target="_blank">{html.escape(h["url"])}</a>'
        if str(h["url"]).startswith("http") else f'{html.escape(h["time"])} · {html.escape(str(h["url"]))}'
        for h in reversed(hist[-15:])) or "Nothing yet."
    priv = "".join(f'<option value="{p}"{" selected" if s["privacy"] == p else ""}>{p}</option>'
                   for p in ("private", "unlisted", "public"))
    rep = {"WARN": warn, "YTCOLOR": "#16a34a" if yt_ok else "#dc2626", "YTTEXT": yt_text, "YTBTN": btn,
           "HANDLE": html.escape(s["handle"], quote=True), "PRIV": priv, "LIMIT": str(s["limit"]),
           "AUTO": "checked" if s["auto"] else "", "INT": str(s["interval"]), "STATUS": html.escape(status_text()),
           "LOG": html.escape("\n".join(LOG)), "COUNT": str(len(hist)), "HIST": h_html}
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
        self.send(401, "Password required", headers={"WWW-Authenticate": 'Basic realm="TikTok to YouTube"'})
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
        n = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        path = urlparse(self.path).path
        if path == "/save":
            s = load_settings()
            s["handle"] = form.get("handle", "").strip().lstrip("@")[:60]
            s["privacy"] = form.get("privacy") if form.get("privacy") in ("private", "unlisted", "public") else "private"
            s["limit"] = max(1, min(30, int(form.get("limit") or 5)))
            s["interval"] = max(1, min(168, int(form.get("interval") or 24)))
            s["auto"] = "auto" in form
            SETTINGS_F.write_text(json.dumps(s))
            STATE["next"] = time.time() + 60 if s["auto"] else None
            log("Settings saved.")
        elif path == "/run":
            if not STATE["running"]:
                threading.Thread(target=run_job, daemon=True).start()
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
    log(f"Dashboard running on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
