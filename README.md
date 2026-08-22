# Jamidrome

A small, loose take on Spotify Jam: anyone on the network searches for a
song, it's downloaded and normalized on the spot, and it plays on a shared
screen/speaker a few seconds later. It's a companion to
[SpotiDrome](../spotidrome) — same Navidrome library, same download
pipeline, just a much faster front door for "play this one song right now"
instead of syncing whole playlists.

## Two front doors, one backend

- **Request** (port `9998`). Open this on your phone. Search, pick the
  result you meant, add it to the queue. Shows what's currently playing
  and who's up next.

  Search checks YouTube Music's own "songs" category first — YouTube's
  own classification of a result as an actual released track, which
  specifically excludes covers, reuploads, lyric videos, and live
  performances — so the real release shows up first (searching "Faint
  Linkin Park" puts the actual Meteora track on top, not a lyric-video
  reupload), with its real album name already known rather than guessed
  at after downloading. Plain YouTube search only fills in whatever's left
  (deduped), so covers/live versions are still findable, just never
  crowding out the real thing.
- **Player** (port `9999`). Open this once, on whatever's connected to the
  speakers (a TV, an old laptop, whatever), and leave it open — this is
  the one real jam, playing for the room. It plays the queue automatically
  as songs become ready, with **⏭ Skip** and a real **⏸ Pause** (pausing
  affects the shared jam for everyone watching, same as pausing a normal
  music player — it's not a per-browser thing).

Both ports are meant to sit behind your own reverse proxy rather than be
opened directly — see **Behind a reverse proxy** below.

Both talk to the same Flask backend, which:
1. Downloads the picked track via `yt-dlp` straight to FLAC, loudness-
   normalized to the same target Spotidrome's own downloads use, so it
   doesn't stick out volume-wise.
2. Tags it properly — real genre from Spotify's catalog (falling back to
   YouTube's own video tags the same way Spotidrome does), and a real
   album looked up via `yt-dlp` if one isn't already embedded, rather than
   being left with YouTube's generic embedded metadata (genre always just
   "Music"; album usually empty). This logic is copied in from Spotidrome
   (same functions, same tolerances) rather than Jamidrome calling into it
   at runtime — it stays a fully standalone app, just tags exactly as well
   as a normal Spotidrome-synced track instead of a thinner version of it.
3. Plays it **immediately from that local download** the moment it's
   ready — it does not wait on Navidrome.
4. Separately, in the background, rsyncs a copy into the Navidrome
   library (under a `Jam/` folder) and triggers a scan, purely so the
   track ends up archived permanently. This never blocks or delays
   playback.
5. Refuses to add a song that's already sitting in the queue (or currently
   playing) a second time.
6. Separately checks Navidrome's own library (via its Subsonic API) for a
   track that's already a good match by title/artist/duration, using the
   same matching tolerances Spotidrome uses to vet its own download
   candidates. If it's already there, the request still joins the queue —
   it just streams straight from the existing Navidrome copy instead of
   downloading a redundant new one, skipping the download step entirely.
7. Auto-advances the queue when a track ends — driven by the player page's
   own `ended` event, with a server-side timer as a fallback in case the
   player page gets closed or the browser hiccups, so the jam doesn't get
   stuck waiting for a signal that may never arrive.
8. **Auto DJ**: if the queue ever runs completely dry (nothing playing,
   nothing waiting) it doesn't just go silent — it asks YouTube Music for
   a "radio" continuation seeded from whatever played last (the same
   up-next logic behind YouTube Music's own autoplay, so what comes back
   is genuinely similar rather than just "more by this artist"), skips
   anything played recently, and queues one pick through the exact same
   download/tag/duplicate-check pipeline as a real request — tagged
   `requested_by: "🔁 Auto DJ"` so it's obviously distinct from an actual
   person's pick. Toggleable from the player page (top right); off by
   default only when the jam has no history at all yet to seed a pick
   from. A real request dropped in at any point always takes priority —
   Auto DJ only ever adds when the queue is otherwise completely empty.

Pause is a real, global pause on the one shared jam — not a per-browser
thing. If you happen to have the player page open in more than one place
at once, they all show (and play) the exact same state; there's no
"individual session" behind any of it. Reloading the page (or resuming
from pause) picks up wherever the shared timeline currently is rather
than restarting the track from 0:00.

## Inviting people

The player page has a **🔗 Invite** button (top-left). Pick how long the
link should stay valid (15 minutes up to 24 hours) and hit Generate — it
shows an actual scannable QR code right there on screen (point a phone
camera at it), plus the same `<player-url>/invite/<token>` link as text to
copy/share directly. Anyone who opens it (by scanning or by the link)
gets redirected straight to the request page. Each Generate mints a brand
new, independent link (generating another one doesn't invalidate earlier
ones), and a link is reusable by anyone who has it until it expires or
you revoke it from the same modal. Any link still listed under Active
Links can be reopened (🔗) to bring its link/QR back up again, without
needing to generate a new one.

The Active Links list itself is scoped per browser (a random id kept in
that browser's `localStorage`) — open the player page in a different
browser, or a private/incognito window, and its Active Links starts
empty, seeing none of another session's links and unable to revoke them
either. The links *themselves* are still "whoever has the link" with no
accounts — this scoping is just about which session's own management
list shows what, not about restricting who can use a link once shared.

## Behind a reverse proxy

If the request and player pages are exposed through your own reverse proxy
on separate hostnames (rather than opened directly on `:9998`/`:9999`), set
`REQUEST_PAGE_URL` in `.env` (copy `.env.example`) to the request page's
full external URL, e.g.:

```
REQUEST_PAGE_URL=https://aanvragenjam.example.com
```

This is what the invite-link redirect and the player's idle-screen hint
use — there's no way to derive one hostname from the other once they're on
different domains, so it has to be told explicitly. Leave it unset and
everything falls back to the old behavior of swapping `:9999` for `:9998`
on whatever host the page was reached by, for a plain direct-port setup
with no proxy in front.

## Setup

Reuses Spotidrome's `.env` directly (`SSH_HOST`/`SSH_USER`/`SSH_PORT`/
`SSH_MUSIC_PATH`/`NAVIDROME_URL`/`NAVIDROME_USER`/`NAVIDROME_PASSWORD`/
`SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET`/`SPOTIFY_REDIRECT_URI`) and its
already-authorized SSH key and Spotify OAuth token cache (`~/.ssh/id_rsa`
and `~/.ssh/.spotify_cache` on the host) — nothing to configure or
re-authenticate here as long as Spotidrome is already set up on this
machine. Genre lookup just falls back to YouTube's own video tags if
Spotify was never connected through Spotidrome at all.

It also joins Spotidrome's docker network (`spotidrome_default`) so it can
reach Spotidrome's `bgutil-pot` container instead of running a second one —
**Spotidrome needs to be up first** (`docker compose up -d` in `../spotidrome`)
before this one is started, since that network has to already exist.

```bash
docker compose up -d --build
```

Then open the player URL on the display/speaker device and the request URL
on your phone — either the reverse-proxied hostnames (see below) or
`http://<host>:9999` / `http://<host>:9998` directly.

## Notes / known trade-offs

- No accounts — the "requested by" name on the request page is just a
  free-text label stored in that browser's `localStorage`, not an identity.
  Anyone can remove anyone's still-queued (not yet downloading) request.
- The player page needs one click on first load (browser autoplay policy
  requires a user gesture before audio can play) — after that it keeps
  advancing on its own with no further interaction.
- Queue/download state persists across a restart (`data/state.json`), but
  whatever was mid-download when the process stopped is requeued rather
  than resumed.
