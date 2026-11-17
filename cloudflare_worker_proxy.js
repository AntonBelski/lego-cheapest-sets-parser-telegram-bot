/**
 * Free Cloudflare Worker: Dual Anti-Block Proxy Relay + Exact 5-Minute GitHub Actions Trigger
 *
 * Features:
 * 1. HTTP Relay Proxy (`GET /?url=https://...`):
 *    Masks GitHub Actions Azure datacenter IPs behind Cloudflare's edge network.
 * 2. Scheduled Cron Trigger (`*/5 * * * *`):
 *    If GITHUB_PAT and GITHUB_REPO ("AntonBelski/lego-cheapest-sets-parser-telegram-bot")
 *    are configured in Worker variables, triggers GitHub Actions `workflow_dispatch`
 *    on the exact 5-minute mark (bypassing GitHub's built-in cron delays).
 */
export default {
  async fetch(request, env) {
    const urlObj = new URL(request.url);
    const targetUrl = urlObj.searchParams.get("url");

    if (!targetUrl) {
      return new Response("LEGO Tracker Relay OK. Pass ?url=https://...", { status: 200 });
    }

    if (env.PROXY_SECRET) {
      const provided = request.headers.get("X-Proxy-Secret");
      if (provided !== env.PROXY_SECRET) {
        return new Response("Forbidden", { status: 403 });
      }
    }

    const headers = new Headers();
    headers.set(
      "User-Agent",
      request.headers.get("User-Agent") ||
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    );
    headers.set("Accept", request.headers.get("Accept") || "text/html,application/json,*/*;q=0.8");
    headers.set("Accept-Language", "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7");

    try {
      const response = await fetch(targetUrl, {
        method: "GET",
        headers,
        redirect: "follow",
      });

      const respHeaders = new Headers(response.headers);
      respHeaders.set("Access-Control-Allow-Origin", "*");
      return new Response(response.body, {
        status: response.status,
        headers: respHeaders,
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: String(err) }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
  },

  async scheduled(event, env, ctx) {
    if (!env.GITHUB_PAT || !env.GITHUB_REPO) {
      return;
    }
    const apiUrl = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/lego_tracker.yml/dispatches`;
    await fetch(apiUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_PAT}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "LEGO-Tracker-CF-Cron",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: { check_retirement: "false" },
      }),
    });
  },
};
