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

    // Everything else: serve the static site assets.
    return env.ASSETS.fetch(request);
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", ...CORS },
  });
}
