"""
TikTok -> YouTube using the Zernio API (your own account only).

Only ONE secret needed: ZERNIO_API_KEY  (zernio.com dashboard -> API keys)
No Google tokens, no client_secrets.json.

LOCAL:   python tiktok_to_youtube.py   (asks questions)
RAILWAY: set env vars; runs with no prompts and exits.

Env vars:
  ZERNIO_API_KEY   (required)
  TIKTOK_HANDLE    e.g. myname (no @)
  PRIVACY          private / unlisted / public   (default private)
  LIMIT            videos per run                (default 5)
  DONE_FILE        finished-IDs file (default /data/done.txt if /data exists)
"""

import os
import time
import webbrowser
from pathlib import Path

import requests
import yt_dlp

API = "https://zernio.com/api/v1"
HERE = Path(__file__).parent
TMP = Path("/tmp/downloads") if os.name != "nt" else HERE / "downloads"
DONE = Path(os.getenv("DONE_FILE") or ("/data/done.txt" if Path("/data").is_dir() else HERE / "done.txt"))
INTERACTIVE = not os.getenv("TIKTOK_HANDLE")

KEY = os.getenv("ZERNIO_API_KEY") or (input("Zernio API key: ").strip() if INTERACTIVE else "")
if not KEY:
    raise SystemExit("Set ZERNIO_API_KEY.")


def api(method, path, **kw):
    for attempt in range(3):
        r = requests.request(method, API + path, headers={"Authorization": f"Bearer {KEY}"}, timeout=120, **kw)
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"Zernio {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("Zernio rate limit, try again later.")


def find_youtube():
    accounts = api("GET", "/accounts").get("accounts", [])
    for a in accounts:
        if a.get("platform") == "youtube" and a.get("isActive", True):
            return a["_id"]
    return None


def ensure_youtube():
    """Use the connected YouTube channel, or start the connect flow."""
    acct = find_youtube()
    if acct:
        print("YouTube is connected.")
        return acct

    profiles = api("GET", "/profiles").get("profiles", [])
    if not profiles:
        raise SystemExit("No Zernio profile found. Create one in the Zernio dashboard first.")
    profile = next((p for p in profiles if p.get("isDefault")), profiles[0])
    url = api("GET", "/connect/youtube", params={"profileId": profile["_id"]})["authUrl"]

    print("\nYouTube is not connected yet. Open this link and click Allow:\n" + url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    for _ in range(60):  # wait up to 5 minutes
        time.sleep(5)
        acct = find_youtube()
        if acct:
            print("YouTube connected!")
            return acct
    raise SystemExit("Timed out waiting for YouTube to connect. Run again after you click Allow.")


def list_tiktoks(handle):
    opts = {"quiet": True, "extract_flat": True, "ignoreerrors": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.tiktok.com/@{handle}", download=False)
    return list(reversed([e for e in (info or {}).get("entries", []) if e]))  # oldest first


def download(url):
    TMP.mkdir(parents=True, exist_ok=True)
    opts = {"quiet": True, "format": "best[ext=mp4]/best", "outtmpl": str(TMP / "%(id)s.%(ext)s")}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info)), info


def host_video(path):
    """Upload the file to Zernio and return its public URL."""
    p = api("POST", "/media/presign", json={"filename": path.name, "contentType": "video/mp4"})
    with path.open("rb") as f:
        r = requests.put(p["uploadUrl"], data=f, headers={"Content-Type": "video/mp4"}, timeout=600)
    r.raise_for_status()
    return p["publicUrl"]


def post_to_youtube(account_id, media_url, title, description, privacy):
    body = {
        "content": description[:4900],
        "mediaItems": [{"type": "video", "url": media_url}],
        "platforms": [{
            "platform": "youtube",
            "accountId": account_id,
            "platformSpecificData": {
                "title": (title.strip() or "My TikTok")[:100],
                "visibility": privacy,
                "categoryId": "22",
                "madeForKids": False,
            },
        }],
        "publishNow": True,
    }
    res = api("POST", "/posts", json=body)
    plat = (res.get("post", {}).get("platforms") or [{}])[0]
    return plat.get("platformPostUrl") or res.get("message", "queued")


def main():
    if INTERACTIVE:
        handle = input("Your TikTok handle: ")
        privacy = input("Privacy (public/private/unlisted) [private]: ").strip() or "private"
        n = input("How many videos this run? [5]: ").strip()
        limit = int(n) if n.isdigit() else 5
    else:
        handle = os.environ["TIKTOK_HANDLE"]
        privacy = os.getenv("PRIVACY", "private")
        limit = int(os.getenv("LIMIT", "5"))
    handle = handle.strip().lstrip("@")

    account_id = ensure_youtube()

    DONE.parent.mkdir(parents=True, exist_ok=True)
    done = set(DONE.read_text().split()) if DONE.exists() else set()
    entries = [e for e in list_tiktoks(handle) if e.get("id") not in done]
    print(f"Found {len(entries)} new TikTok(s). Posting up to {limit}.")

    for entry in entries[:limit]:
        url = entry.get("url") or entry.get("webpage_url")
        try:
            path, info = download(url)
            desc = info.get("description") or info.get("title") or ""
            link = post_to_youtube(account_id, host_video(path), desc.split("\n")[0], desc, privacy)
            print(f"Posted: {link}")
            with DONE.open("a") as f:
                f.write(entry["id"] + "\n")
            path.unlink(missing_ok=True)
        except Exception as e:
            print(f"Skipped {url}: {e}")
    print("Done.")


if __name__ == "__main__":
    main()
