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

// KILL-SWITCHES for every Anthropic-API-calling route.
//
// DEFAULTS:
//   /api/extract-team — ALLOWED. Admin-only tool (team screenshot upload),
//                       no visitor-facing UI calls it, traffic is naturally
//                       low. Set BLOCK_EXTRACT_TEAM=1 to disable.
//   /api/ai           — ALLOWED. Visitor-driven AI Analyst tab. Rate-limited
//                       to RATE_LIMIT requests per IP per hour as a cost
//                       circuit-breaker. Set BLOCK_AI=1 to disable.
//   /api/article-summary — BLOCKED. Visitor-driven per-article summaries
//                          on the news feed; can fire many times per visit.
//                          Set ALLOW_SUMMARY=1 to enable.
//
// Master switches:
//   ALLOW_ANTHROPIC=1 — force-enables every route regardless of others.
//   BLOCK_ANTHROPIC=1 — force-blocks every route regardless of others.
//
// While a route is blocked it returns 503 immediately WITHOUT touching the
// Anthropic API, so no charges accrue.
function anthropicAllowed(env, route) {
  if (env.BLOCK_ANTHROPIC === "1") return false;
  if (env.ALLOW_ANTHROPIC === "1") return true;
  if (route === "extract-team") return env.BLOCK_EXTRACT_TEAM !== "1";  // default ALLOW
  if (route === "ai")           return env.BLOCK_AI !== "1";            // default ALLOW
  if (route === "summary")      return env.ALLOW_SUMMARY === "1";       // default BLOCK
  return false;
}
function anthropicBlockResponse(route) {
  // extract-team and ai default-allow; the only way they get here is via
  // BLOCK_X=1 or BLOCK_ANTHROPIC=1, so the unblock flag differs.
  const flag = route === "extract-team" ? "remove BLOCK_EXTRACT_TEAM (or BLOCK_ANTHROPIC)"
             : route === "ai"           ? "remove BLOCK_AI (or BLOCK_ANTHROPIC)"
             : route === "summary"      ? "set ALLOW_SUMMARY=1"
             :                            "set ALLOW_ANTHROPIC=1";
  return json({
    error: {
      message: "AI features for this route are disabled by the account " +
               "holder to prevent API charges. To re-enable, " + flag + " " +
               "in the Worker's environment variables.",
      code: "ANTHROPIC_BLOCKED",
      route,
    },
  }, 503);
}

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
      if (!anthropicAllowed(env, "summary")) return anthropicBlockResponse("summary");

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
      if (/\bI'?d be (happy|glad) to\b|\bI (don'?t|do not) (see|have)\b|(could|can) you (please )?(share|provide|paste)|please (share|provide|paste)|you'?ve provided|the (full |complete )?article (text|content)|\bas an ai\b|\bI (cannot|can'?t|am unable)\b|light on specifics|lacks? specifics|no specifics|no (concrete |confirmed )?(names|specifics|detail)|without (confirmed )?names|names? (were |are )?(not )?(provided|given|confirmed|mentioned)|wait for confirmation|before burning trades|speculative|a (geelong|carlton|melbourne|adelaide|brisbane|collingwood|essendon|fremantle|hawthorn|richmond|sydney|cats|blues|demons|crows|lions|magpies|bombers|dockers|hawks|tigers|swans|saints|suns|giants|eagles|kangaroos|power|bulldogs) (player|star|gun|midfielder|defender|forward|ruckman|ruck)|a (?:young |veteran |key |big |small )?player\b|filler|mostly (navigation|nav)|no [a-z ]{0,30}(detail|details|specifics)|without (named|confirmed )?(players|names)|no (named|confirmed) players?|named players|before locking in|hold (off |your )?trades?|until (official|confirmed) team|official team (list|sheet)|confirmed teams/i.test(summary)) return json({ summary: "" });
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
      if (!anthropicAllowed(env, "ai")) return anthropicBlockResponse("ai");

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

    // ── Team screenshot -> player names (Claude vision) ──
    if (url.pathname === "/api/extract-team") {
      if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
      if (request.method !== "POST") return new Response("Method not allowed", { status: 405, headers: CORS });
      if (!env.ANTHROPIC_API_KEY) return json({ names: [], error: "AI not configured" }, 500);
      if (!anthropicAllowed(env, "extract-team")) return anthropicBlockResponse("extract-team");
      const limited = await rateLimited(request, env);
      if (limited) return limited;
      let p; try { p = await request.json(); } catch { p = {}; }
      const b64 = (p.image_base64 || "").replace(/^data:[^,]+,/, "");
      const mt = p.media_type || "image/png";
      if (!b64) return json({ names: [] });
      const upstream = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: { "content-type": "application/json", "x-api-key": env.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01" },
        body: JSON.stringify({
          model: "claude-haiku-4-5-20251001",
          max_tokens: 1000,
          messages: [{ role: "user", content: [
            { type: "image", source: { type: "base64", media_type: mt, data: b64 } },
            { type: "text", text: "This is a screenshot of an AFL fantasy (SuperCoach/AFL Fantasy) team. Output ONLY plain player names, one per line, exactly as written. No prefixes, no bullets, no numbering, no positions, no prices, no clubs, no commentary, no apologies, no quotes. If you cannot clearly read any player name, output the single token NONE and nothing else. Do NOT guess, infer or autocomplete." }
          ]}]
        })
      });
      if (!upstream.ok) return json({ names: [] });
      const data = await upstream.json();
      const txt = (data.content && data.content[0] && data.content[0].text) || "";
      // Reject prose lines: real names are short, 2-4 words, every word starts
      // uppercase, and none of the words are common English fillers. Stops
      // Claude's apologetic responses ("I can see this is a fantasy AFL team
      // screenshot, but the image quality is too low...") being treated as
      // names when the OCR fails.
      const FILLER = new Set(["I","Im","Ive","The","This","That","These","Those","To","Is","Are","Was","Were","Be","Been","Can","Cannot","Cant","Could","Would","Should","See","Read","Make","Out","Without","With","From","For","Of","On","In","At","An","A","And","Or","But","No","Not","None","Sorry","Apologies","Unfortunately","However","Note","Names","Name","Player","Players","Team","Image","Screenshot","Quality","Resolution","Provide","Need","Higher","Clearer","Blurry","Low","Risk","Risking","Guessing","Inferring","Visible","Plainly","Listing","Following","Here","Are"]);
      const lineOk = (s) => {
        if (!s || s.length < 3 || s.length > 40) return false;
        if (/[!?:;()\[\]{}<>@#$%^&*=_/\\|"]/.test(s)) return false;
        if (/\d/.test(s)) return false;
        const words = s.split(/\s+/);
        if (words.length < 2 || words.length > 4) return false;
        for (const w of words) {
          if (!w) return false;
          if (FILLER.has(w)) return false;
          // each word: starts uppercase, may contain letters / apostrophe /
          // hyphen / single dot for initials (e.g. "T.J.", "O'Brien").
          if (!/^[A-Z][A-Za-z'.\-]*$/.test(w)) return false;
        }
        return true;
      };
      const names = txt.split(/\r?\n/)
        .map(x => x.replace(/^[\s\-*\u2022]+/, "").replace(/^\d+[.)\s]+/, "").trim())
        .filter(lineOk)
        .slice(0, 40);
      return json({ names });
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

    // Everything else: serve static assets, with an SPA fallback to index.html
    // for clean app routes (e.g. /predict, /risers) that are not real files,
    // so deep links and refreshes load the app instead of 404ing.
    const assetRes = await env.ASSETS.fetch(request);
    if (assetRes.status === 404 && request.method === "GET" && !url.pathname.slice(1).includes(".")) {
      return env.ASSETS.fetch(new Request(new URL("/", request.url), request));
    }
    return assetRes;
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
