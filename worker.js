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

// ── Per-route SEO ──────────────────────────────────────────────────────────
// The app is a client-rendered SPA that never sets document.title, so every
// route would otherwise serve the homepage's <title>/description to crawlers
// AND to social scrapers (which don't run JS). We rewrite the shell's metadata
// per pathname with HTMLRewriter (streaming, no parsing cost) so each route is
// independently crawlable and shareable.
const SITE = "https://aflfantasywire.com";
const SEO_ROUTES = {
  "/": {
    title: "AFL Fantasy & SuperCoach Hub 2026 — News, Prices & Predictions | AFLFantasyWire",
    description: "Live AFL Fantasy and SuperCoach news, prices, breakevens, score predictions, form guide, waiver targets and injury updates. Your edge for Classic and Draft.",
  },
  "/rankings": {
    title: "AFL Fantasy & SuperCoach Player Rankings | AFLFantasyWire",
    description: "Live AFL Fantasy and SuperCoach player rankings by average, price and value. Compare every player's scores, breakevens and ownership in one place.",
  },
  "/prices": {
    title: "AFL Fantasy & SuperCoach Prices, Risers & Fallers | AFLFantasyWire",
    description: "Track AFL Fantasy and SuperCoach prices and breakevens, plus the biggest projected price risers and fallers each round to time your trades.",
  },
  "/predict": {
    title: "AFL Fantasy Score Predictions & Projections | AFLFantasyWire",
    description: "Round-by-round AFL Fantasy and SuperCoach score predictions — projected numbers, value picks and matchup-based upside for every player.",
  },
  "/feed": {
    title: "AFL Fantasy News Feed — Injuries, Team News & Rumours | AFLFantasyWire",
    description: "The latest AFL Fantasy and SuperCoach news: injuries, late outs, role changes, team selection and rumours, updated through the day.",
  },
  "/formguide": {
    title: "AFL Fantasy Form Guide | AFLFantasyWire",
    description: "AFL Fantasy and SuperCoach form guide — recent scores, three-round averages and trends to spot who's hot and who's cooling before you trade.",
  },
  "/waiver": {
    title: "AFL Fantasy Waiver Wire & Trade Targets | AFLFantasyWire",
    description: "The best AFL Fantasy and SuperCoach waiver wire and trade targets — cash cows, breakout picks and value buys ranked for your team.",
  },
  "/tools": {
    title: "AFL Fantasy Tools & Calculators | AFLFantasyWire",
    description: "Free AFL Fantasy and SuperCoach tools — price projections, breakeven calculators and trade planners to build a better team.",
  },
  "/ai": {
    title: "AI AFL Fantasy Analyst — Trade & Captain Advice | AFLFantasyWire",
    description: "Ask the AI AFL Fantasy analyst for trade advice, captain picks and player comparisons, powered by live SuperCoach and AFL Fantasy data.",
  },
  "/watchlist": {
    title: "My Watchlist | AFLFantasyWire",
    description: "Your personal AFL Fantasy and SuperCoach watchlist.",
    noindex: true,
  },
};
// SEO-friendly aliases the app also accepts -> their canonical route.
const SEO_ALIASES = {
  "/players": "/rankings", "/top-200": "/rankings",
  "/risers": "/prices", "/fallers": "/prices",
  "/news": "/feed", "/news-feed": "/feed",
  "/form": "/formguide",
  "/wire": "/waiver", "/waiver-wire": "/waiver",
};
function seoForPath(pathname) {
  let p = (pathname || "/").toLowerCase().replace(/\/+$/, "") || "/";
  if (SEO_ALIASES[p]) p = SEO_ALIASES[p];
  const meta = SEO_ROUTES[p];
  // Unknown paths consolidate onto the homepage so junk URLs don't get indexed.
  if (!meta) return { path: "/", ...SEO_ROUTES["/"] };
  return { path: p, ...meta };
}
function injectSeo(response, url) {
  const seo = seoForPath(url.pathname);
  const canonical = SITE + (seo.path === "/" ? "/" : seo.path);
  const setContent = (attr, val) => ({ element(e) { e.setAttribute(attr, val); } });
  let rw = new HTMLRewriter()
    .on("title", { element(e) { e.setInnerContent(seo.title); } })
    .on('meta[name="description"]', setContent("content", seo.description))
    .on('meta[property="og:title"]', setContent("content", seo.title))
    .on('meta[property="og:description"]', setContent("content", seo.description))
    .on('meta[name="twitter:title"]', setContent("content", seo.title))
    .on('meta[name="twitter:description"]', setContent("content", seo.description))
    .on('meta[property="og:url"]', setContent("content", canonical))
    .on('link[rel="canonical"]', setContent("href", canonical));
  if (seo.noindex) {
    rw = rw.on("head", {
      element(e) { e.append('<meta name="robots" content="noindex,follow">', { html: true }); },
    });
  }
  const out = rw.transform(response);
  // The shell is rewritten per route, but every route shares index.html's ETag,
  // so a CDN keyed on that ETag would pin one route's rendered <head> for all of
  // them. Drop the ETag and don't store the HTML at the edge so each request
  // reflects its own route. The (fingerprinted) JS/CSS/image assets are separate
  // responses and keep their own long-lived caching.
  const headers = new Headers(out.headers);
  headers.set("cache-control", "no-store");
  headers.delete("etag");
  return new Response(out.body, { status: out.status, statusText: out.statusText, headers });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // ── Accounts: login, registration, Google sign-in, per-user data sync ──
    // Backed by the SUBS KV namespace under distinct key prefixes, so no new
    // binding is needed. See handleAccount() at the bottom of this file.
    if (url.pathname.startsWith("/api/auth/") || url.pathname === "/api/data") {
      return handleAccount(request, env, url);
    }

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

    // ── Feature recommendations from the in-app widget ──
    // POST stores a suggestion in the SUBS KV under an "fb:" key; the home
    // machine pulls them via GET /api/feedback?secret=... (same secret as
    // /api/subscriptions) and writes them into the repo for review.
    if (url.pathname === "/api/feedback") {
      if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
      if (request.method === "POST") {
        if (!env.SUBS) return json({ error: "feedback storage not configured" }, 500);
        let p; try { p = await request.json(); } catch { p = {}; }
        const text = (p.text || "").toString().trim().slice(0, 1000);
        if (!text) return json({ error: "empty" }, 400);
        const at = new Date().toISOString();
        const id = await sha256(at + "|" + text);
        await env.SUBS.put("fb:" + id, JSON.stringify({
          text,
          page: (p.page || "").toString().slice(0, 120),
          at,
        }), { expirationTtl: 60 * 60 * 24 * 90 });  // 90-day TTL keeps KV bounded
        return json({ ok: true });
      }
      // GET: list (secret-gated) for the home-machine puller.
      if (!env.SUBS) return json({ feedback: [] });
      if (!env.PUSH_LIST_SECRET || url.searchParams.get("secret") !== env.PUSH_LIST_SECRET) {
        return new Response("Unauthorized", { status: 401, headers: CORS });
      }
      const out = [];
      let cursor;
      do {
        const list = await env.SUBS.list({ prefix: "fb:", cursor });
        for (const k of list.keys) {
          const v = await env.SUBS.get(k.name);
          if (v) { try { out.push({ id: k.name.slice(3), ...JSON.parse(v) }); } catch (_) {} }
        }
        cursor = list.list_complete ? null : list.cursor;
      } while (cursor);
      out.sort((a, b) => (a.at < b.at ? 1 : -1));
      return json({ feedback: out });
    }

    // Everything else: serve static assets, with an SPA fallback to index.html
    // for clean app routes (e.g. /predict, /risers) that are not real files,
    // so deep links and refreshes load the app instead of 404ing.
    let assetRes = await env.ASSETS.fetch(request);
    if (assetRes.status === 404 && request.method === "GET" && !url.pathname.slice(1).includes(".")) {
      assetRes = await env.ASSETS.fetch(new Request(new URL("/", request.url), request));
    }
    // Give each app route its own crawlable + shareable metadata by rewriting
    // the served HTML shell per pathname (the SPA never sets it client-side).
    const ct = assetRes.headers.get("content-type") || "";
    if (request.method === "GET" && ct.includes("text/html") && assetRes.status === 200) {
      return injectSeo(assetRes, url);
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

// ════════════════════════════════════════════════════════════════════════════
//  ACCOUNTS  —  email+password + Google sign-in, HttpOnly session cookies, and
//  per-user data sync. Storage: the SUBS KV namespace under these key prefixes:
//    u:e:<email>   -> userId              (email -> id lookup)
//    u:i:<id>      -> user record JSON     {id,email,name,googleSub,pass,created}
//    s:<token>     -> session JSON         {uid,created}   (90-day TTL)
//    d:<id>        -> user data JSON        {data:{...},updatedAt}
// ════════════════════════════════════════════════════════════════════════════

const SESSION_TTL = 60 * 60 * 24 * 90;         // 90 days
const PBKDF2_ITERS = 310000;                   // OWASP-ish for PBKDF2-SHA256
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const SYNC_KEYS = new Set([
  "afw_myteam", "afw_myteam_raw", "afw_watchlist",
  "afw_theme", "afw_syncwatch", "afw_tab", "aflfw_seen_news",
]);

async function handleAccount(request, env, url) {
  const store = env.USERS || env.SUBS;
  const p = url.pathname;
  const method = request.method;
  if (method === "OPTIONS") return new Response(null, { status: 204 });

  // Public: tells the client whether Google sign-in is available.
  if (p === "/api/auth/config") {
    return acctJson({ googleClientId: env.GOOGLE_CLIENT_ID || "" });
  }
  if (!store) return acctJson({ error: "Account storage is not configured." }, 500);

  // ── register ──
  if (p === "/api/auth/register" && method === "POST") {
    const body = await readJson(request);
    const email = String(body.email || "").trim().toLowerCase();
    const password = String(body.password || "");
    if (!EMAIL_RE.test(email)) return acctJson({ error: "Enter a valid email address." }, 400);
    if (password.length < 8) return acctJson({ error: "Password must be at least 8 characters." }, 400);
    if (await store.get("u:e:" + email)) return acctJson({ error: "An account with that email already exists." }, 409);

    const pass = await hashPassword(password);
    const user = { id: crypto.randomUUID(), email, name: "", googleSub: "", pass, created: new Date().toISOString() };
    await store.put("u:i:" + user.id, JSON.stringify(user));
    await store.put("u:e:" + email, user.id);
    return sessionResponse(store, user);
  }

  // ── login ──
  if (p === "/api/auth/login" && method === "POST") {
    const body = await readJson(request);
    const email = String(body.email || "").trim().toLowerCase();
    const password = String(body.password || "");
    const fail = () => acctJson({ error: "Incorrect email or password." }, 401);
    if (!EMAIL_RE.test(email) || !password) return fail();
    const id = await store.get("u:e:" + email);
    if (!id) return fail();
    const user = await getUser(store, id);
    if (!user || !user.pass) return fail();
    const ok = await verifyPassword(password, user.pass);
    if (!ok) return fail();
    return sessionResponse(store, user);
  }

  // ── Google sign-in (verify the ID token, then upsert by email) ──
  if (p === "/api/auth/google" && method === "POST") {
    if (!env.GOOGLE_CLIENT_ID) return acctJson({ error: "Google sign-in is not configured." }, 500);
    const body = await readJson(request);
    let claims;
    try {
      claims = await verifyGoogleToken(String(body.credential || ""), env.GOOGLE_CLIENT_ID);
    } catch (e) {
      return acctJson({ error: "Google sign-in failed." }, 401);
    }
    const email = String(claims.email || "").trim().toLowerCase();
    if (!email || claims.email_verified === false) return acctJson({ error: "Google account email is not verified." }, 401);

    let id = await store.get("u:e:" + email);
    let user;
    if (id) {
      user = await getUser(store, id);
      if (user && !user.googleSub) { user.googleSub = claims.sub; await store.put("u:i:" + user.id, JSON.stringify(user)); }
    }
    if (!user) {
      user = { id: crypto.randomUUID(), email, name: String(claims.name || ""), googleSub: String(claims.sub || ""), pass: null, created: new Date().toISOString() };
      await store.put("u:i:" + user.id, JSON.stringify(user));
      await store.put("u:e:" + email, user.id);
    }
    return sessionResponse(store, user);
  }

  // ── who am I ──
  if (p === "/api/auth/me" && method === "GET") {
    const user = await sessionUser(request, store);
    return acctJson({ user: user ? publicUser(user) : null });
  }

  // ── logout ──
  if (p === "/api/auth/logout" && method === "POST") {
    const token = getCookie(request, "afw_sess");
    if (token) await store.delete("s:" + token);
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json", "set-cookie": clearCookie() },
    });
  }

  // ── per-user data blob (My Team / watchlist / preferences) ──
  if (p === "/api/data") {
    const user = await sessionUser(request, store);
    if (!user) return acctJson({ error: "Not signed in." }, 401);
    if (method === "GET") {
      const raw = await store.get("d:" + user.id);
      if (!raw) return acctJson({ data: null, updatedAt: 0 });
      try { return acctJson(JSON.parse(raw)); } catch (e) { return acctJson({ data: null, updatedAt: 0 }); }
    }
    if (method === "PUT") {
      const body = await readJson(request);
      const incoming = body && body.data && typeof body.data === "object" ? body.data : {};
      // Only persist known keys, and cap value sizes so KV can't be abused.
      const data = {};
      for (const k of Object.keys(incoming)) {
        if (SYNC_KEYS.has(k) && typeof incoming[k] === "string" && incoming[k].length <= 20000) data[k] = incoming[k];
      }
      const updatedAt = Date.now();
      await store.put("d:" + user.id, JSON.stringify({ data, updatedAt }));
      return acctJson({ ok: true, updatedAt });
    }
    return acctJson({ error: "Method not allowed." }, 405);
  }

  return acctJson({ error: "Not found." }, 404);
}

// Same-origin JSON (no wildcard CORS — these routes use credentialed cookies).
function acctJson(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}
async function readJson(request) { try { return await request.json(); } catch { return {}; } }
function publicUser(u) { return { email: u.email, name: u.name || "", google: !!u.googleSub }; }
async function getUser(store, id) {
  const raw = await store.get("u:i:" + id);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}

// Create a session + set the cookie, returning the public user record.
async function sessionResponse(store, user) {
  const token = bufToHex(crypto.getRandomValues(new Uint8Array(32)));
  await store.put("s:" + token, JSON.stringify({ uid: user.id, created: Date.now() }), { expirationTtl: SESSION_TTL });
  return new Response(JSON.stringify({ user: publicUser(user) }), {
    status: 200,
    headers: { "content-type": "application/json", "set-cookie": sessionCookie(token, SESSION_TTL) },
  });
}
async function sessionUser(request, store) {
  const token = getCookie(request, "afw_sess");
  if (!token) return null;
  const raw = await store.get("s:" + token);
  if (!raw) return null;
  let sess; try { sess = JSON.parse(raw); } catch { return null; }
  return sess && sess.uid ? getUser(store, sess.uid) : null;
}
function sessionCookie(token, maxAge) {
  return `afw_sess=${token}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${maxAge}`;
}
function clearCookie() {
  return "afw_sess=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0";
}
function getCookie(request, name) {
  const c = request.headers.get("cookie") || "";
  const m = c.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : null;
}

// ── password hashing (PBKDF2-SHA256 via WebCrypto) ──
async function hashPassword(password, saltHex, iterations = PBKDF2_ITERS) {
  const salt = saltHex ? hexToBuf(saltHex) : crypto.getRandomValues(new Uint8Array(16));
  const keyMat = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", salt, iterations, hash: "SHA-256" }, keyMat, 256);
  return { hash: bufToHex(bits), salt: bufToHex(salt), iterations };
}
async function verifyPassword(password, stored) {
  if (!stored || !stored.salt) return false;
  const got = await hashPassword(password, stored.salt, stored.iterations || PBKDF2_ITERS);
  return timingSafeEqual(got.hash, stored.hash);
}
function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ── Google ID-token verification (RS256 against Google's JWKS) ──
let _googleCerts = null;      // { keys: [...jwk], exp: epochMs }
async function fetchGoogleCerts() {
  if (_googleCerts && _googleCerts.exp > Date.now()) return _googleCerts.keys;
  const r = await fetch("https://www.googleapis.com/oauth2/v3/certs");
  const j = await r.json();
  let ttl = 3600000;
  const cc = r.headers.get("cache-control") || "";
  const mm = cc.match(/max-age=(\d+)/);
  if (mm) ttl = parseInt(mm[1], 10) * 1000;
  _googleCerts = { keys: j.keys || [], exp: Date.now() + ttl };
  return _googleCerts.keys;
}
async function verifyGoogleToken(idToken, clientId) {
  const parts = idToken.split(".");
  if (parts.length !== 3) throw new Error("malformed token");
  const header = JSON.parse(b64urlToStr(parts[0]));
  const payload = JSON.parse(b64urlToStr(parts[1]));
  const keys = await fetchGoogleCerts();
  const jwk = keys.find((k) => k.kid === header.kid);
  if (!jwk) throw new Error("unknown signing key");
  const key = await crypto.subtle.importKey(
    "jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]
  );
  const data = new TextEncoder().encode(parts[0] + "." + parts[1]);
  const sig = b64urlToBuf(parts[2]);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, sig, data);
  if (!ok) throw new Error("bad signature");
  if (payload.aud !== clientId) throw new Error("aud mismatch");
  if (payload.iss !== "https://accounts.google.com" && payload.iss !== "accounts.google.com") throw new Error("iss mismatch");
  if (!payload.exp || payload.exp * 1000 < Date.now()) throw new Error("expired");
  return payload;
}

// ── byte/string helpers ──
function bufToHex(buf) {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function hexToBuf(hex) {
  const a = new Uint8Array(hex.length / 2);
  for (let i = 0; i < a.length; i++) a[i] = parseInt(hex.substr(i * 2, 2), 16);
  return a;
}
function b64urlToBuf(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  const a = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
  return a;
}
function b64urlToStr(s) {
  return new TextDecoder().decode(b64urlToBuf(s));
}
