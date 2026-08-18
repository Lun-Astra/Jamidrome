# Jamidrome

A small, loose take on Spotify Jam: anyone on the network searches for a
song, it's downloaded and normalized on the spot, and it plays on a shared
screen/speaker a few seconds later. It's a companion to
[SpotiDrome](../spotidrome) — same Navidrome library, same download
pipeline, just a much faster front door for "play this one song right now"
instead of syncing whole playlists.

## Two front doors, one backend

- **Request** (port `9998`). Open this on your phone. Search YouTube, pick
  the result you meant, add it to the queue. Shows what's currently playing
  and who's up next.
- **Player** (port `9999`). Open this once on whatever's connected to the
  speakers (a TV, an old laptop, whatever) and leave it open. It has no
  input of its own beyond a skip button — it just plays the queue,
  automatically, as songs become ready.

Both ports are meant to sit behind your own reverse proxy rather than be
opened directly — see **Behind a reverse proxy** below.

Both talk to the same Flask backend, which:
1. Downloads the picked track via `yt-dlp` straight to FLAC, loudness-
   normalized to the same target Spotidrome's own downloads use, so it
   doesn't stick out volume-wise.
2. Plays it **immediately from that local download** the moment it's
   ready — it does not wait on Navidrome.
3. Separately, in the background, rsyncs a copy into the Navidrome
   library (under a `Jam/` folder) and triggers a scan, purely so the
   track ends up archived permanently. This never blocks or delays
   playback.
4. Refuses to add a song that's already sitting in the queue (or
   currently playing) a second time.
5. Auto-advances the queue when a track ends — driven by the player page's
   own `ended` event, with a server-side timer as a fallback in case the
   player page gets closed or the browser hiccups, so the jam doesn't get
   stuck waiting for a signal that may never arrive.

## Inviting people

The player page has a **🔗 Invite** button (top-left). Pick how long the
link should stay valid (15 minutes up to 24 hours), hit Generate, and share
the resulting `<player-url>/invite/<token>` link — anyone who opens it gets
redirected straight to the request page. Each Generate mints a brand new,
independent link (generating another one doesn't invalidate earlier ones),
and a link is reusable by anyone who has it until it expires or you revoke
it from the same modal. There's no per-person limit or accounts — it's a
"whoever has the link" model, same trust level as the rest of the app.

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
`SSH_MUSIC_PATH`/`NAVIDROME_URL`/`NAVIDROME_USER`/`NAVIDROME_PASSWORD`) and
its already-authorized SSH key (`~/.ssh/id_rsa` on the host) — nothing to
configure here as long as Spotidrome is already set up on this machine.

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
