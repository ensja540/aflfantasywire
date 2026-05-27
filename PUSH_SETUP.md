# Web Push notifications — setup runbook

How watchlist push alerts fit together:

```
Browser (index.html)                Cloudflare Worker            Home machine
 ─ bell → subscribeToPush()  ──POST /api/subscribe──►  SUBS KV     notify.py
   (public VAPID key baked in)                          (stores      ├─ GET /api/subscriptions?secret=…
                                                          sub +      ├─ match fresh news.json ↔ watchlist
                                                          watchlist) └─ pywebpush.send() ──► push service ──► device
```

The **sending** is done in Python (`notify.py`) with `pywebpush`, which handles
the RFC 8291 payload encryption. The Worker only **stores** subscriptions and
lists them back to the sender. The browser subscribes with the public VAPID key
(already hard-coded in `index.html`); the matching private key lives only in
`.env` on the home machine and as nothing on the server.

The keypair is consistent: the private key below derives the exact public key
baked into the bundle (verified).

---

## 1. Cloudflare: create the subscription store (KV) — one time

```
wrangler kv namespace create SUBS
```

Copy the printed `id` and add this block to `wrangler.jsonc` (top level):

```jsonc
"kv_namespaces": [
  { "binding": "SUBS", "id": "PASTE_THE_PRINTED_ID_HERE" }
]
```

> Do **not** commit `wrangler.jsonc` with a placeholder/invalid id — the deploy
> validates KV ids and will fail. Add it only once you have the real id.

## 2. Cloudflare: set the list secret — one time

This gates `/api/subscriptions` so it isn't world-readable. Pick any long random
string and use the **same** value in `.env` (step 4).

```
wrangler secret put PUSH_LIST_SECRET
```

## 3. Deploy the Worker

```
wrangler deploy
```

(If your repo auto-deploys on `git push`, that covers `worker.js` — but KV
bindings and secrets created above still need the `wrangler` commands.)

## 4. Home machine: `.env` (gitignored — never commit)

Add these three lines to `C:\aflfantasywire\.env`:

```
PUSH_LIST_SECRET=<same value you set in step 2>
VAPID_PRIVATE_KEY=MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgHre2L8wILmvwwbvHyH6i_LRsFIjd9Oz6HlujnE4VFRWhRANCAATR1sOVJmeIFq3FbITLKhxCzHclo5rsIOAD5EgeX9MMggreGiUw89Jlkd3x0Zw5Rkee27yIC1pLpVNwPOxLs3KO
VAPID_SUBJECT=mailto:ensor.jack@gmail.com
```

## 5. Run the sender

```
venv\Scripts\python.exe notify.py
```

Run it after each scrape. To automate, call it at the end of `auto_scrape.py`'s
cycle (ask Claude to wire it in).

Behaviour:
- Only pushes news from the last `FRESH_HOURS` (24h) so a new subscriber / first
  run isn't blasted with the backlog.
- Skips `General`-category puff pieces unless they're flagged `urgent`.
- De-dupes per device via `notify_sent.json` (gitignored).
- Prunes dead subscriptions (HTTP 404/410) via `/api/unsubscribe`.

---

## Notes & caveats

- **iOS/iPadOS** only deliver Web Push when the site is **installed to the Home
  Screen** (Add to Home Screen) and needs a linked `manifest.json` + icons.
  Desktop Chrome/Edge/Firefox and Android Chrome work without install. (PWA
  manifest/icon linking is a separate follow-up.)
- The bell only appears on browsers that support the Push API, and only latches
  "on" once the Worker has confirmed it stored the subscription — so until steps
  1–3 are done, tapping it will request permission but not stick.
- `sw.js` (the push receiver) is already in the repo and served at `/sw.js`.
