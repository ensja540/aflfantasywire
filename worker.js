// Cloudflare Worker for AFLFantasyWire.
//
// Serves the static site (via the ASSETS binding) and proxies AI requests to
// the Anthropic API using a SERVER-SIDE key (the ANTHROPIC_API_KEY secret), so
// the AI feature works for every visitor without anyone pasting their own key.
//
// Deploy:
//   npm i -g wrangler
//   wrangler secret put ANTHROPIC_API_KEY   # paste your key when prompted
//   wrangler deploy
//
// The key lives only in Cloudflare (never in the page source or the repo).

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "POST, OPTIONS",
  "access-control-allow-headers": "content-type",
};

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
