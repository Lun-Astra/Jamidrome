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

  Every not-yet-playing track has an **▲ upvote** button — votes bump a
  track's effective position in the queue (and jump it ahead for
  downloading too), toggleable by clicking again to take your vote back.
  Whatever's currently playing has a **⏭ vote-to-skip** button instead,
  for anyone without access to the actual player screen; once enough
  people vote (a small fixed number — there's no accounts/presence here
  to compute a real majority against) it skips immediately. A "Recently
  Played" list at the bottom has a **↺** on each track to put it straight
  back on the queue. One browser can only have a few requests waiting at
  once (a small fixed cap) so nobody can monopolize the whole queue —
  votes and the cap both key off a per-browser id kept in `localStorage`,
  the same mechanism the player page's invite links already use.
- **Player** (port `9999`). Open this once, on whatever's connected to the
  speakers (a TV, an old laptop, whatever), and leave it open — this is
  the one real jam, playing for the room. It plays the queue automatically
  as songs become ready, with **⏭ Skip** and a real **⏸ Pause** (pausing
  affects the shared jam for everyone watching, same as pausing a normal
  music player — it's not a per-browser thing), and crossfades into the
  next track over its last few seconds rather than cutting hard — see
  **Crossfade** below.

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
   library (under a `Jam/` folder) and triggers a scan, so it's there
   to play again without a re-download for a while. This never blocks
   or delays playback. `Jam/` is a rolling window, not permanent
   storage — see **Jam/ folder cleanup** below.
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
8. **Auto DJ**: if nothing's lined up behind whatever's currently playing
   (or the jam is fully idle), it doesn't just go silent — it asks
   YouTube Music for a "radio" continuation seeded from whatever's
   playing right now, or whatever played last if nothing is (the same
   up-next logic behind YouTube Music's own autoplay, so what comes back
   is genuinely similar rather than just "more by this artist"), skips
   anything played recently, and queues one pick through the exact same
   download/tag/duplicate-check pipeline as a real request — tagged
   `requested_by: "🔁 Auto DJ"` so it's obviously distinct from an actual
   person's pick. It fires *before* a track ends, not only once the queue
   is already empty — otherwise every Auto DJ pick would mean an actual
   silence gap while it searches and downloads. Toggleable from the
   player page (top right); off by default only when the jam has no
   history at all yet to seed a pick from. A real request dropped in at
   any point always takes priority — Auto DJ only ever adds when nothing
   else is already waiting.

Pause is a real, global pause on the one shared jam — not a per-browser
thing. If you happen to have the player page open in more than one place
at once, they all show (and play) the exact same state; there's no
"individual session" behind any of it. Reloading the page (or resuming
from pause) picks up wherever the shared timeline currently is rather
than restarting the track from 0:00.

## Jam/ folder cleanup

Every played track gets synced into `Jam/` on the Navidrome host so it's
there to play again without a re-download. Left unchecked that's an
unbounded download loop — a jam left running unattended (Auto DJ) adds to
it indefinitely, and that's exactly what filled a real Navidrome host's
disk solid once already (Auto DJ itself now also checks free space
before adding anything further — see **Auto DJ** above — but that only
stops things from getting *worse*, it doesn't reclaim anything already
there).

A background job checks every 6 hours and deletes the *oldest* files in
`Jam/` until it's back under 5GB total and triggers a full rescan, which
marks the vanished files missing. (It used to delete their rows from
`navidrome.db` with the `sqlite3` CLI — never do that: the CLI's SQLite
computes expression-index keys differently from Navidrome's bundled one,
which corrupted the database.)
`Jam/` is a rolling window this way, not permanent storage — a track
still played from the local download the moment it's requested either
way, this only affects whether an *old* jam track is still sitting there
to stream again later without a fresh download.

## Crossfade

The player page fades into the next track over its last few seconds
instead of cutting hard — but only when that next track is *actually
already downloaded* by then (Auto DJ's early-prefetch above exists partly
to make that the common case). If it isn't ready in time, playback just
falls back to the exact hard-cut-at-`ended` behavior from before —
crossfading is purely a bonus layered on top, never something the jam
waits on or can get stuck because of.

Implementation-wise, it deliberately avoids touching the page's existing
Web Audio graph (the one feeding the spectrum visualizer) at all — that
graph has been a real source of hard-to-debug silent-audio bugs in this
project before, and crossfading doesn't need it. A second, ordinary
`<audio>` element (never routed through `createMediaElementSource`, so it
just plays through the browser's normal output like any two concurrent
`<audio>` elements on a page do) gets preloaded with whatever's on deck.
Once the current track is within a few seconds of ending, both play
together while their volumes ramp between them; at the end of the ramp,
the *original* `#audio` element itself takes over the new track — seeking
to wherever the temporary element had reached — so it stays the one
thing the visualizer and volume/mute controls ever have to deal with. The
cost of that simplicity is a handoff blip well under a second right at
the end of each crossfade, traded deliberately for not needing a second
full Web Audio chain.

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

Every host (the player page, LunaDrome) sees and can revoke every live
link. A link is the key to the jam: the request page keeps its token and
sends it on every call, and the API refuses guests without a live one
(see **Internet access** below). Ending the jam revokes all links.

## Internet access, host login and the speaker

The jam is meant to be reachable from the internet, so access is split in
three, enforced by one deny-by-default gate in `backend/app.py`:

- **Public:** `/config`, `/invite/<token>` (the link itself) and cover art
  of tracks in the queue.
- **Guests:** search, request, vote, react — only with a live invite token
  (`X-Jam-Invite` header). `JAM_REQUIRE_INVITE=0` turns this off for a
  LAN-only setup.
- **Host:** everything else (playback, streams, invites, moderation). The
  player page asks for `JAM_HOST_PIN` once; apps like LunaDrome log in with
  Navidrome credentials (`POST /host/login`, checked against Navidrome —
  admins only, or the `JAM_HOST_USERS` allowlist). Either way you get a
  host token (`X-Jam-Host-Token`, or `?key=` for audio URLs), valid 90 days.

**One speaker at a time.** The jam lives on the server; a device only plays
it. `POST /speaker/claim` makes a device the speaker ("Play here" — the
player page's Start button does it), it heartbeats every 5s, and any other
device that claims takes over (the old one is told on its next heartbeat
and goes quiet). If the speaker goes silent for 20s — app closed, laptop
asleep, phone lost signal — the jam **pauses where it is** instead of
playing through the queue to nobody, and resumes at the same second when
a device claims again. Guests can keep adding songs meanwhile. A pause
someone pressed on purpose is never undone by a claim.

`/queue/add-library` queues a track straight from the Navidrome library by
song id (no download); `/jam/end` clears the queue, revokes all invites,
drops the speaker and turns Auto DJ off. `/player/state` includes
`server_time` so clients can correct for their own clock.

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
