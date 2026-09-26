import difflib, glob, io, json, os, re, secrets, shlex, shutil, subprocess, sys, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
import requests as http
from flask import Flask, jsonify, request, send_file, abort, redirect, Response
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TCON, COMM, error as ID3Error
from ytmusicapi import YTMusic
import qrcode

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

# Genre lookup needs Spotify — reuses the SAME cached OAuth token Spotidrome
# already keeps refreshed at this path (both apps share the ~/.ssh bind
# mount), rather than Jamidrome needing its own separate login flow.
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = os.environ.get("SPOTIFY_REDIRECT_URI", "http://localhost:8080/callback")
SPOTIFY_CACHE_PATH    = "/root/.ssh/.spotify_cache"

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
SKIP_VOTE_THRESHOLD  = 1     # a single vote skips — no accounts/presence
                             # tracking to compute a real quorum against, and
                             # requiring several people to agree was more
                             # friction than the feature was worth.
# Set when the request/player pages sit behind a reverse proxy on separate
# hostnames (e.g. jam.example.com / aanvragenjam.example.com) rather than
# being reached directly on :9999/:9998 — there's no way to derive one
# hostname from the other in that setup, so it has to be configured.
REQUEST_PAGE_URL     = os.environ.get("REQUEST_PAGE_URL", "").rstrip("/")
MIN_INVITE_TTL_SEC   = 60
MAX_INVITE_TTL_SEC   = 7 * 24 * 3600

# This app has no accounts of any kind otherwise — a single shared PIN, set
# once via .env and known only to whoever's actually running the jam, is the
# entire moderation boundary for the /host/* routes below. Deliberately not
# more than that: real accounts would be pure overhead for a living-room
# party app that already treats "has the QR link" as "trusted enough to
# request/vote."
JAM_HOST_PIN = os.environ.get("JAM_HOST_PIN", "")

# Once the jam is reachable from the internet, "can reach the page" no longer
# means "was invited" — every guest route requires a live invite token (or
# host auth). Set JAM_REQUIRE_INVITE=0 to go back to the old open-LAN mode.
JAM_REQUIRE_INVITE = os.environ.get("JAM_REQUIRE_INVITE", "1").strip().lower() not in ("0", "false", "no", "")

# Host login for apps (LunaDrome): Navidrome credentials, verified against
# Navidrome itself, exchanged for a long-lived random host token. Empty
# JAM_HOST_USERS = any Navidrome *admin* may host; otherwise a comma-separated
# allowlist of usernames.
JAM_HOST_USERS = {u.strip().lower() for u in os.environ.get("JAM_HOST_USERS", "").split(",") if u.strip()}
HOST_TOKEN_TTL_SEC = 90 * 24 * 3600

# Only one device plays the jam at a time (the "speaker"). It heartbeats
# every few seconds; if it goes quiet for this long, the jam pauses right
# where it was instead of the watchdog playing songs into the void.
SPEAKER_TIMEOUT_SEC = 20

REACTION_EMOJIS      = {"🔥", "❤️", "😂", "👏", "🎉", "💃"}
REACTION_COOLDOWN_SEC = 1.5   # per session — keeps one person from flooding
                              # the shared screen with a single held-down tap
MAX_REACTIONS_KEPT   = 60

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ─── State ───────────────────────────────────────────────────────────────────
# queue:  ordered, not-yet-finished items — status one of
#         queued -> downloading -> ready -> playing
# history: finished (done/failed) items, most recent last, capped at MAX_HISTORY
# invites: token -> {created_at, expires_at, ttl_seconds} — share links minted
#          from the player page; each one is independent (generating a new
#          one does not invalidate earlier ones) and reusable by anyone who
#          has it until it expires.
# banned_sessions: session_ids the host has kicked — checked wherever a
#          session_id could otherwise request/upvote/skip-vote; doesn't
#          revoke anything already queued, just blocks new interactions.
# host_tokens: token -> {user, label, created_at, expires_at} — minted by
#          /host/login for apps that authenticated with Navidrome creds or the PIN.
# speaker: {device_id, name, last_seen} of the one device currently playing
#          the jam, or None — see SPEAKER_TIMEOUT_SEC.
# pause_reason: "host" (someone pressed pause) or "no_speaker" (auto-pause);
#          only the latter is undone automatically when a speaker claims.
state_lock = threading.Lock()
state = {"queue": [], "history": [], "now_playing_id": None, "playback_started_at": None, "invites": {},
         "paused": False, "pause_started_at": None, "autoplay_enabled": True, "banned_sessions": [],
         "host_tokens": {}, "speaker": None, "pause_reason": None}

# Live emoji reactions fired from the request page and animated over the
# player page — deliberately kept out of `state`/state.json entirely rather
# than persisted: they're meant to feel like a live crowd reaction, not
# something that should survive a backend restart or show up in history.
reactions_lock = threading.Lock()
reactions = []           # rolling list of {seq, emoji, ts}, newest last
_reaction_seq = 0
_last_reaction_at = {}   # session_id -> unix time of their last accepted reaction


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
    state["paused"] = False
    state["pause_started_at"] = None
    state["pause_reason"] = None
    # Whatever device was the speaker has to claim again after a restart.
    state["speaker"] = None
    state.setdefault("host_tokens", {})
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


def _prune_expired_host_tokens():
    """Must be called with state_lock held."""
    now = time.time()
    tokens = state.setdefault("host_tokens", {})
    for t in [t for t, v in tokens.items() if v.get("expires_at", 0) <= now]:
        del tokens[t]


def _is_host():
    """Host = a valid host token (header, or ?key= for <audio src> which
    can't send headers) or the shared PIN header the web player page uses."""
    pin = request.headers.get("X-Jam-Host-Pin", "")
    if JAM_HOST_PIN and pin and secrets.compare_digest(pin, JAM_HOST_PIN):
        return True
    token = request.headers.get("X-Jam-Host-Token", "") or request.args.get("key", "")
    if not token:
        return False
    with state_lock:
        info = (state.get("host_tokens") or {}).get(token)
        return bool(info) and info.get("expires_at", 0) > time.time()


def _require_host():
    """Gate for every host-only route. Returns a Flask response to abort
    the request with, or None to let it proceed."""
    if _is_host():
        return None
    return jsonify({"error": "Host login required"}), 401


# Kept for the existing /host/* moderation routes below.
_require_host_pin = _require_host


def _invite_token_from_request():
    return request.headers.get("X-Jam-Invite", "") or request.args.get("invite", "")


def _require_guest():
    """Gate for guest routes (search, request, vote, react): a live invite
    token or host auth. Open to anyone when JAM_REQUIRE_INVITE is off."""
    if not JAM_REQUIRE_INVITE or _is_host():
        return None
    token = _invite_token_from_request()
    if token:
        with state_lock:
            _prune_expired_invites()
            if token in state["invites"]:
                return None
    return jsonify({"error": "This jam needs a valid invite link", "invite_required": True}), 401


def _verify_navidrome_user(username, password=None, token=None, salt=None):
    """Checks the given Navidrome credentials against Navidrome itself and
    returns (ok, error). Also enforces who may host: JAM_HOST_USERS if set,
    otherwise Navidrome admins only."""
    if not NAVIDROME_URL:
        return False, "NAVIDROME_URL isn't configured"
    params = {"u": username, "v": "1.16.1", "c": "jamidrome", "f": "json"}
    if token and salt:
        params.update(t=token, s=salt)
    elif password:
        params["p"] = password
    else:
        return False, "Missing credentials"
    try:
        resp = http.get(f"{NAVIDROME_URL}/rest/getUser", params={**params, "username": username}, timeout=10)
        body = resp.json().get("subsonic-response", {})
    except Exception:
        return False, "Couldn't reach Navidrome"
    if body.get("status") != "ok":
        return False, (body.get("error") or {}).get("message") or "Wrong username or password"
    user = body.get("user") or {}
    if JAM_HOST_USERS:
        if username.lower() not in JAM_HOST_USERS:
            return False, "This account isn't allowed to host the jam"
    elif not user.get("adminRole"):
        return False, "Only Navidrome admins can host the jam"
    return True, None


def _speaker_alive():
    """Must be called with state_lock held."""
    sp = state.get("speaker")
    return bool(sp) and time.time() - sp.get("last_seen", 0) <= SPEAKER_TIMEOUT_SEC


def _pause_locked(reason):
    """Must be called with state_lock held."""
    if state["now_playing_id"] and not state["paused"]:
        state["paused"] = True
        state["pause_started_at"] = time.time()
        state["pause_reason"] = reason


def _resume_locked():
    """Must be called with state_lock held."""
    if state["paused"] and state["pause_started_at"] is not None:
        paused_for = time.time() - state["pause_started_at"]
        if state["playback_started_at"] is not None:
            # Shifts the "clock" forward by however long it was paused,
            # so elapsed = now - playback_started_at keeps meaning
            # "how much of the track has actually played", not
            # counting the paused interval as elapsed playback time.
            state["playback_started_at"] += paused_for
    state["paused"] = False
    state["pause_started_at"] = None
    state["pause_reason"] = None


def _is_banned(session_id):
    return bool(session_id) and session_id in (state.get("banned_sessions") or [])


def public_view(item, include_stream=False, session_id=None):
    v = {k: item.get(k) for k in ("id", "video_id", "title", "artist", "thumbnail", "album", "genre",
                                   "duration", "status", "requested_by", "added_at", "progress")}
    v["navidrome_song_id"] = item.get("navidrome_song_id")
    v["source"] = "library" if str(item.get("video_id") or "").startswith("nd:") else "youtube"
    upvotes = item.get("upvotes") or []
    v["votes"] = len(upvotes)
    v["voted_by_me"] = bool(session_id) and session_id in upvotes
    if item.get("status") == "playing":
        skip_votes = item.get("skip_votes") or []
        v["skip_votes"] = len(skip_votes)
        v["skip_threshold"] = SKIP_VOTE_THRESHOLD
        v["voted_skip_by_me"] = bool(session_id) and session_id in skip_votes
    if include_stream:
        # /api/ prefix matters: nginx only proxies paths under /api/ to this
        # backend on both ports — everything else falls through to its SPA
        # catch-all and serves player.html back instead (200 OK, text/html).
        # A bare "/stream/<id>" here silently handed the <audio> element an
        # HTML page instead of audio, which is why playback never worked.
        v["stream_url"] = f"/api/stream/{item['id']}"
    return v


# ─── Navidrome duplicate check ──────────────────────────────────────────────
# Title/artist matching adapted from Spotidrome's own candidate-vetting
# logic (same tolerances) — not called into at runtime (Jamidrome stays a
# standalone app), just the same proven approach re-implemented here so a
# jam request doesn't trigger a redundant download of a track that's
# already sitting in the library under its properly-tagged name.

def _normalize_title(s):
    s = (s or "").lower()
    s = re.sub(r"\(feat\.?[^)]*\)|\[feat\.?[^\]]*\]", "", s)
    s = re.sub(r"\((remaster(ed)?[^)]*|official[^)]*|lyric[^)]*|audio)\)", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _title_close_enough(candidate_title, expected_title):
    cand_n = _normalize_title(candidate_title)
    exp_n = _normalize_title(expected_title)
    if not exp_n or not cand_n:
        return False
    if exp_n in cand_n or cand_n in exp_n:
        return True
    if difflib.SequenceMatcher(None, cand_n, exp_n).ratio() >= 0.72:
        return True
    words = [w for w in exp_n.split() if len(w) > 2]
    return bool(words) and sum(1 for w in words if w in cand_n) / len(words) >= 0.6

def _split_camel_case(s):
    """Insert spaces at lower->upper transitions — YouTube channel names
    very often glue the artist name directly onto a suffix with no
    separator at all (e.g. 'LuisFonsiVEVO', 'SkilletMusic'), which would
    otherwise tokenize as one solid word that can't overlap with anything
    ('luisfonsivevo' shares no words with 'luis fonsi') even though the
    channel is unambiguously that artist."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s or "")

def _artist_close_enough(candidate_artist, expected_artist):
    """candidate_artist is Navidrome's clean tagged artist; expected_artist
    is whatever came off YouTube (channel/uploader name), which is often
    noisier — e.g. "Bonnie Tyler Official" or "Bonnie Tyler - Topic" for a
    clean "Bonnie Tyler" tag. Checked as word-overlap in both directions
    rather than requiring either string to contain the other whole, since
    neither side is reliably the "clean" one to substring-match against."""
    cand = re.sub(r"\s*-\s*topic$", "", _split_camel_case(candidate_artist or "").lower()).strip()
    exp = _split_camel_case(expected_artist or "").lower().strip()
    if not cand or not exp:
        return not cand and not exp
    if cand in exp or exp in cand:
        return True
    cand_words = {w for w in re.split(r"[^\w]+", cand) if len(w) > 2}
    exp_words = {w for w in re.split(r"[^\w]+", exp) if len(w) > 2}
    if not cand_words or not exp_words:
        return False
    smaller = min(len(cand_words), len(exp_words))
    return len(cand_words & exp_words) / smaller >= 0.6

def _duration_close_enough(candidate_sec, expected_sec, pct=0.15, floor=15):
    if not expected_sec:
        return True  # nothing to compare against — don't penalize
    if not candidate_sec:
        return False
    return abs(candidate_sec - expected_sec) <= max(floor, expected_sec * pct)

def check_navidrome_duplicate(title, artist, duration_sec):
    """Search Navidrome's own library via the Subsonic API for a song that's
    already a good match for this request. Returns {title, artist, duration,
    navidrome_song_id} if found, else None — the caller uses navidrome_song_id
    to stream the existing copy directly instead of downloading a new one.
    Best-effort: any failure (Navidrome unreachable, not configured, etc.) is
    treated as 'no match found', never as a reason to block the request."""
    if not (NAVIDROME_URL and NAVIDROME_USER):
        return None
    query = _normalize_title(title)[:60] or (title or "")[:60]
    try:
        resp = http.get(f"{NAVIDROME_URL}/rest/search3", params={
            "query": query, "songCount": 20, "albumCount": 0, "artistCount": 0,
            "u": NAVIDROME_USER, "p": NAVIDROME_PASSWORD, "v": "1.16.1",
            "c": "jamidrome", "f": "json",
        }, timeout=10)
        resp.raise_for_status()
        songs = (resp.json().get("subsonic-response", {})
                              .get("searchResult3", {}).get("song", []))
    except Exception:
        return None

    for song in songs:
        cand_title = song.get("title", "")
        cand_artist = song.get("artist", "")
        cand_duration = song.get("duration")
        if not _title_close_enough(cand_title, title):
            continue
        if not _artist_close_enough(cand_artist, artist):
            continue
        if not _duration_close_enough(cand_duration, duration_sec):
            continue
        return {"title": cand_title, "artist": cand_artist, "duration": cand_duration,
                "navidrome_song_id": song.get("id")}
    return None


# ─── Tagging & genre lookup ──────────────────────────────────────────────────
# Copied from Spotidrome (not called into it — Jamidrome stays standalone,
# no runtime dependency on it being up) so a jam-requested track gets
# exactly the same real genre tag and "Unknown Album" correction a normal
# Spotidrome-synced track does, rather than being left with whatever
# generic tags YouTube's own embedded metadata provides (genre always just
# "Music"; album usually empty or the video's own title).

def sanitize(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def primary_artist(artist):
    return (artist or "").split(",")[0].strip()

def get_sp():
    """Reuses Spotidrome's own cached Spotify OAuth token (same cache file,
    same ~/.ssh bind mount) rather than Jamidrome needing its own separate
    login flow — Spotidrome already keeps it refreshed for its own genre
    lookups. Returns None if there's no cached token yet (Spotify was never
    connected through Spotidrome) or refresh fails; either way, genre
    lookup just falls back to the YouTube-tag method below."""
    try:
        auth = SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri=SPOTIFY_REDIRECT_URI,
            scope="playlist-read-private playlist-read-collaborative user-library-read",
            cache_path=SPOTIFY_CACHE_PATH, open_browser=False)
        token = auth.get_cached_token()
        if not token:
            return None
        if auth.is_token_expired(token):
            token = auth.refresh_access_token(token["refresh_token"])
        return spotipy.Spotify(auth=token["access_token"])
    except Exception as e:
        print(f"[jam] Spotify auth unavailable: {e}", file=sys.stderr)
        return None

YT_GENRE_KEYWORDS = {
    "drum and bass", "drum n bass", "dnb", "death metal", "black metal",
    "thrash metal", "heavy metal", "nu metal", "metalcore", "deathcore",
    "hard rock", "soft rock", "hip hop", "hip-hop", "r&b", "rnb", "k-pop",
    "j-pop", "new age", "synthwave", "lo-fi", "lofi", "drill", "grime",
    "rock", "pop", "metal", "rap", "soul", "jazz", "blues", "country",
    "folk", "classical", "electronic", "house", "techno", "trance",
    "dubstep", "reggae", "ska", "punk", "indie", "alternative", "grunge",
    "emo", "funk", "disco", "gospel", "ambient", "edm", "garage", "opera",
    "latin", "soundtrack",
}

# yt-dlp's --add-metadata embeds the source video's own YouTube *category*
# ("Music", "People & Blogs", "Gaming"...) straight into the GENRE tag —
# a much coarser classification than a music genre (every music video is
# generically "Music"), and not something lookup_genre()/YT_GENRE_KEYWORDS
# above ever produces itself — it's already sitting in the file before any
# of this app's own genre logic runs. fix_tags() clears it on sight rather
# than leaving it in place whenever a real genre can't be found to replace
# it with (see the same fix in Spotidrome, made after exactly this ended
# up as a real track's permanent "genre").
YT_VIDEO_CATEGORIES = {
    "film & animation", "autos & vehicles", "music", "pets & animals",
    "sports", "short movies", "travel & events", "gaming", "videoblogging",
    "people & blogs", "comedy", "entertainment", "news & politics",
    "howto & style", "education", "science & technology",
    "nonprofits & activism", "movies", "anime/animation", "action/adventure",
    "classics", "documentary", "drama", "family", "foreign", "horror",
    "sci-fi/fantasy", "thriller", "shorts", "shows", "trailers",
}

def lookup_genre_from_youtube(artist):
    key = primary_artist(artist).strip()
    if not key:
        return None
    cmd = ["yt-dlp", "--dump-json", "--no-playlist",
           "--default-search", "https://music.youtube.com/search?q=",
           f"ytsearch3:{key}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if result.returncode != 0 or not result.stdout.strip():
            return None
    except Exception:
        return None
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            info = json.loads(line)
        except Exception:
            continue
        for tag in (info.get("tags") or []):
            normalized = re.sub(r"[^a-z0-9&\- ]", "", tag.lower()).strip()
            if normalized in YT_GENRE_KEYWORDS:
                return normalized.title()
    return None

_genre_cache = {}
_genre_cache_lock = threading.Lock()

def lookup_genre(artist):
    """Spotify's catalog first, falling back to YouTube tags when Spotify
    has nothing for this artist. Cached per artist for the process's life."""
    if not artist:
        return None
    key = primary_artist(artist).lower()
    if not key:
        return None
    with _genre_cache_lock:
        if key in _genre_cache:
            return _genre_cache[key]
    genre = None
    sp = get_sp()
    if sp:
        try:
            result = sp.search(q=f"artist:{key}", type="artist", limit=1)
            items = result.get("artists", {}).get("items", [])
            if items:
                genres = items[0].get("genres") or []
                if genres:
                    genre = genres[0].title()
        except Exception as e:
            print(f"[jam] Spotify genre lookup failed for {artist!r}: {e}", file=sys.stderr)
    if not genre:
        try:
            genre = lookup_genre_from_youtube(artist)
        except Exception as e:
            print(f"[jam] YouTube genre fallback failed for {artist!r}: {e}", file=sys.stderr)
    with _genre_cache_lock:
        _genre_cache[key] = genre
    return genre

def fix_tags(filepath, title, artist, album, album_artist=None, source_url=None, genre=None):
    album_artist = album_artist or artist
    try:
        tags = FLAC(filepath)
        tags["title"] = [title]
        tags["artist"] = [artist]
        tags["album"] = [album]
        tags["albumartist"] = [album_artist]
        if source_url:
            tags["comment"] = [source_url]
        if genre:
            tags["genre"] = [genre]
        elif (tags.get("genre") or [""])[0].strip().lower() in YT_VIDEO_CATEGORIES:
            # No real genre to set, but what's already there is a raw
            # YouTube video category yt-dlp auto-embedded, not a music
            # genre at all — clear it rather than keep it just because
            # nothing better was found.
            del tags["genre"]
        tags.save()
    except Exception as e:
        print(f"[jam] Tag fix failed for {filepath}: {e}", file=sys.stderr)

BAD_ALBUM_VALUES = {"", "unknown album"}

def lookup_real_album(url, timeout=15):
    """Ask yt-dlp for the real album/release of a track, without downloading it."""
    if not url:
        return None
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-playlist", "--skip-download",
               "--socket-timeout", "10", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        info = json.loads(result.stdout.strip().split("\n")[0])
        album = (info.get("album") or info.get("release") or "").strip()
        return album or None
    except Exception:
        return None

def lookup_album_from_spotify(title, artist):
    """Spotify's own catalog, searched by title+artist — a much more
    reliable album source than yt-dlp's embedded video metadata for
    Jamidrome's case specifically: unlike Spotidrome (which starts from a
    real Spotify track and only falls back to yt-dlp's metadata in the
    rare case Spotify's own album field is somehow missing), every
    Jamidrome request starts from a plain YouTube search with no Spotify
    data at all, and plenty of real videos — lyric videos, "visualizer"
    uploads, fan uploads — simply don't carry album/release tags for
    yt-dlp to find, even though the track is unambiguously a real,
    catalogued release."""
    sp = get_sp()
    if not sp:
        return None
    try:
        # A field-qualified "track:"/"artist:" filter is too strict here —
        # Jamidrome's "artist" is often just a YouTube channel name (a
        # VEVO channel, a cover/compilation channel, a name with no space
        # before a suffix at all — "LuisFonsiVEVO" for "Luis Fonsi") rather
        # than the real Spotify artist name, so a strict filter reliably
        # finds nothing for those. Free-text search instead, then vet each
        # candidate with the exact same title/artist fuzzy-matching this
        # app already uses for everything else, rather than trusting
        # Spotify's own top result blindly.
        query = f"{_normalize_title(title)} {primary_artist(artist)}".strip()
        result = sp.search(q=query, type="track", limit=5)
        for item in result.get("tracks", {}).get("items", []):
            if not _title_close_enough(item.get("name", ""), title):
                continue
            if not any(_artist_close_enough(a.get("name", ""), artist)
                       for a in item.get("artists", [])):
                continue
            name = (item.get("album") or {}).get("name")
            if name and name.strip():
                return name.strip()
    except Exception as e:
        print(f"[jam] Spotify album lookup failed for {title!r}/{artist!r}: {e}", file=sys.stderr)
    return None

def maybe_correct_album(flac_path, title, artist, album, playlist_name, source_url, local_dir, album_artist=None):
    """If album looks like a placeholder (empty/'Unknown Album'/the playlist
    name itself), look up the real album — Spotify's catalog first, then
    yt-dlp's own video metadata as a fallback — and move the file into the
    corrected album folder. Returns (album, flac_path), updated if
    corrected."""
    normalized = (album or "").strip().lower()
    if normalized not in BAD_ALBUM_VALUES and normalized != (playlist_name or "").strip().lower():
        return album, flac_path
    real_album = lookup_album_from_spotify(title, artist) or lookup_real_album(source_url)
    if not real_album or real_album.strip().lower() == normalized:
        return album, flac_path
    try:
        new_album_dir = os.path.join(local_dir, sanitize(real_album))
        os.makedirs(new_album_dir, exist_ok=True)
        new_path = os.path.join(new_album_dir, os.path.basename(flac_path))
        if os.path.abspath(new_path) != os.path.abspath(flac_path):
            shutil.move(flac_path, new_path)
        fix_tags(new_path, title, artist, real_album, album_artist=album_artist, source_url=source_url)
        return real_album, new_path
    except Exception as e:
        print(f"[jam] Album correction failed for {flac_path}: {e}", file=sys.stderr)
        return album, flac_path


# ─── yt-dlp search & download ───────────────────────────────────────────────

_ytmusic_client = None
_ytmusic_disabled = False
# gunicorn here is one process with many threads (--workers 1 --threads 16
# — deliberately: the whole queue/playback state lives in this one
# process's memory, so more worker *processes* would each get their own
# disconnected copy of it). That means this client, and whatever HTTP
# session ytmusicapi keeps internally, is genuinely shared across every
# concurrent request. Auto DJ's background thread calls into it completely
# independently of (and now, with autofill running roughly every 2-20s,
# increasingly likely to overlap with) a manual /search request's own
# thread — unsynchronized concurrent use of that shared session was
# segfaulting the entire process (every open connection, including
# whatever anyone was actively listening to, dropped at once — "skips
# every track" from the outside), not just raising an ordinary, catchable
# Python exception. Held around both construction and every actual call
# into the client.
_ytmusic_lock = threading.Lock()

def _get_ytmusic():
    """Lazily construct a shared YTMusic client, matching Spotidrome's own
    pattern: if construction ever fails (e.g. no network at startup),
    disable it for the rest of the process rather than retrying on every
    single search."""
    global _ytmusic_client, _ytmusic_disabled
    if _ytmusic_disabled:
        return None
    with _ytmusic_lock:
        if _ytmusic_client is None:
            try:
                _ytmusic_client = YTMusic()
            except Exception as e:
                print(f"[jam] YTMusic init failed, disabling: {e}", file=sys.stderr)
                _ytmusic_disabled = True
                return None
        return _ytmusic_client

def _search_ytmusic_songs(query, limit, timeout=12):
    """YouTube Music's own 'songs' category — YouTube's own classification
    of a result as an actual released track, curated to specifically
    exclude covers, reuploads, lyric videos, and live performances (Spotify
    catalog data isn't involved here at all — this is YouTube Music's own
    metadata, the same signal Spotidrome uses to vet its download
    candidates). Run with a hard timeout via a background thread since
    ytmusicapi's HTTP calls have no timeout of their own."""
    ytm = _get_ytmusic()
    if not ytm:
        return []
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with _ytmusic_lock:  # serialize against every other caller of ytm — see _ytmusic_lock above
            future = executor.submit(ytm.search, query, filter="songs", limit=limit)
            results = future.result(timeout=timeout)
    except Exception:
        return []
    finally:
        executor.shutdown(wait=False)

    # YT Music's own search matches loosely against the whole query, so a
    # query like "linkin park faint" happily returns Numb/Crawling/Papercut
    # too — anything by the matched artist, not just the song actually
    # being searched for. Subtracting the query's own artist-name words
    # leaves (roughly) just the song-title part the user typed, and each
    # candidate's title needs to actually relate to that remainder —
    # unless there's no remainder at all, i.e. the query was just an
    # artist name with no particular song in mind, which should keep
    # matching everything by them.
    def words(s):
        return {w for w in re.split(r"[^\w]+", (s or "").lower()) if len(w) > 2}

    query_words = words(query)

    out = []
    for r in results or []:
        video_id = r.get("videoId")
        if not video_id:
            continue
        artists = ", ".join(a.get("name", "") for a in (r.get("artists") or []) if a.get("name"))
        title = r.get("title") or "Unknown title"
        leftover = query_words - words(artists)
        if leftover and not (leftover & words(title)):
            continue  # not actually related to what was searched
        album = (r.get("album") or {}).get("name")
        out.append({
            "video_id": video_id,
            "title": title,
            "artist": artists or "Unknown artist",
            "album": album,
            "duration": r.get("duration_seconds"),
            # Real video thumbnail rather than ytmusicapi's own (a small,
            # low-res channel-icon-style image) — same CDN path the plain
            # yt-dlp search results below already use, for visual consistency.
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "url": f"https://music.youtube.com/watch?v={video_id}",
        })
    return out

def _search_yt_dlp(query, limit):
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

def search_tracks(query, limit=SEARCH_RESULT_COUNT):
    """YouTube Music's 'songs' results go first — the real release, not a
    reupload/cover/lyric video/live performance — with plain YouTube
    search filling in the rest (deduped by video_id) so covers, live
    versions, etc. are still findable, just never crowding out the real
    thing at the top."""
    songs = _search_ytmusic_songs(query, limit)
    seen = {r["video_id"] for r in songs}
    remaining = max(0, limit - len(songs))
    generic = _search_yt_dlp(query, limit) if remaining else []
    for r in generic:
        if r["video_id"] not in seen:
            songs.append(r)
            seen.add(r["video_id"])
    return songs[:limit]


def _parse_length_to_seconds(length):
    """YT Music's get_watch_playlist reports each track's length as an
    'M:SS' or 'H:MM:SS' string (its own duration_seconds field is
    unreliable — often just None, as seen above) rather than a number."""
    if not length:
        return None
    parts = str(length).split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds or None


def get_similar_tracks(seed_video_id, limit=20, timeout=12):
    """YouTube Music's own 'radio' continuation for a track — the same
    'up next' logic YT Music's own autoplay uses, so what comes back is
    genuinely stylistically similar rather than just 'more by this
    artist'. Same lazy client / hard-timeout pattern as _search_ytmusic_songs,
    since ytmusicapi's HTTP calls have no timeout of their own. Excludes the
    seed track itself, which get_watch_playlist otherwise echoes back as
    the first entry."""
    ytm = _get_ytmusic()
    if not ytm:
        return []
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with _ytmusic_lock:  # serialize against every other caller of ytm — see _ytmusic_lock above
            future = executor.submit(ytm.get_watch_playlist, videoId=seed_video_id, limit=limit)
            result = future.result(timeout=timeout)
    except Exception as e:
        print(f"[jam] get_watch_playlist failed for seed {seed_video_id}: {e}", file=sys.stderr)
        return []
    finally:
        executor.shutdown(wait=False)

    out = []
    for t in (result or {}).get("tracks") or []:
        video_id = t.get("videoId")
        if not video_id or video_id == seed_video_id:
            continue
        artists = ", ".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name"))
        out.append({
            "video_id": video_id,
            "title": t.get("title") or "Unknown title",
            "artist": artists or "Unknown artist",
            "album": (t.get("album") or {}).get("name") if isinstance(t.get("album"), dict) else None,
            "duration": _parse_length_to_seconds(t.get("length")),
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "url": f"https://music.youtube.com/watch?v={video_id}",
        })
    return out


# ─── Autoplay ("Auto DJ") ────────────────────────────────────────────────────
# When the queue runs completely dry, the jam shouldn't just go silent —
# pick something similar to whatever played last and queue it exactly like
# a real request, same download/tag/duplicate-check pipeline and all. Off
# by default whenever there's nothing to seed it from (a brand new jam that
# nobody's requested anything in yet); toggleable from the player page.

_autofill_lock = threading.Lock()  # separate from state_lock: held for the
                                    # whole (slow, network-bound) autofill
                                    # attempt, just to stop two overlapping
_autofill_running = False          # attempts rather than every state read
_autofill_next_attempt_at = 0      # backoff after a fruitless attempt, so a
                                    # run of "found nothing new" doesn't retry
                                    # every 2 seconds forever

AUTOFILL_COOLDOWN_SEC = 20
AUTOFILL_HISTORY_AVOID = 25  # don't re-suggest anything played this recently
# Auto DJ runs unattended, indefinitely, for as long as nobody's actively
# steering the jam — with no cap of its own, that's an unbounded download
# loop syncing a fresh file into the Navidrome library every time it
# fires. Over days of a quiet jam nobody was watching, that alone added
# ~19GB (285 tracks) to a library that turned out to already be sitting
# near capacity, and was enough to actually fill the disk solid — at
# which point Navidrome couldn't write *anything* (its own DB, its own
# transcode cache), which looked exactly like "every track just skips"
# from the outside, because it effectively was: nothing could actually
# stream. A human requesting songs is inherently self-limiting; this
# background loop isn't, so it gets its own explicit guard instead.
MIN_FREE_DISK_GB_FOR_AUTOFILL = 5


def _autofill_seed_video_id():
    """Must be called with state_lock held. Whatever's playing right now if
    anything is (the most locally-relevant seed — matters once autofill can
    fire pre-emptively, before that track has even finished), else the
    most-recently-played track from history. A real video_id either way —
    every track has one, even a Navidrome-match one; see _enqueue_track.
    None if this jam has no history yet at all and nothing is playing."""
    cur = find_item(state["now_playing_id"]) if state["now_playing_id"] else None
    for item in ([cur] if cur else []) + list(reversed(state["history"])):
        seed = _youtube_video_id(item)
        if seed:
            return seed
    return None


def _youtube_video_id(item):
    """A track added from the Navidrome library has a synthetic "nd:<id>"
    video_id; Auto DJ can only seed from a real YouTube id, which gets
    resolved in the background for those (yt_video_id) — see
    _resolve_youtube_id_bg."""
    vid = item.get("yt_video_id") or item.get("video_id") or ""
    return None if vid.startswith("nd:") else (vid or None)


def _navidrome_host_free_gb():
    """Best-effort free space (GB) on the Navidrome host's music
    filesystem. Returns None if the check itself fails for any reason
    (SSH hiccup, not configured, etc.) — callers should treat that as
    'unknown, don't block on it', not as 'definitely full'; this is a
    guard against Auto DJ's own unattended growth, not a general-purpose
    health check that should ever stop a real human's request."""
    if not (SSH_HOST and SSH_USER):
        return None
    try:
        cmd = _ssh_cmd(f"df --output=avail -B1 {shlex.quote(SSH_MUSIC_PATH)} | tail -1")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        return int(result.stdout.strip()) / (1024 ** 3)
    except Exception:
        return None


def maybe_trigger_autofill():
    """Cheap check called from the watchdog loop every couple of seconds;
    the actual network-bound work only happens in a spawned thread, and
    only when nothing is already in flight.

    Fires whenever nothing is lined up *behind* whatever's currently
    playing — not only once the jam goes fully idle. Firing only on full
    idle meant a real silence gap every time: the current track ends,
    now_playing goes empty, autofill starts a search+download that can
    itself take 10-60s, and only then does anything play. Checking for
    "no upcoming item" instead lets it prefetch and download the next
    pick *while* the current one is still playing, so it's usually
    already sitting there ready by the time it's actually needed."""
    global _autofill_running, _autofill_next_attempt_at
    with state_lock:
        if not state.get("autoplay_enabled", True):
            return
        upcoming = [i for i in state["queue"] if i["status"] != "playing"]
        if upcoming:
            return
        seed = _autofill_seed_video_id()
    if not seed or time.time() < _autofill_next_attempt_at:
        return
    free_gb = _navidrome_host_free_gb()
    if free_gb is not None and free_gb < MIN_FREE_DISK_GB_FOR_AUTOFILL:
        print(f"[jam] Auto DJ paused — only {free_gb:.1f}GB free on the Navidrome host "
              f"(need {MIN_FREE_DISK_GB_FOR_AUTOFILL}GB); a real request will still go through.",
              file=sys.stderr)
        _autofill_next_attempt_at = time.time() + AUTOFILL_COOLDOWN_SEC * 15  # check back in ~5 min, not every 20s
        return
    if not _autofill_lock.acquire(blocking=False):
        return
    _autofill_running = True
    threading.Thread(target=_run_autofill, args=(seed,), daemon=True).start()


def _run_autofill(seed_video_id):
    global _autofill_next_attempt_at, _autofill_running
    try:
        with state_lock:
            avoid_ids = {i["video_id"] for i in state["history"][-AUTOFILL_HISTORY_AVOID:] if i.get("video_id")}
            avoid_ids |= {i["video_id"] for i in state["queue"]}
            older_history = [i["video_id"] for i in state["history"][:-AUTOFILL_HISTORY_AVOID]
                              if i.get("video_id")]
        candidates = get_similar_tracks(seed_video_id, limit=20)
        pick = next((c for c in candidates if c["video_id"] not in avoid_ids), None)
        # The seed is always "whatever's playing/just played" — over a long
        # unattended jam, that one seed's ~20 "similar tracks" can all end
        # up already recently played, and since the seed itself doesn't
        # change between attempts, retrying just keeps re-fetching the
        # exact same exhausted pool every 20s forever rather than actually
        # finding anything new. Falling back to a *different* seed pulled
        # from further back in history (still real history, just old
        # enough to be outside the recent-repeat window) gives it a fresh
        # pool to draw from instead of getting stuck.
        if not pick and older_history:
            fallback_seed = secrets.choice(older_history)
            if fallback_seed != seed_video_id:
                fallback_candidates = get_similar_tracks(fallback_seed, limit=20)
                pick = next((c for c in fallback_candidates if c["video_id"] not in avoid_ids), None)
        if not pick:
            _autofill_next_attempt_at = time.time() + AUTOFILL_COOLDOWN_SEC
            return
        item, err = _enqueue_track(
            pick["video_id"], pick["url"], pick["title"], pick["artist"],
            duration=pick["duration"], thumbnail=pick["thumbnail"],
            known_album=pick.get("album") or "", requested_by="🔁 Auto DJ")
        if err:
            # Near-impossible (a fresh video_id colliding with the now-empty
            # queue), but don't hammer on it if it somehow happens.
            _autofill_next_attempt_at = time.time() + AUTOFILL_COOLDOWN_SEC
        else:
            print(f"[jam] Auto DJ queued: {pick['artist']} - {pick['title']}", flush=True)
    except Exception as e:
        print(f"[jam] Autofill failed: {e}", flush=True)
        _autofill_next_attempt_at = time.time() + AUTOFILL_COOLDOWN_SEC
    finally:
        _autofill_running = False
        _autofill_lock.release()


def ffprobe_duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", path],
                            capture_output=True, text=True, timeout=15)
        return float(r.stdout.strip())
    except Exception:
        return None


_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")

def _set_item_progress(item_id, pct):
    with state_lock:
        cur = find_item(item_id)
        if cur:
            cur["progress"] = pct

def _run_with_progress(cmd, item_id, timeout=180):
    """Like subprocess.run, but parses yt-dlp's own --newline progress
    output live and pushes each update straight into the queue item, so the
    request page can show a real progress bar instead of a static
    'downloading…' label. Returns an object with .returncode/.stderr,
    matching what the rest of download_track()'s error handling expects."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    lines = []
    try:
        for line in proc.stdout:
            lines.append(line)
            m = _PROGRESS_RE.search(line)
            if m:
                _set_item_progress(item_id, float(m.group(1)))
    finally:
        killer.cancel()
    proc.wait()
    result = type("Result", (), {})()
    result.returncode = proc.returncode
    result.stderr = "".join(lines)
    return result

def download_track(item):
    """Download, loudness-normalize, and tag straight to FLAC. Genre lookup
    and 'Unknown Album' correction reuse the exact same approach Spotidrome
    uses on its own downloads (see the Tagging & genre lookup section
    above) — copied in rather than called into it, so a jam-requested
    track ends up tagged exactly as well as a normal Spotidrome-synced one,
    not left with YouTube's own generic embedded metadata (genre always
    just "Music"; album usually empty)."""
    out_path = os.path.join(DOWNLOAD_DIR, f"{item['id']}.flac")
    _set_item_progress(item["id"], 0)

    def attempt(pot_args):
        cmd = ["yt-dlp",
               "-x", "--audio-format", "flac", "--audio-quality", "0",
               "--postprocessor-args", f"ExtractAudio:-af {LOUDNORM_FILTER}",
               "--add-metadata", "--embed-thumbnail",
               "--output", out_path.replace(".flac", ".%(ext)s"),
               "--no-playlist", "--retries", "2", "--fragment-retries", "2",
               "--socket-timeout", "10", "--newline",
               ] + pot_args + [item["url"]]
        return _run_with_progress(cmd, item["id"])

    r = attempt(YTDLP_POT_ARGS)
    needs_fallback = r.returncode != 0 or not os.path.exists(out_path)
    if needs_fallback and ("403" in (r.stderr or "") or "not available" in (r.stderr or "")):
        # Same fallback Spotidrome relies on: the mweb client occasionally
        # gets a probabilistic 403 from YouTube's anti-bot enforcement:
        # android routes around it, at the cost of a real quality drop
        # (legacy itag 18, ~96kbps AAC transcoded to FLAC) since android's
        # proper adaptive audio streams are themselves currently blocked by
        # a separate YouTube-side SABR restriction.
        _set_item_progress(item["id"], 0)  # fresh attempt, previous % is stale
        r = attempt(_pot_args_for_client("android"))

    if r.returncode != 0 or not os.path.exists(out_path):
        _cleanup_downloaded_files(item["id"])  # yt-dlp can leave behind a
        # partial audio file, thumbnail, etc. even on a failed run
        raise RuntimeError((r.stderr or "yt-dlp failed")[-300:])

    # Unlike Spotidrome's own algorithmic best-match search, a jam request
    # comes from a human picking an exact search result they could already
    # see the title/artist/thumbnail of — so the classic "matched a
    # completely different song" failure Spotidrome had to guard against
    # (see spotidrome-wrong-track-bugs notes) barely applies here. Still
    # worth a cheap sanity check: yt-dlp resolving that exact video_id to
    # something wildly longer or shorter than what search reported (a stale/
    # redirected id, a since-edited upload, an actual yt-dlp mismatch) is
    # rare but not impossible, and would otherwise silently become the new
    # "official" duration with nothing to flag it. Checked before fix_tags/
    # maybe_correct_album do any tagging or moving, so a rejected file never
    # gets either.
    duration = ffprobe_duration(out_path)
    expected_duration = item.get("duration")
    if expected_duration and duration and not _duration_close_enough(duration, expected_duration):
        _cleanup_downloaded_files(item["id"])
        raise RuntimeError(
            f"downloaded audio is {duration/60:.1f} min but the search result said "
            f"~{expected_duration/60:.1f} min — likely the wrong video")

    # If the pick came from the YT Music 'songs' search tier, its own real
    # album name is already known and authoritative — start from that
    # instead of empty, so maybe_correct_album's placeholder check sees a
    # real value and skips the guess-based lookup entirely.
    known_album = item.get("known_album") or ""
    genre = lookup_genre(item["artist"])
    fix_tags(out_path, item["title"], item["artist"], known_album, album_artist=item["artist"],
             source_url=item["url"], genre=genre)
    album, out_path = maybe_correct_album(
        out_path, item["title"], item["artist"], known_album, "Jam", item["url"], DOWNLOAD_DIR,
        album_artist=item["artist"])

    return out_path, duration, album, genre


def _cleanup_downloaded_files(item_id):
    # Recursive: maybe_correct_album can move the file into an
    # album-named subfolder within DOWNLOAD_DIR, so a flat glob on
    # DOWNLOAD_DIR itself would miss it after a correction.
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, "**", f"{item_id}.*"), recursive=True):
        try:
            os.remove(f)
        except Exception:
            pass


def _ssh_cmd(remote_command):
    return ["ssh", "-i", SSH_KEY, "-p", str(SSH_PORT),
            "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            f"{SSH_USER}@{SSH_HOST}", remote_command]


def sync_to_navidrome_bg(item):
    """Best-effort archival copy into the Jam/ folder — never blocks or
    affects playback, which already streams from the local download.
    Jam/ is a *rolling* archive, not permanent storage — see
    cleanup_jam_folder below; a jam left running unattended (Auto DJ)
    downloads indefinitely, and without a cap this is exactly what
    filled the Navidrome host's disk solid once already."""
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
            http.put(f"{NAVIDROME_URL}/api/scanner/trigger",
                      auth=(NAVIDROME_USER, NAVIDROME_PASSWORD), timeout=15)
    except Exception as e:
        print(f"[jam] Navidrome sync failed for {item['id']}: {e}", flush=True)


# ─── Jam/ folder cleanup ─────────────────────────────────────────────────────
# Every played track gets rsynced into Jam/ (see sync_to_navidrome_bg above)
# with nothing bounding how much accumulates — over days of Auto DJ running
# unattended, that's an unbounded download loop, and it's what actually
# filled the Navidrome host's disk solid once already (Auto DJ now checks
# free space before adding anything more — see MIN_FREE_DISK_GB_FOR_AUTOFILL
# — but that only stops it from getting *worse*; it doesn't reclaim
# anything already there, and normal human requests aren't capped at all
# either). This keeps Jam/ itself under a fixed size going forward,
# deleting the oldest files first once over — a rolling window rather than
# permanent storage.

JAM_FOLDER_MAX_BYTES = 5 * 1024 ** 3  # 5GB
JAM_CLEANUP_INTERVAL_SEC = 6 * 3600   # check every 6 hours
NAVIDROME_DB_PATH = "/var/lib/navidrome/navidrome.db"


def cleanup_jam_folder():
    """Deletes the oldest files in Jam/ until it's back under
    JAM_FOLDER_MAX_BYTES and triggers a full rescan. Best-effort throughout: any
    failure just means trying again next interval, never something that
    should affect playback."""
    if not (SSH_HOST and SSH_USER):
        return
    remote_dir = f"{SSH_MUSIC_PATH}/{NAV_SUBFOLDER}"
    try:
        list_cmd = _ssh_cmd(f"find {shlex.quote(remote_dir)} -maxdepth 1 -name '*.flac' "
                             f"-printf '%T@ %s %f\\n' 2>/dev/null")
        result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return
        files = []
        for line in result.stdout.splitlines():
            parts = line.split(" ", 2)
            if len(parts) != 3:
                continue
            try:
                files.append((float(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                continue
        files.sort(key=lambda f: f[0])  # oldest first

        total = sum(f[1] for f in files)
        to_delete = []
        freed = 0
        while total > JAM_FOLDER_MAX_BYTES and files:
            _mtime, size, fname = files.pop(0)
            to_delete.append(fname)
            total -= size
            freed += size
        if not to_delete:
            return

        quoted_paths = " ".join(shlex.quote(f"{remote_dir}/{f}") for f in to_delete)
        subprocess.run(_ssh_cmd(f"rm -f -- {quoted_paths}"), capture_output=True, timeout=30)
        print(f"[jam] Cleaned up {len(to_delete)} old Jam/ file(s), freed ~{freed / 1024**2:.0f}MB", flush=True)

        # No direct DB delete here any more: writing navidrome.db with the
        # sqlite3 CLI corrupts it (the CLI's SQLite computes expression-index
        # keys differently from Navidrome's bundled one → "database disk
        # image is malformed", scans silently rolled back for a month). A
        # full scan marks the vanished files missing, which hides them.
        if NAVIDROME_URL and NAVIDROME_USER:
            http.put(f"{NAVIDROME_URL}/api/scanner/trigger",
                      auth=(NAVIDROME_USER, NAVIDROME_PASSWORD), params={"fullScan": "true"}, timeout=15)
    except Exception as e:
        print(f"[jam] Jam/ cleanup failed: {e}", flush=True)


def jam_cleanup_loop():
    while True:
        cleanup_jam_folder()
        time.sleep(JAM_CLEANUP_INTERVAL_SEC)


# ─── Queue engine ────────────────────────────────────────────────────────────

def _queue_sort_key(item):
    """Higher votes first, ties broken by request order — used everywhere
    'what's next' matters (promoting, picking what to download next,
    what's displayed as up-next) instead of raw list position, so an
    upvoted track actually jumps the line rather than just looking more
    popular where it already sat."""
    return (-len(item.get("upvotes") or []), item.get("added_at", 0))


def _promote_next_if_idle():
    """If nothing is playing, start the highest-voted ready item. Must be
    called with state_lock held."""
    if state["now_playing_id"] is not None:
        return
    failed = [i for i in state["queue"] if i["status"] == "failed"]
    for item in failed:
        state["queue"].remove(item)
        state["history"].append(item)
    ready = [i for i in state["queue"] if i["status"] == "ready"]
    if ready:
        nxt = min(ready, key=_queue_sort_key)
        nxt["status"] = "playing"
        state["now_playing_id"] = nxt["id"]
        state["playback_started_at"] = time.time()
        # Nobody is listening — start the new track paused at 0:00 so it's
        # still there, unheard, when a speaker claims the jam.
        if not _speaker_alive():
            _pause_locked("no_speaker")


def pause_playback():
    with state_lock:
        if state["now_playing_id"] and not state["paused"]:
            _pause_locked("host")
            save_state()

def resume_playback():
    with state_lock:
        if state["paused"]:
            _resume_locked()
            if not _speaker_alive():
                # Resuming with no speaker would just play into the void.
                _pause_locked("no_speaker")
            save_state()


def advance(expected_id=None):
    """Finish whatever's currently playing and promote the next ready item.
    If expected_id is given, only acts when it still matches the current
    now_playing_id — a harmless no-op guard against a duplicate/retried
    advance call (a flaky network retry, the watchdog and a client signal
    landing at nearly the same moment, etc.) skipping two tracks instead
    of one."""
    with state_lock:
        if expected_id is not None and state["now_playing_id"] != expected_id:
            return
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
        # A pause is tied to a specific track's timeline — moving to a new
        # one (skip, or the current one simply ending) means whatever was
        # paused no longer applies.
        state["paused"] = False
        state["pause_started_at"] = None
        state["pause_reason"] = None
        _promote_next_if_idle()
        save_state()


def download_worker_loop():
    while True:
        with state_lock:
            queued = [i for i in state["queue"] if i["status"] == "queued"]
            item = min(queued, key=_queue_sort_key) if queued else None
            if item:
                item["status"] = "downloading"
        if not item:
            time.sleep(1)
            continue
        try:
            local_path, duration, album, genre = download_track(item)
            with state_lock:
                # The host can delete a queue item mid-download (see
                # route_host_delete) — it can't be safely ripped out of the
                # list while this same dict is being mutated out from under
                # it, so a delete on an in-flight download just flags it
                # instead. Discovering that flag here means: don't promote
                # it, don't sync it to Navidrome, just discard the file and
                # drop it from the queue entirely (no history entry either —
                # the host removed it, it never really "played").
                if item.get("removed"):
                    if item in state["queue"]:
                        state["queue"].remove(item)
                    save_state()
                    should_sync = False
                else:
                    item["local_path"] = local_path
                    item["duration"] = duration or item.get("duration")
                    item["album"] = album
                    item["genre"] = genre
                    item["status"] = "ready"
                    _promote_next_if_idle()
                    save_state()
                    should_sync = True
            if should_sync:
                threading.Thread(target=sync_to_navidrome_bg, args=(item,), daemon=True).start()
            else:
                try:
                    if local_path and os.path.exists(local_path):
                        os.remove(local_path)
                except Exception:
                    pass
        except Exception as e:
            with state_lock:
                if item.get("removed"):
                    if item in state["queue"]:
                        state["queue"].remove(item)
                else:
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
            # Option A: the speaker went quiet (app closed, PC asleep, phone
            # lost signal) — pause where we are instead of auto-advancing
            # through songs nobody hears.
            if state.get("speaker") and not _speaker_alive():
                print(f"[jam] Speaker {state['speaker'].get('name')!r} timed out — pausing", flush=True)
                state["speaker"] = None
                _pause_locked("no_speaker")
                save_state()
            elif not state.get("speaker") and state["now_playing_id"] and not state["paused"]:
                _pause_locked("no_speaker")
                save_state()
            cur_id = state["now_playing_id"]
            started = state["playback_started_at"]
            paused = state["paused"]
            cur = find_item(cur_id) if cur_id else None
            duration = (cur or {}).get("duration") or 0
        if cur and started and not paused and time.time() - started > duration + ADVANCE_BUFFER_SEC:
            advance()
        maybe_trigger_autofill()


# ─── Invite links ────────────────────────────────────────────────────────────
# Minted from the player page so the host can hand out a link that drops
# people straight onto the request page — one row of state per generated
# link, each independently timed out.

def _prune_expired_invites():
    """Must be called with state_lock held."""
    now = time.time()
    expired = [t for t, inv in state["invites"].items() if inv["expires_at"] <= now]
    for t in expired:
        del state["invites"][t]


def _request_page_url():
    if REQUEST_PAGE_URL:
        return REQUEST_PAGE_URL + "/"
    # Fallback for a direct-port setup with no reverse proxy in front (no
    # REQUEST_PAGE_URL configured): derive the request page's address by
    # swapping :9999 for :9998 on whatever host the link was opened from.
    # $http_host (forwarded below as X-Forwarded-Host) preserves whatever
    # host:port the browser actually sent, unlike nginx's own $host which
    # strips the port.
    host_hdr = request.headers.get("X-Forwarded-Host") or request.host or ""
    hostname = host_hdr.split(":")[0] or "localhost"
    return f"http://{hostname}:9998/"


# ─── Access control ──────────────────────────────────────────────────────────
# One gate for every route, deny-by-default: anything not listed as public or
# guest is host-only, so a route added later can't accidentally ship open to
# the internet.

PUBLIC_ENDPOINTS = {"route_config", "route_invite_consume", "route_cover", "route_host_login", "static"}
GUEST_ENDPOINTS = {"route_search", "route_queue_get", "route_queue_add", "route_queue_add_library",
                   "route_queue_upvote", "route_vote_skip", "route_requeue", "route_queue_remove",
                   "route_reaction_send", "route_invite_check"}


@app.before_request
def _access_gate():
    if request.method == "OPTIONS" or request.endpoint is None:
        return None  # CORS preflight / 404s
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if request.endpoint in GUEST_ENDPOINTS:
        return _require_guest()
    return _require_host()


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/config")
def route_config():
    return jsonify({"request_page_url": REQUEST_PAGE_URL or None, "invite_required": JAM_REQUIRE_INVITE,
                    "host_pin_enabled": bool(JAM_HOST_PIN)})


@app.route("/invite/check")
def route_invite_check():
    """Lets the request page tell "valid invite" from "expired" up front."""
    return jsonify({"ok": True})


# ─── Host login & speaker ────────────────────────────────────────────────────

@app.route("/host/login", methods=["POST"])
def route_host_login():
    """Exchanges Navidrome credentials ({username, password} or Subsonic
    {username, token, salt}) or the host PIN ({pin}) for a host token."""
    data = request.json or {}
    username = (data.get("username") or "").strip()
    pin = (data.get("pin") or "").strip()
    if pin:
        if not (JAM_HOST_PIN and secrets.compare_digest(pin, JAM_HOST_PIN)):
            time.sleep(1)  # cheap brake on PIN guessing
            return jsonify({"error": "Wrong PIN"}), 403
        username = "pin"
    elif username:
        ok, err = _verify_navidrome_user(username, password=data.get("password"),
                                         token=data.get("token"), salt=data.get("salt"))
        if not ok:
            time.sleep(1)
            return jsonify({"error": err}), 403
    else:
        return jsonify({"error": "username or pin required"}), 400
    token = secrets.token_urlsafe(24)
    now = time.time()
    with state_lock:
        _prune_expired_host_tokens()
        state["host_tokens"][token] = {"user": username, "label": (data.get("label") or "")[:60],
                                       "created_at": now, "expires_at": now + HOST_TOKEN_TTL_SEC}
        save_state()
    return jsonify({"token": token, "user": username, "expires_at": now + HOST_TOKEN_TTL_SEC})


@app.route("/host/logout", methods=["POST"])
def route_host_logout():
    token = request.headers.get("X-Jam-Host-Token", "")
    with state_lock:
        state["host_tokens"].pop(token, None)
        save_state()
    return jsonify({"ok": True})


def _speaker_view():
    """Must be called with state_lock held."""
    sp = state.get("speaker")
    if not sp or not _speaker_alive():
        return None
    return {"device_id": sp["device_id"], "name": sp.get("name") or "Unknown device"}


@app.route("/speaker/claim", methods=["POST"])
def route_speaker_claim():
    """Makes the calling device the one speaker ("Play here"). Takes over
    from any other device immediately — it finds out on its next
    heartbeat and stops. Undoes an auto-pause, never a host pause."""
    data = request.json or {}
    device_id = (data.get("device_id") or "").strip()[:80]
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    with state_lock:
        state["speaker"] = {"device_id": device_id, "name": (data.get("name") or "")[:60],
                            "last_seen": time.time()}
        if state["paused"] and state.get("pause_reason") == "no_speaker":
            _resume_locked()
        _promote_next_if_idle()
        save_state()
        return jsonify({"active": True, "speaker": _speaker_view(), "server_time": time.time()})


@app.route("/speaker/heartbeat", methods=["POST"])
def route_speaker_heartbeat():
    """Sent every few seconds by the active speaker. active=false means
    another device took over (or it timed out) — stop playing."""
    device_id = ((request.json or {}).get("device_id") or "").strip()
    with state_lock:
        sp = state.get("speaker")
        active = bool(sp) and sp["device_id"] == device_id and _speaker_alive()
        if active:
            sp["last_seen"] = time.time()
        return jsonify({"active": active, "speaker": _speaker_view(), "server_time": time.time()})


@app.route("/speaker/release", methods=["POST"])
def route_speaker_release():
    """Graceful stop (app closing, "Stop playing here") — pause right away
    instead of waiting out SPEAKER_TIMEOUT_SEC."""
    device_id = ((request.json or {}).get("device_id") or "").strip()
    with state_lock:
        sp = state.get("speaker")
        if sp and sp["device_id"] == device_id:
            state["speaker"] = None
            _pause_locked("no_speaker")
            save_state()
    return jsonify({"ok": True})


@app.route("/jam/end", methods=["POST"])
def route_jam_end():
    """Ends the jam: clears the queue and what's playing, revokes every
    invite link and drops the speaker. History stays for ↺ requeue."""
    with state_lock:
        for item in state["queue"]:
            if item["status"] == "downloading":
                item["removed"] = True  # worker loop discards it when done
            else:
                threading.Timer(1, _cleanup_downloaded_files, args=(item["id"],)).start()
        state["queue"] = [i for i in state["queue"] if i["status"] == "downloading"]
        state["now_playing_id"] = None
        state["playback_started_at"] = None
        state["paused"] = False
        state["pause_started_at"] = None
        state["pause_reason"] = None
        state["invites"] = {}
        state["speaker"] = None
        # Otherwise Auto DJ would refill the "ended" jam straight away.
        state["autoplay_enabled"] = False
        save_state()
    return jsonify({"ok": True})


@app.route("/search")
def route_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    return jsonify({"results": search_tracks(q)})


@app.route("/queue", methods=["GET"])
def route_queue_get():
    session_id = request.headers.get("X-Jam-Session", "")
    is_host = _is_host()  # outside state_lock — _is_host takes it itself
    with state_lock:
        now_playing = find_item(state["now_playing_id"]) if state["now_playing_id"] else None
        upcoming = sorted((i for i in state["queue"] if i["status"] != "playing"), key=_queue_sort_key)
        return jsonify({
            "server_time": time.time(),
            "speaker": _speaker_view(),
            "pause_reason": state.get("pause_reason"),
            "autofill_in_progress": _autofill_running,
            "now_playing": public_view(now_playing, include_stream=is_host, session_id=session_id) if now_playing else None,
            "playback_started_at": state["playback_started_at"],
            "paused": state["paused"],
            "pause_started_at": state["pause_started_at"],
            "autoplay_enabled": state.get("autoplay_enabled", True),
            "queue": [public_view(i, session_id=session_id) for i in upcoming],
            "history": [public_view(i, session_id=session_id) for i in state["history"][-10:]],
        })


def _enqueue_track(video_id, url, title, artist, duration=None, thumbnail="",
                    known_album="", requested_by="Anonymous", session_id=None):
    """Shared by the manual /queue/add route, the requeue-from-history
    route, and the similar-songs autofill below — same duplicate check,
    same Navidrome-match short-circuit, same queue bookkeeping, so any of
    those is treated exactly like a plain human request from here on.
    Returns (item_or_none, error_or_none); error is a (message,
    http_status) pair when not None."""
    title = (title or "Unknown title").strip()
    artist = (artist or "Unknown artist").strip()

    # session_id is None for the Auto DJ/similar-songs autofill call below,
    # which _is_banned already treats as "not banned" — a ban only ever
    # blocks a real human session, never the server's own autofill.
    if _is_banned(session_id):
        return None, ("You've been removed from this jam by the host", 403)

    # Checked outside state_lock — it's a Navidrome network call, not
    # shared in-memory state, and shouldn't hold up every other request
    # while it's in flight.
    match = check_navidrome_duplicate(title, artist, duration)

    with state_lock:
        active_ids = {i["video_id"] for i in state["queue"] if i["status"] in ("queued", "downloading", "ready", "playing")}
        if video_id in active_ids:
            return None, ("That song is already in the queue", 409)

        item = {
            "id": uuid.uuid4().hex[:12],
            "video_id": video_id, "url": url,
            "thumbnail": thumbnail or "",
            "requested_by": (requested_by or "Anonymous").strip()[:40] or "Anonymous",
            "session_id": session_id,
            "added_at": time.time(), "progress": 0,
        }
        if match:
            # Already in the library — queue it, but play the existing copy
            # straight off Navidrome instead of downloading a new one.
            item["title"] = match["title"]
            item["artist"] = match["artist"]
            item["duration"] = match["duration"] or duration
            item["navidrome_song_id"] = match["navidrome_song_id"]
            item["status"] = "ready"
        else:
            item["title"] = title
            item["artist"] = artist
            item["duration"] = duration
            item["known_album"] = known_album
            item["status"] = "queued"
        state["queue"].append(item)
        # A track that skipped straight to "ready" (the Navidrome-match
        # case) needs this nudge itself — normally only a finished download
        # triggers it, and this item never goes through that path at all.
        _promote_next_if_idle()
        save_state()
        return item, None


@app.route("/queue/add", methods=["POST"])
def route_queue_add():
    data = request.json or {}
    video_id = (data.get("video_id") or "").strip()
    url = (data.get("url") or "").strip()
    if not video_id or not url:
        return jsonify({"error": "video_id and url are required"}), 400
    session_id = request.headers.get("X-Jam-Session", "")

    item, err = _enqueue_track(
        video_id, url, data.get("title"), data.get("artist"),
        duration=data.get("duration"), thumbnail=data.get("thumbnail"),
        # Present when the pick came from the YT Music 'songs' search tier —
        # its own real album name, straight from the authoritative source,
        # rather than something download_track() has to go guess afterward.
        known_album=(data.get("album") or "").strip(),
        requested_by=data.get("requested_by"), session_id=session_id)
    if err:
        message, status = err
        return jsonify({"error": message}), status
    return jsonify(public_view(item, session_id=session_id))


def _navidrome_get_song(song_id):
    try:
        resp = http.get(f"{NAVIDROME_URL}/rest/getSong", params={
            "id": song_id, "u": NAVIDROME_USER, "p": NAVIDROME_PASSWORD,
            "v": "1.16.1", "c": "jamidrome", "f": "json"}, timeout=10)
        body = resp.json().get("subsonic-response", {})
        return body.get("song") if body.get("status") == "ok" else None
    except Exception:
        return None


def _resolve_youtube_id_bg(item_id, title, artist):
    """Library tracks have no YouTube id, but Auto DJ seeds its "radio"
    from one — look the song up on YT Music once, in the background."""
    try:
        results = _search_ytmusic_songs(f"{artist} {title}", 3)
        match = next((r for r in results if _title_close_enough(r["title"], title)), None)
        if not match:
            return
        with state_lock:
            for item in state["queue"] + state["history"]:
                if item["id"] == item_id:
                    item["yt_video_id"] = match["video_id"]
                    save_state()
                    break
    except Exception as e:
        print(f"[jam] YouTube id lookup failed for {artist} - {title}: {e}", flush=True)


def _enqueue_library(song_id, requested_by="Anonymous", session_id=None):
    """Queues a song straight from the Navidrome library — no search, no
    download; it streams from Navidrome through /stream/<id>."""
    if _is_banned(session_id):
        return None, ("You've been removed from this jam by the host", 403)
    song = _navidrome_get_song(song_id)
    if not song:
        return None, ("That song wasn't found in the library", 404)
    video_id = f"nd:{song_id}"
    item_id = uuid.uuid4().hex[:12]
    with state_lock:
        active_ids = {i["video_id"] for i in state["queue"] if i["status"] in ("queued", "downloading", "ready", "playing")}
        if video_id in active_ids:
            return None, ("That song is already in the queue", 409)
        item = {
            "id": item_id, "video_id": video_id, "url": None,
            "title": song.get("title") or "Unknown title",
            "artist": song.get("artist") or "Unknown artist",
            "album": song.get("album"), "genre": song.get("genre"),
            "duration": song.get("duration"),
            # Relative on purpose: served by /cover/<id>, which proxies
            # Navidrome's cover art without exposing its credentials.
            "thumbnail": f"/api/cover/{item_id}",
            "navidrome_song_id": song_id, "cover_art_id": song.get("coverArt") or song_id,
            "requested_by": (requested_by or "Anonymous").strip()[:40] or "Anonymous",
            "session_id": session_id, "added_at": time.time(), "progress": 100,
            "status": "ready",
        }
        state["queue"].append(item)
        _promote_next_if_idle()
        save_state()
    threading.Thread(target=_resolve_youtube_id_bg, args=(item_id, item["title"], item["artist"]),
                     daemon=True).start()
    return item, None


@app.route("/queue/add-library", methods=["POST"])
def route_queue_add_library():
    data = request.json or {}
    song_id = (data.get("song_id") or "").strip()
    if not song_id:
        return jsonify({"error": "song_id is required"}), 400
    session_id = request.headers.get("X-Jam-Session", "")
    item, err = _enqueue_library(song_id, data.get("requested_by"), session_id)
    if err:
        message, status = err
        return jsonify({"error": message}), status
    return jsonify(public_view(item, session_id=session_id))


@app.route("/cover/<item_id>")
def route_cover(item_id):
    """Cover art for a library-added track. Public (an <img> can't send
    auth headers) but only for ids that are in this jam's queue/history."""
    with state_lock:
        item = next((i for i in state["queue"] + state["history"] if i["id"] == item_id), None)
        cover_id = item.get("cover_art_id") if item else None
    if not cover_id:
        abort(404)
    try:
        upstream = http.get(f"{NAVIDROME_URL}/rest/getCoverArt", params={
            "id": cover_id, "size": 600, "u": NAVIDROME_USER, "p": NAVIDROME_PASSWORD,
            "v": "1.16.1", "c": "jamidrome"}, timeout=15)
    except Exception:
        abort(502)
    if upstream.status_code != 200:
        abort(404)
    return Response(upstream.content, mimetype=upstream.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/queue/<item_id>/upvote", methods=["POST"])
def route_queue_upvote(item_id):
    """Toggles this session's upvote on a not-yet-playing item — call again
    to take it back. Bumps the item's effective position (see
    _queue_sort_key) rather than physically moving it in the list, so
    added_at stays a meaningful tiebreak and nothing else has to change to
    respect the new order."""
    session_id = request.headers.get("X-Jam-Session", "") or request.remote_addr or ""
    if _is_banned(session_id):
        return jsonify({"error": "You've been removed from this jam by the host"}), 403
    with state_lock:
        item = find_item(item_id)
        if not item or item["status"] == "playing":
            return jsonify({"error": "Not found"}), 404
        upvotes = item.setdefault("upvotes", [])
        if session_id in upvotes:
            upvotes.remove(session_id)
            voted = False
        else:
            upvotes.append(session_id)
            voted = True
        save_state()
        return jsonify({"votes": len(upvotes), "voted_by_me": voted})


@app.route("/queue/<item_id>/vote-skip", methods=["POST"])
def route_vote_skip(item_id):
    """Democratic skip for guests on the request page, who have no other
    way to skip — the player page's own Skip button already acts
    immediately for whoever's standing at the actual display. A small
    fixed threshold rather than a real majority: there's no accounts or
    presence tracking here to know how many people are actually in the
    jam right now to compute one against."""
    session_id = request.headers.get("X-Jam-Session", "") or request.remote_addr or ""
    if _is_banned(session_id):
        return jsonify({"error": "You've been removed from this jam by the host"}), 403
    with state_lock:
        if state["now_playing_id"] != item_id:
            return jsonify({"error": "That track isn't currently playing"}), 400
        item = find_item(item_id)
        skip_votes = item.setdefault("skip_votes", [])
        if session_id not in skip_votes:
            skip_votes.append(session_id)
        count = len(skip_votes)
        will_skip = count >= SKIP_VOTE_THRESHOLD
        if not will_skip:
            save_state()
    if will_skip:
        advance(expected_id=item_id)  # outside state_lock — advance() takes its own
    return jsonify({"skip_votes": min(count, SKIP_VOTE_THRESHOLD), "skip_threshold": SKIP_VOTE_THRESHOLD,
                    "skipped": will_skip})


@app.route("/history/<item_id>/requeue", methods=["POST"])
def route_requeue(item_id):
    """Puts a previously-played (or previously-failed) track back on the
    queue — goes through _enqueue_track exactly like a fresh request, so
    it's a real new item (its own id, its own download if it's not still
    sitting in Navidrome) rather than trying to reuse anything from the
    old one."""
    with state_lock:
        hist_item = next((h for h in state["history"] if h["id"] == item_id), None)
    if not hist_item:
        return jsonify({"error": "Not found"}), 404
    session_id = request.headers.get("X-Jam-Session", "")
    data = request.json or {}
    requested_by = data.get("requested_by") or hist_item.get("requested_by") or "Anonymous"

    if str(hist_item.get("video_id") or "").startswith("nd:"):
        item, err = _enqueue_library(hist_item["navidrome_song_id"], requested_by, session_id)
        if err:
            message, status = err
            return jsonify({"error": message}), status
        return jsonify(public_view(item, session_id=session_id))

    item, err = _enqueue_track(
        hist_item["video_id"], hist_item["url"], hist_item["title"], hist_item["artist"],
        duration=hist_item.get("duration"), thumbnail=hist_item.get("thumbnail"),
        known_album=hist_item.get("album") or hist_item.get("known_album") or "",
        requested_by=requested_by, session_id=session_id)
    if err:
        message, status = err
        return jsonify({"error": message}), status
    return jsonify(public_view(item, session_id=session_id))


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
        upcoming = sorted((i for i in state["queue"] if i["status"] != "playing"), key=_queue_sort_key)[:10]
        # Only the very next ("on deck") item needs its stream_url — that's
        # the one the player page preloads into its second <audio> element
        # to crossfade into, well before the current track actually ends.
        up_next = [public_view(i, include_stream=(j == 0)) for j, i in enumerate(upcoming)]
        return jsonify({
            "now_playing": public_view(now_playing, include_stream=True) if now_playing else None,
            "playback_started_at": state["playback_started_at"],
            "paused": state["paused"],
            "pause_started_at": state["pause_started_at"],
            "autoplay_enabled": state.get("autoplay_enabled", True),
            "autofill_in_progress": _autofill_running,
            "pause_reason": state.get("pause_reason"),
            "speaker": _speaker_view(),
            # Clients compute elapsed = server_time - playback_started_at
            # (plus their own time since this response) so a device whose
            # clock is off still lands on the right second.
            "server_time": time.time(),
            "up_next": up_next,
        })


@app.route("/player/autoplay", methods=["POST"])
def route_player_autoplay():
    data = request.json or {}
    with state_lock:
        state["autoplay_enabled"] = bool(data.get("enabled", True))
        save_state()
        return jsonify({"autoplay_enabled": state["autoplay_enabled"]})


@app.route("/library/jam-cleanup", methods=["POST"])
def route_jam_cleanup():
    """Manual trigger for the same Jam/ size-cap cleanup the background
    loop runs every few hours — mainly for checking it actually works,
    or forcing an immediate reclaim without waiting for the next
    interval."""
    threading.Thread(target=cleanup_jam_folder, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/player/advance", methods=["POST"])
def route_player_advance():
    data = request.json or {}
    advance(expected_id=data.get("expected_id"))
    return jsonify({"ok": True})


@app.route("/player/pause", methods=["POST"])
def route_player_pause():
    pause_playback()
    return jsonify({"ok": True})


@app.route("/player/resume", methods=["POST"])
def route_player_resume():
    resume_playback()
    return jsonify({"ok": True})


def _proxy_navidrome_stream(song_id):
    """Pipe audio straight from Navidrome's own Subsonic stream endpoint —
    used for a track that turned out to already be in the library, so it
    plays from the existing copy instead of a redundant fresh download.
    Proxied (rather than redirecting the browser straight to Navidrome)
    so Navidrome's credentials never reach the client, and so the <audio>
    element sees same-origin audio either way — matters for
    createMediaElementSource, which throws on cross-origin-tainted media."""
    headers = {}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    try:
        upstream = http.get(f"{NAVIDROME_URL}/rest/stream", params={
            "id": song_id, "u": NAVIDROME_USER, "p": NAVIDROME_PASSWORD,
            "v": "1.16.1", "c": "jamidrome",
            # Optional transcoding for phones on mobile data (Navidrome does it).
            **{k: request.args[k] for k in ("maxBitRate", "format") if request.args.get(k)},
        }, headers=headers, stream=True, timeout=30)
    except Exception:
        abort(502)
    excluded = {"content-encoding", "transfer-encoding", "connection"}
    resp_headers = [(k, v) for k, v in upstream.headers.items() if k.lower() not in excluded]
    return Response(upstream.iter_content(chunk_size=65536),
                     status=upstream.status_code, headers=resp_headers)


@app.route("/stream/<item_id>")
def route_stream(item_id):
    with state_lock:
        item = find_item(item_id)
        path = item.get("local_path") if item else None
        nd_song_id = item.get("navidrome_song_id") if item else None
    if nd_song_id:
        return _proxy_navidrome_stream(nd_song_id)
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
    # A per-browser id the frontend generates once and keeps in
    # localStorage — invites are scoped to whichever session created
    # them (see route_invite_list), so a private/incognito window (its
    # own fresh id, no shared localStorage) correctly starts with none.
    session_id = request.headers.get("X-Jam-Session", "")

    with state_lock:
        _prune_expired_invites()
        token = secrets.token_urlsafe(9)
        now = time.time()
        state["invites"][token] = {"created_at": now, "expires_at": now + ttl_seconds,
                                    "ttl_seconds": ttl_seconds, "session_id": session_id}
        save_state()
        return jsonify({"token": token, "created_at": now, "expires_at": now + ttl_seconds,
                         "path": f"/invite/{token}"})


@app.route("/invite/list")
def route_invite_list():
    with state_lock:
        _prune_expired_invites()
        save_state()
        # Host-only now, so every host sees every live link (web player page
        # and LunaDrome alike), not just the ones this browser created.
        invites = [{"token": t, **inv} for t, inv in state["invites"].items()]
    invites.sort(key=lambda i: i["created_at"], reverse=True)
    return jsonify({"invites": invites})


@app.route("/invite/<token>/revoke", methods=["POST"])
def route_invite_revoke(token):
    with state_lock:
        inv = state["invites"].get(token)
        # Host-only route, so any host may revoke any link.
        if inv:
            state["invites"].pop(token, None)
            save_state()
    return jsonify({"ok": True})


@app.route("/invite/qr")
def route_invite_qr():
    """Renders a QR code PNG for whatever URL the frontend already built
    (it already knows its own real address — location.origin — so there's
    no need to reconstruct or guess a URL server-side here)."""
    data = request.args.get("data", "")
    if not data:
        abort(400)
    img = qrcode.make(data, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png")


@app.route("/invite/<token>")
def route_invite_consume(token):
    with state_lock:
        _prune_expired_invites()
        valid = token in state["invites"]
    if valid:
        # The request page keeps the token (localStorage) and sends it as
        # X-Jam-Invite on every call — it may live on another hostname, so a
        # cookie set here wouldn't reach it.
        return redirect(f"{_request_page_url()}?invite={token}", code=302)
    return ("""<!doctype html><html><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Jamidrome</title>
        <style>body{background:#0d0d12;color:#f0f0f8;font-family:sans-serif;
        display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
        text-align:center;padding:24px}</style></head>
        <body><div><h2>This invite link has expired</h2>
        <p style="color:#9090b0">Ask whoever's hosting for a fresh one.</p></div></body></html>""", 404)


# ─── Host controls ───────────────────────────────────────────────────────────
# The only moderation surface in the app — see _require_host_pin above for
# why a single shared PIN is the whole auth model here. Meant to be reached
# from a hidden panel on the player page only (never surfaced on the public
# request page), since whoever's standing at the actual display is who
# should hold the PIN.

@app.route("/host/queue")
def route_host_queue():
    err = _require_host_pin()
    if err:
        return err
    with state_lock:
        now_playing = find_item(state["now_playing_id"]) if state["now_playing_id"] else None
        upcoming = sorted((i for i in state["queue"] if i["status"] != "playing"), key=_queue_sort_key)

        def host_view(item):
            # public_view strips session_id (guests have no business seeing
            # who requested what) — the host panel needs it specifically to
            # offer "ban this person" next to each track.
            v = public_view(item)
            v["session_id"] = item.get("session_id")
            return v

        return jsonify({
            "now_playing": host_view(now_playing) if now_playing else None,
            "queue": [host_view(i) for i in upcoming],
            "banned_sessions": state.get("banned_sessions") or [],
        })


@app.route("/host/queue/<item_id>/delete", methods=["POST"])
def route_host_delete(item_id):
    """Unconditional delete, regardless of status — unlike the public
    /queue/<id>/remove (queued only, no ownership check either, but that's
    a separate pre-existing gap). Removes a queued/ready item outright;
    flags a downloading one for the worker loop to discard once it finishes
    instead of promoting/syncing it (see download_worker_loop); skips the
    currently-playing item to whatever's next, same as a passed skip vote."""
    err = _require_host_pin()
    if err:
        return err
    do_advance = False
    with state_lock:
        item = find_item(item_id)
        if not item:
            return jsonify({"error": "Not found"}), 404
        if item["status"] == "playing":
            do_advance = True
        elif item["status"] == "downloading":
            item["removed"] = True
        else:
            state["queue"].remove(item)
        save_state()
    if do_advance:
        advance(expected_id=item_id)  # outside state_lock — advance() takes its own
    return jsonify({"ok": True})


@app.route("/host/ban", methods=["POST"])
def route_host_ban():
    err = _require_host_pin()
    if err:
        return err
    session_id = ((request.json or {}).get("session_id") or "").strip()
    if not session_id:
        return jsonify({"error": "session_id required"}), 400
    with state_lock:
        banned = state.setdefault("banned_sessions", [])
        if session_id not in banned:
            banned.append(session_id)
        save_state()
        return jsonify({"ok": True, "banned_sessions": banned})


@app.route("/host/unban", methods=["POST"])
def route_host_unban():
    err = _require_host_pin()
    if err:
        return err
    session_id = ((request.json or {}).get("session_id") or "").strip()
    with state_lock:
        banned = state.setdefault("banned_sessions", [])
        if session_id in banned:
            banned.remove(session_id)
        save_state()
        return jsonify({"ok": True, "banned_sessions": banned})


# ─── Live reactions ──────────────────────────────────────────────────────────
# Fired from the request page's fixed emoji row, polled and animated as
# floating emoji on the player page — see the `reactions` global above for
# why these are kept in-memory only, never in `state`.

@app.route("/reactions/send", methods=["POST"])
def route_reaction_send():
    global _reaction_seq
    emoji = ((request.json or {}).get("emoji") or "").strip()
    if emoji not in REACTION_EMOJIS:
        return jsonify({"error": "Unknown reaction"}), 400
    session_id = request.headers.get("X-Jam-Session", "") or request.remote_addr or ""
    if _is_banned(session_id):
        return jsonify({"error": "You've been removed from this jam by the host"}), 403
    now = time.time()
    with reactions_lock:
        if now - _last_reaction_at.get(session_id, 0) < REACTION_COOLDOWN_SEC:
            return jsonify({"error": "Slow down"}), 429
        _last_reaction_at[session_id] = now
        _reaction_seq += 1
        reactions.append({"seq": _reaction_seq, "emoji": emoji, "ts": now})
        del reactions[:-MAX_REACTIONS_KEPT]
    return jsonify({"ok": True})


@app.route("/reactions/poll")
def route_reactions_poll():
    """Cursor-based, not time-based — the player page remembers the highest
    `seq` it's already shown and asks for anything newer, so a slow poll
    tick or a brief disconnect can't replay the same reaction twice or miss
    one, the way a fixed time window could."""
    after = int(request.args.get("after", 0) or 0)
    with reactions_lock:
        new = [r for r in reactions if r["seq"] > after]
        latest = _reaction_seq
    return jsonify({"reactions": new, "latest": latest})


load_state()
with state_lock:
    # If the queue already had a ready track waiting at the head when the
    # process last stopped, nothing would otherwise promote it to actually
    # play again — that only happens as a side effect of a download
    # finishing or /player/advance being called, neither of which is
    # guaranteed to happen any time soon after a restart.
    _promote_next_if_idle()
    # advance()'s 30s post-play cleanup timer is in-memory only — a track
    # that finished right as the process restarted loses that timer and
    # its local file sits there forever. Sweep for exactly that: any file
    # whose id doesn't belong to a still-active queue item.
    active_ids = {i["id"] for i in state["queue"]}
    save_state()
for f in glob.glob(os.path.join(DOWNLOAD_DIR, "**", "*.flac"), recursive=True):
    if os.path.splitext(os.path.basename(f))[0] not in active_ids:
        try:
            os.remove(f)
        except Exception:
            pass
threading.Thread(target=download_worker_loop, daemon=True).start()
threading.Thread(target=watchdog_loop, daemon=True).start()
threading.Thread(target=jam_cleanup_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
