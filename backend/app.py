import glob, json, os, re, secrets, shlex, subprocess, threading, time, uuid
from flask import Flask, jsonify, request, send_file, abort, redirect
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ─── Config ──────────────────────────────────────────────────────────────────
# Deliberately reuses Spotidrome's own .env (same Navidrome/SSH target — it's
# the same music library, this is just a second, faster front door into it).
DATA_DIR       = "/data"
DOWNLOAD_DIR   = os.path.join(DATA_DIR, "downloads")
STATE_FILE     = os.path.join(DATA_DIR, "state.json")
SSH_KEY        = "/root/.ssh/id_rsa"
NAV_SUBFOLDER  = "Jam"  # tracks land in {music_path}/Jam/ on the Navidrome host

SSH_HOST       = os.environ.get("SSH_HOST", "")
SSH_USER       = os.environ.get("SSH_USER", "")
SSH_PORT       = os.environ.get("SSH_PORT", "22")
SSH_MUSIC_PATH = os.environ.get("SSH_MUSIC_PATH", "/opt/navidrome/music")
NAVIDROME_URL      = os.environ.get("NAVIDROME_URL", "").rstrip("/")
NAVIDROME_USER     = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASSWORD = os.environ.get("NAVIDROME_PASSWORD", "")

# Same loudness target Spotidrome normalizes its own downloads to, so a
# jam-requested track sits at the same volume as everything else in the
# library — see Spotidrome's LOUDNORM_FILTER for the full rationale.
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"

# bgutil-pot-provider is Spotidrome's own container — reused here rather than
# running a second copy, by joining this app's compose to Spotidrome's
# docker network (see docker-compose.yml).
def _pot_args_for_client(player_client):
    return ["--extractor-args", "youtubepot-bgutilhttp:base_url=http://bgutil-pot:4416",
            "--extractor-args", f"youtube:player_client={player_client}",
            "--remote-components", "ejs:github"]

YTDLP_POT_ARGS = _pot_args_for_client("mweb")

MAX_HISTORY         = 50
ADVANCE_BUFFER_SEC   = 5     # grace period added on top of a track's own duration
                             # before the server auto-advances without a client signal
SEARCH_RESULT_COUNT  = 10
REQUEST_PAGE_PORT    = 9998
MIN_INVITE_TTL_SEC   = 60
MAX_INVITE_TTL_SEC   = 7 * 24 * 3600

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ─── State ───────────────────────────────────────────────────────────────────
# queue:  ordered, not-yet-finished items — status one of
#         queued -> downloading -> ready -> playing
# history: finished (done/failed) items, most recent last, capped at MAX_HISTORY
# invites: token -> {created_at, expires_at, ttl_seconds} — share links minted
#          from the player page; each one is independent (generating a new
#          one does not invalidate earlier ones) and reusable by anyone who
#          has it until it expires.
state_lock = threading.Lock()
state = {"queue": [], "history": [], "now_playing_id": None, "playback_started_at": None, "invites": {}}


def load_state():
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
    except Exception:
        return
    # A track still "downloading" when the process last stopped never
    # finished — requeue it so the worker picks it back up, rather than
    # leaving it stuck forever.
    for item in saved.get("queue", []):
        if item.get("status") == "downloading":
            item["status"] = "queued"
    state.update(saved)
    # Nothing was actually playing across a restart — the audio element on
    # any open player page is gone. Let the worker re-promote once ready.
    state["now_playing_id"] = None
    state["playback_started_at"] = None
    for item in state["queue"]:
        if item.get("status") == "playing":
            item["status"] = "ready"


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def find_item(item_id):
    for item in state["queue"]:
        if item["id"] == item_id:
            return item
    return None


def public_view(item, include_stream=False):
    v = {k: item[k] for k in ("id", "video_id", "title", "artist", "thumbnail",
                               "duration", "status", "requested_by", "added_at")}
    if include_stream:
        v["stream_url"] = f"/stream/{item['id']}"
    return v


# ─── yt-dlp search & download ───────────────────────────────────────────────

def search_tracks(query, limit=SEARCH_RESULT_COUNT):
    cmd = (["yt-dlp", "--dump-json", "--flat-playlist", "--no-playlist"] + YTDLP_POT_ARGS +
           [f"ytsearch{limit}:{query}"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return []
    if result.returncode != 0:
        return []
    out = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        thumbs = e.get("thumbnails") or []
        thumb = thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{e.get('id')}/hqdefault.jpg"
        out.append({
            "video_id": e.get("id"),
            "title": e.get("title") or "Unknown title",
            "artist": e.get("channel") or e.get("uploader") or "Unknown artist",
            "duration": e.get("duration"),
            "thumbnail": thumb,
            "url": e.get("webpage_url") or e.get("url"),
        })
    return out


def ffprobe_duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", path],
                            capture_output=True, text=True, timeout=15)
        return float(r.stdout.strip())
    except Exception:
        return None


def download_track(item):
    """Download + loudness-normalize straight to FLAC, exactly like
    Spotidrome's own downloads, so playback and the permanent library copy
    are identical files."""
    out_path = os.path.join(DOWNLOAD_DIR, f"{item['id']}.flac")

    def attempt(pot_args):
        cmd = ["yt-dlp",
               "-x", "--audio-format", "flac", "--audio-quality", "0",
               "--postprocessor-args", f"ExtractAudio:-af {LOUDNORM_FILTER}",
               "--add-metadata", "--embed-thumbnail",
               "--output", out_path.replace(".flac", ".%(ext)s"),
               "--no-playlist", "--retries", "2", "--fragment-retries", "2",
               "--socket-timeout", "10", "--no-progress",
               ] + pot_args + [item["url"]]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180)

    r = attempt(YTDLP_POT_ARGS)
    needs_fallback = r.returncode != 0 or not os.path.exists(out_path)
    if needs_fallback and ("403" in (r.stderr or "") or "not available" in (r.stderr or "")):
        # Same fallback Spotidrome relies on: the mweb client occasionally
        # gets a probabilistic 403 from YouTube's anti-bot enforcement:
        # android routes around it, at the cost of a real quality drop
        # (legacy itag 18, ~96kbps AAC transcoded to FLAC) since android's
        # proper adaptive audio streams are themselves currently blocked by
        # a separate YouTube-side SABR restriction.
        r = attempt(_pot_args_for_client("android"))

    if r.returncode != 0 or not os.path.exists(out_path):
        _cleanup_downloaded_files(item["id"])  # yt-dlp can leave behind a
        # partial audio file, thumbnail, etc. even on a failed run
        raise RuntimeError((r.stderr or "yt-dlp failed")[-300:])
    return out_path, ffprobe_duration(out_path)


def _cleanup_downloaded_files(item_id):
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{item_id}.*")):
        try:
            os.remove(f)
        except Exception:
            pass


def _ssh_cmd(remote_command):
    return ["ssh", "-i", SSH_KEY, "-p", str(SSH_PORT),
            "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            f"{SSH_USER}@{SSH_HOST}", remote_command]


def sync_to_navidrome_bg(item):
    """Best-effort archival copy into the permanent library — never blocks
    or affects playback, which already streams from the local download."""
    if not (SSH_HOST and SSH_USER):
        return
    try:
        remote_dir = f"{SSH_MUSIC_PATH}/{NAV_SUBFOLDER}"
        subprocess.run(_ssh_cmd(f"mkdir -p {shlex.quote(remote_dir)}"),
                        capture_output=True, timeout=15)
        safe_name = re.sub(r'[<>:"/\\|?*]', "_", f"{item['artist']} - {item['title']}")[:180]
        dest = f"{SSH_USER}@{SSH_HOST}:{remote_dir}/{safe_name}.flac"
        rsync_cmd = ["rsync", "-a", "-e", f"ssh -i {SSH_KEY} -p {SSH_PORT} -o StrictHostKeyChecking=no -o BatchMode=yes",
                     item["local_path"], dest]
        subprocess.run(rsync_cmd, capture_output=True, timeout=120)
        if NAVIDROME_URL and NAVIDROME_USER:
            import requests as http
            http.put(f"{NAVIDROME_URL}/api/scanner/trigger",
                      auth=(NAVIDROME_USER, NAVIDROME_PASSWORD), timeout=15)
    except Exception as e:
        print(f"[jam] Navidrome sync failed for {item['id']}: {e}", flush=True)


# ─── Queue engine ────────────────────────────────────────────────────────────

def _promote_next_if_idle():
    """If nothing is playing and the head of the queue is ready, start it.
    Must be called with state_lock held."""
    if state["now_playing_id"] is not None:
        return
    while state["queue"] and state["queue"][0]["status"] == "failed":
        state["history"].append(state["queue"].pop(0))
    if state["queue"] and state["queue"][0]["status"] == "ready":
        nxt = state["queue"][0]
        nxt["status"] = "playing"
        state["now_playing_id"] = nxt["id"]
        state["playback_started_at"] = time.time()


def advance():
    """Finish whatever's currently playing and promote the next ready item."""
    with state_lock:
        cur_id = state["now_playing_id"]
        if cur_id:
            cur = find_item(cur_id)
            if cur:
                cur["status"] = "done"
                state["queue"].remove(cur)
                state["history"].append(cur)
                state["history"] = state["history"][-MAX_HISTORY:]
                if cur.get("local_path"):
                    # The background Navidrome rsync started the moment
                    # this track finished downloading, minutes ago by now
                    # (it just finished an entire play-through) — safe to
                    # delete the local copy shortly, rather than letting
                    # every played track sit in /data/downloads forever.
                    threading.Timer(30, _cleanup_downloaded_files, args=(cur["id"],)).start()
        state["now_playing_id"] = None
        state["playback_started_at"] = None
        _promote_next_if_idle()
        save_state()


def download_worker_loop():
    while True:
        with state_lock:
            item = next((i for i in state["queue"] if i["status"] == "queued"), None)
            if item:
                item["status"] = "downloading"
        if not item:
            time.sleep(1)
            continue
        try:
            local_path, duration = download_track(item)
            with state_lock:
                item["local_path"] = local_path
                item["duration"] = duration or item.get("duration")
                item["status"] = "ready"
                _promote_next_if_idle()
                save_state()
            threading.Thread(target=sync_to_navidrome_bg, args=(item,), daemon=True).start()
        except Exception as e:
            with state_lock:
                item["status"] = "failed"
                item["error"] = str(e)[:300]
                _promote_next_if_idle()  # a failed item must not block everything queued behind it
                save_state()
            print(f"[jam] Download failed for {item.get('title')}: {e}", flush=True)


def watchdog_loop():
    """Safety net: if the player page never reports 'ended' (closed tab,
    crashed browser, etc.) the jam shouldn't just hang forever."""
    while True:
        time.sleep(2)
        with state_lock:
            cur_id = state["now_playing_id"]
            started = state["playback_started_at"]
            cur = find_item(cur_id) if cur_id else None
            duration = (cur or {}).get("duration") or 0
        if cur and started and time.time() - started > duration + ADVANCE_BUFFER_SEC:
            advance()


# ─── Invite links ────────────────────────────────────────────────────────────
# Minted from the player page (:9999) so the host can hand out a link that
# drops people straight onto the request page (:9998) — one row of state per
# generated link, each independently timed out.

def _prune_expired_invites():
    """Must be called with state_lock held."""
    now = time.time()
    expired = [t for t, inv in state["invites"].items() if inv["expires_at"] <= now]
    for t in expired:
        del state["invites"][t]


def _request_page_url():
    # $http_host (forwarded below as X-Forwarded-Host) preserves whatever
    # host:port the browser actually sent, unlike nginx's own $host which
    # strips the port — needed here since the link has to swap :9999 for
    # :9998 on whatever hostname/IP someone is actually browsing from.
    host_hdr = request.headers.get("X-Forwarded-Host") or request.host or ""
    hostname = host_hdr.split(":")[0] or "localhost"
    return f"http://{hostname}:{REQUEST_PAGE_PORT}/"


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/search")
def route_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    return jsonify({"results": search_tracks(q)})


@app.route("/queue", methods=["GET"])
def route_queue_get():
    with state_lock:
        now_playing = find_item(state["now_playing_id"]) if state["now_playing_id"] else None
        return jsonify({
            "now_playing": public_view(now_playing, include_stream=True) if now_playing else None,
            "playback_started_at": state["playback_started_at"],
            "queue": [public_view(i) for i in state["queue"] if i["status"] != "playing"],
            "history": [public_view(i) for i in state["history"][-10:]],
        })


@app.route("/queue/add", methods=["POST"])
def route_queue_add():
    data = request.json or {}
    video_id = (data.get("video_id") or "").strip()
    url = (data.get("url") or "").strip()
    title = (data.get("title") or "Unknown title").strip()
    if not video_id or not url:
        return jsonify({"error": "video_id and url are required"}), 400

    with state_lock:
        active_ids = {i["video_id"] for i in state["queue"] if i["status"] in ("queued", "downloading", "ready", "playing")}
        if video_id in active_ids:
            return jsonify({"error": "That song is already in the queue"}), 409

        item = {
            "id": uuid.uuid4().hex[:12],
            "video_id": video_id, "url": url, "title": title,
            "artist": (data.get("artist") or "Unknown artist").strip(),
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail") or "",
            "requested_by": (data.get("requested_by") or "Anonymous").strip()[:40] or "Anonymous",
            "status": "queued", "added_at": time.time(),
        }
        state["queue"].append(item)
        save_state()
        return jsonify(public_view(item))


@app.route("/queue/<item_id>/remove", methods=["POST"])
def route_queue_remove(item_id):
    with state_lock:
        item = find_item(item_id)
        if not item:
            return jsonify({"error": "Not found"}), 404
        if item["status"] != "queued":
            return jsonify({"error": "Can only remove a track that hasn't started downloading yet"}), 400
        state["queue"].remove(item)
        save_state()
        return jsonify({"ok": True})


@app.route("/player/state")
def route_player_state():
    with state_lock:
        now_playing = find_item(state["now_playing_id"]) if state["now_playing_id"] else None
        return jsonify({
            "now_playing": public_view(now_playing, include_stream=True) if now_playing else None,
            "playback_started_at": state["playback_started_at"],
            "up_next": [public_view(i) for i in state["queue"] if i["status"] != "playing"][:10],
        })


@app.route("/player/advance", methods=["POST"])
def route_player_advance():
    advance()
    return jsonify({"ok": True})


@app.route("/stream/<item_id>")
def route_stream(item_id):
    with state_lock:
        item = find_item(item_id)
        path = item.get("local_path") if item else None
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="audio/flac", conditional=True)


@app.route("/invite/create", methods=["POST"])
def route_invite_create():
    data = request.json or {}
    try:
        ttl_seconds = int(data.get("ttl_seconds"))
    except (TypeError, ValueError):
        return jsonify({"error": "ttl_seconds must be a number"}), 400
    ttl_seconds = max(MIN_INVITE_TTL_SEC, min(MAX_INVITE_TTL_SEC, ttl_seconds))

    with state_lock:
        _prune_expired_invites()
        token = secrets.token_urlsafe(9)
        now = time.time()
        state["invites"][token] = {"created_at": now, "expires_at": now + ttl_seconds, "ttl_seconds": ttl_seconds}
        save_state()
        return jsonify({"token": token, "created_at": now, "expires_at": now + ttl_seconds,
                         "path": f"/invite/{token}"})


@app.route("/invite/list")
def route_invite_list():
    with state_lock:
        _prune_expired_invites()
        save_state()
        invites = [{"token": t, **inv} for t, inv in state["invites"].items()]
    invites.sort(key=lambda i: i["created_at"], reverse=True)
    return jsonify({"invites": invites})


@app.route("/invite/<token>/revoke", methods=["POST"])
def route_invite_revoke(token):
    with state_lock:
        state["invites"].pop(token, None)
        save_state()
    return jsonify({"ok": True})


@app.route("/invite/<token>")
def route_invite_consume(token):
    with state_lock:
        _prune_expired_invites()
        valid = token in state["invites"]
    if valid:
        return redirect(_request_page_url(), code=302)
    return ("""<!doctype html><html><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Jamidrome</title>
        <style>body{background:#0d0d12;color:#f0f0f8;font-family:sans-serif;
        display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
        text-align:center;padding:24px}</style></head>
        <body><div><h2>This invite link has expired</h2>
        <p style="color:#9090b0">Ask whoever's hosting for a fresh one.</p></div></body></html>""", 404)


load_state()
threading.Thread(target=download_worker_loop, daemon=True).start()
threading.Thread(target=watchdog_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
