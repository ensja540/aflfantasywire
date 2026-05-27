// Cloudflare Worker for AFLFantasyWire.
//
// Serves the static site (via the ASSETS binding) and proxies AI requests to
// the Anthropic API using a SERVER-SIDE key (the ANTHROPIC_API_KEY secret), so
// the AI feature works for every visitor without anyone pasting their own key.
//
// Deploy:
//   npm i -g wrangler
//   wrangler kv namespace create RATE_LIMIT   # copy the printed id into wrangler.jsonc
//   wrangler secret put ANTHROPIC_API_KEY     # paste your key when prompted
//   wrangler deploy
//
// The key lives only in Cloudflare (never in the page source or the repo).

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "POST, OPTIONS",
  "access-control-allow-headers": "content-type",
};

// AI proxy abuse guard: max requests per client IP per rolling hour.
const RATE_LIMIT = 10;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // On-demand full-article summary: fetch the source article server-side
    // (no CORS limits, key stays server-side), extract its text, and summarise
    // it with Claude. Falls back to the snippet we already have when the source
    // can't be fetched (paywall/block).
    if (url.pathname === "/api/article-summary") {
      if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
      if (request.method !== "POST") return new Response("Method not allowed", { status: 405, headers: CORS });
      if (!env.ANTHROPIC_API_KEY) return json({ error: { message: "AI proxy missing ANTHROPIC_API_KEY." } }, 500);

      const limited = await rateLimited(request, env);
      if (limited) return limited;

      let payload;
      try { payload = await request.json(); } catch { payload = {}; }
      const artUrl = (payload.url || "").trim();
      const headline = (payload.headline || "").slice(0, 300);
      let text = (payload.text || "").trim();

      if (artUrl && /^https?:\/\//i.test(artUrl)) {
        try {
          const ctrl = new AbortController();
          const to = setTimeout(() => ctrl.abort(), 8000);
          const r = await fetch(artUrl, {
            headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36" },
            redirect: "follow",
            signal: ctrl.signal,
          });
          clearTimeout(to);
          if (r.ok) {
            const full = extractArticleText(await r.text());
            if (full.length > text.length) text = full;
          }
        } catch (_) { /* fall back to the snippet */ }
      }

      text = text.slice(0, 9000);
      if (!text || text.length < 40) return json({ summary: "" });

      const prompt =
        "Summarise this AFL article for a fantasy footy (SuperCoach/AFL Fantasy) app in 3-5 sentences. " +
        "Lead with what matters to fantasy coaches — selection, injury, role, form, price implications. " +
        "Name the specific players and clubs by their full names and cite concrete numbers; never use vague references like 'a player' or 'the player'. " +
        "Plain text, no preamble, no markdown.\n\n" +
        (headline ? "Headline: " + headline + "\n\n" : "") + "Article:\n" + text;

      const upstream = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": env.ANTHROPIC_API_KEY,
          "anthropic-version": "2023-06-01",
        },
        body: JSON.stringify({
          model: "claude-haiku-4-5-20251001",
          max_tokens: 700,
          messages: [{ role: "user", content: prompt }],
        }),
      });
      if (!upstream.ok) return json({ summary: "" });
      const data = await upstream.json();
      const summary = (data.content && data.content[0] && data.content[0].text || "").trim();
      // Reject meta/refusal "waffle" (model asking for the article text).
      if (/\bI'?d be (happy|glad) to\b|\bI (don'?t|do not) (see|have)\b|(could|can) you (please )?(share|provide|paste)|please (share|provide|paste)|you'?ve provided|the (full |complete )?article (text|content)|\bas an ai\b|\bI (cannot|can'?t|am unable)\b|light on specifics|lacks? specifics|no specifics|no (concrete |confirmed )?(names|specifics|detail)|without (confirmed )?names|names? (were |are )?(not )?(provided|given|confirmed|mentioned)|wait for confirmation|before burning trades|speculative|a (geelong|carlton|melbourne|adelaide|brisbane|collingwood|essendon|fremantle|hawthorn|richmond|sydney|cats|blues|demons|crows|lions|magpies|bombers|dockers|hawks|tigers|swans|saints|suns|giants|eagles|kangaroos|power|bulldogs) player/i.test(summary)) return json({ summary: "" });
      return json({ summary });
    }

    if (url.pathname === "/api/ai") {
      if (request.method === "OPTIONS") {
        return new Response(null, { headers: CORS });
      }
      if (request.method !== "POST") {
        return new Response("Method not allowed", { status: 405, headers: CORS });
      }
      if (!env.ANTHROPIC_API_KEY) {
        return json({ error: { message: "AI proxy is missing the ANTHROPIC_API_KEY secret." } }, 500);
      }

      // Rate limit: 10 requests per IP per hour (KV-backed hourly bucket).
      // Skips gracefully if the RATE_LIMIT KV namespace isn't bound yet.
      if (env.RATE_LIMIT) {
        const ip = request.headers.get("CF-Connecting-IP") || "unknown";
        const bucket = Math.floor(Date.now() / 3600000); // current hour
        const key = `rl:${ip}:${bucket}`;
        const used = parseInt((await env.RATE_LIMIT.get(key)) || "0", 10);
        if (used >= RATE_LIMIT) {
          return new Response(
            JSON.stringify({ error: { message: `Rate limit reached: ${RATE_LIMIT} AI requests per hour. Try again later.` } }),
            { status: 429, headers: { "content-type": "application/json", "retry-after": "3600", ...CORS } }
          );
        }
        // Reserve this request's slot (TTL covers the rest of the hour).
        await env.RATE_LIMIT.put(key, String(used + 1), { expirationTtl: 3600 });
      }

      const body = await request.text();
      const upstream = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": env.ANTHROPIC_API_KEY,
          "anthropic-version": "2023-06-01",
        },
        body,
      });
      return new Response(await upstream.text(), {
        status: upstream.status,
        headers: { "content-type": "application/json", ...CORS },
      });
    }

    // ── Web Push: VAPID public key (client needs it to subscribe) ──
    if (url.pathname === "/api/vapid") {
      return json({ publicKey: env.VAPID_PUBLIC || "" });
    }

    // ── Web Push: store a device's push subscription + its watchlist ──
    // Backed by the SUBS KV namespace; the sender (notify.py) reads them via
    // /api/subscriptions and pushes news matching each watchlist.
    if (url.pathname === "/api/subscribe") {
      if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
      if (request.method !== "POST") return new Response("Method not allowed", { status: 405, headers: CORS });
      if (!env.SUBS) return json({ error: "push storage not configured" }, 500);
      let p; try { p = await request.json(); } catch { p = {}; }
      const sub = p.subscription;
      if (!sub || !sub.endpoint) return json({ error: "missing subscription" }, 400);
      const key = "sub:" + (await sha256(sub.endpoint));
      await env.SUBS.put(key, JSON.stringify({
        subscription: sub,
        watchlist: Array.isArray(p.watchlist) ? p.watchlist.map(String) : [],
        updated: new Date().toISOString(),
      }));
      return json({ ok: true });
    }

    if (url.pathname === "/api/unsubscribe") {
      if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
      if (request.method !== "POST") return new Response("Method not allowed", { status: 405, headers: CORS });
      if (!env.SUBS) return json({ ok: true });
      let p; try { p = await request.json(); } catch { p = {}; }
      const endpoint = (p.endpoint || (p.subscription && p.subscription.endpoint) || "").trim();
      if (endpoint) await env.SUBS.delete("sub:" + (await sha256(endpoint)));
      return json({ ok: true });
    }

    // List all stored subscriptions (for the home-machine sender). Gated by a
    // shared secret so it isn't world-readable.
    if (url.pathname === "/api/subscriptions") {
      if (!env.SUBS) return json({ subscriptions: [] });
      if (!env.PUSH_LIST_SECRET || url.searchParams.get("secret") !== env.PUSH_LIST_SECRET) {
        return new Response("Unauthorized", { status: 401, headers: CORS });
      }
      const out = [];
      let cursor;
      do {
        const list = await env.SUBS.list({ prefix: "sub:", cursor });
        for (const k of list.keys) {
          const v = await env.SUBS.get(k.name);
          if (v) { try { out.push(JSON.parse(v)); } catch (_) {} }
        }
        cursor = list.list_complete ? null : list.cursor;
      } while (cursor);
      return json({ subscriptions: out });
    }

    // Everything else: serve the static site assets.
    return env.ASSETS.fetch(request);
  },
};

// Hex SHA-256 of a string (used to key subscriptions by endpoint).
async function sha256(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", ...CORS },
  });
}

// Returns a 429 Response when the client IP is over the hourly AI budget (and
// reserves a slot otherwise), or null when there's no limit / KV isn't bound.
async function rateLimited(request, env) {
  if (!env.RATE_LIMIT) return null;
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const bucket = Math.floor(Date.now() / 3600000);
  const key = `rl:${ip}:${bucket}`;
  const used = parseInt((await env.RATE_LIMIT.get(key)) || "0", 10);
  if (used >= RATE_LIMIT) {
    return new Response(
      JSON.stringify({ error: { message: `Rate limit reached: ${RATE_LIMIT} AI requests per hour. Try again later.` } }),
      { status: 429, headers: { "content-type": "application/json", "retry-after": "3600", ...CORS } }
    );
  }
  await env.RATE_LIMIT.put(key, String(used + 1), { expirationTtl: 3600 });
  return null;
}

// Best-effort readable-text extraction from an article's HTML: drop scripts and
// styles, prefer paragraph text, and fall back to a full tag-strip.
function extractArticleText(html) {
  html = html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<noscript[\s\S]*?<\/noscript>/gi, " ");
  const ps = [];
  const re = /<p\b[^>]*>([\s\S]*?)<\/p>/gi;
  let m;
  while ((m = re.exec(html))) {
    const t = m[1].replace(/<[^>]+>/g, " ").replace(/&[a-z#0-9]+;/gi, " ").replace(/\s+/g, " ").trim();
    if (t.length > 40) ps.push(t);
  }
  let text = ps.join("\n");
  if (text.length < 400) {
    text = html.replace(/<[^>]+>/g, " ").replace(/&[a-z#0-9]+;/gi, " ").replace(/\s+/g, " ").trim();
  }
  return text;
}
