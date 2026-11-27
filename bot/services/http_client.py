from __future__ import annotations

import asyncio
import logging
import random
import time
from urllib.parse import quote, urlparse

import httpx
from bot.config import settings

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

logger = logging.getLogger(__name__)

BROWSER_PROFILES: tuple[tuple[str, str], ...] = (
    (
        "safari17_0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    ),
    (
        "chrome124",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ),
    (
        "chrome120",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ),
)

USER_AGENTS = tuple(ua for _, ua in BROWSER_PROFILES)


class AntiBlockHttpClient:
    """
    Resilient HTTP client designed to prevent IP blocklisting:
    - Uses `curl_cffi` TLS/JA3/HTTP2 browser fingerprint impersonation (bypasses Cloudflare & anti-bot WAFs)
    - Enforces per-domain rate limiting with randomized jitter so no site is burst-spammed
    - Supports rotating HTTP/SOCKS5 proxies (`PROXY_URLS`)
    - Supports optional free Cloudflare Worker relay (`CF_WORKER_PROXY_URL`) for GitHub Actions datacenter IPs
    - Tracks total HTTP requests per cycle for observability
    """

    def __init__(self) -> None:
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._domain_last_call: dict[str, float] = {}
        self._url_locks: dict[str, asyncio.Lock] = {}
        # Cache key: (url, use_jina_html_on_block) -> (expires_at_monotonic, status, text, headers)
        self._response_cache: dict[tuple[str, bool], tuple[float, int, str, dict[str, str]]] = {}
        self.total_requests_made: int = 0
        self.requests_by_domain: dict[str, int] = {}

    def clear_cache(self) -> None:
        self._response_cache.clear()

    def record_external_call(self, url: str) -> None:
        domain = urlparse(url).netloc.lower() or "unknown"
        if domain.startswith("www."):
            domain = domain[4:]
        self.total_requests_made += 1
        self.requests_by_domain[domain] = self.requests_by_domain.get(domain, 0) + 1

    def format_api_calls_summary(self) -> str:
        lines = [
            "=" * 88,
            f"EXTERNAL SITE HTTP / API CALLS SUMMARY (Total: {self.total_requests_made} requests)",
            "=" * 88,
        ]
        for dom, cnt in sorted(self.requests_by_domain.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  • {dom:<32} : {cnt} call(s)")
        lines.append("=" * 88)
        return "\n".join(lines)

    def _get_domain_lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._domain_locks:
            self._domain_locks[domain] = asyncio.Lock()
        return self._domain_locks[domain]

    def _get_url_lock(self, cache_key: str) -> asyncio.Lock:
        if cache_key not in self._url_locks:
            self._url_locks[cache_key] = asyncio.Lock()
        return self._url_locks[cache_key]

    async def _throttle_domain(self, domain: str) -> None:
        lock = self._get_domain_lock(domain)
        async with lock:
            last = self._domain_last_call.get(domain, 0.0)
            now = time.monotonic()
            if domain == "r.jina.ai":
                # Keep comfortably below r.jina.ai's free-tier 20 req/min rate limit
                delay = random.uniform(3.2, 4.0)
            else:
                delay = random.uniform(settings.min_domain_delay_sec, settings.max_domain_delay_sec)
            wait_for = (last + delay) - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._domain_last_call[domain] = time.monotonic()

    def _get_cached(self, url: str, use_jina_html_on_block: bool) -> tuple[int, str, dict[str, str]] | None:
        now = time.monotonic()
        # A successful 200 cached under either flag can always be reused
        for flag in (use_jina_html_on_block, not use_jina_html_on_block):
            entry = self._response_cache.get((url, flag))
            if entry and entry[0] > now:
                if entry[1] == 200 or flag == use_jina_html_on_block:
                    return entry[1], entry[2], entry[3]
        return None

    def _set_cached(
        self,
        url: str,
        use_jina_html_on_block: bool,
        status: int,
        text: str,
        headers: dict[str, str],
    ) -> None:
        ttl = 120.0 if (status == 200 and text) else (20.0 if status == 403 else 0.0)
        if ttl <= 0:
            return
        now = time.monotonic()
        if len(self._response_cache) > 250:
            expired = [k for k, v in self._response_cache.items() if v[0] <= now]
            for k in expired:
                self._response_cache.pop(k, None)
        self._response_cache[(url, use_jina_html_on_block)] = (now + ttl, status, text, headers)

    def _choose_proxy(self) -> str | None:
        if settings.proxy_urls:
            return random.choice(settings.proxy_urls)
        return None

    def _build_target_url_and_headers(
        self,
        url: str,
        extra_headers: dict[str, str] | None = None,
        use_cf_worker: bool = False,
    ) -> tuple[str, dict[str, str]]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if extra_headers:
            headers.update(extra_headers)

        if use_cf_worker and settings.cf_worker_proxy_url:
            worker_base = settings.cf_worker_proxy_url.rstrip("/")
            target = f"{worker_base}/?url={quote(url, safe='')}"
            if settings.cf_worker_secret:
                headers["X-Proxy-Secret"] = settings.cf_worker_secret
            return target, headers

        return url, headers

    async def get_text(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 18.0,
        use_cf_worker_on_block: bool = True,
        use_jina_html_on_block: bool = True,
        use_cache: bool = True,
    ) -> tuple[int, str, dict[str, str]]:
        """
        Perform a GET request with TLS fingerprint impersonation, domain jitter,
        short-lived response caching, and in-flight deduplication per URL.
        Returns (status_code, response_text, response_headers).
        """
        if use_cache:
            cached = self._get_cached(url, use_jina_html_on_block)
            if cached is not None:
                return cached

        url_lock = self._get_url_lock(url)
        async with url_lock:
            if use_cache:
                cached = self._get_cached(url, use_jina_html_on_block)
                if cached is not None:
                    return cached

            domain = urlparse(url).netloc.lower()
            await self._throttle_domain(domain)
            self.record_external_call(url)

            proxy = self._choose_proxy()
            target_url, req_headers = self._build_target_url_and_headers(url, headers, use_cf_worker=False)

            status, text, resp_headers = await self._execute_get(
                target_url, req_headers, proxy, timeout, retry_on_429=(domain == "r.jina.ai")
            )

            # 1. If blocked (403/429/503) and Cloudflare Worker relay is configured, retry through Worker
            if (
                status in (0, 403, 429, 503)
                and use_cf_worker_on_block
                and settings.cf_worker_proxy_url
            ):
                logger.info("Retrying %s via Cloudflare Worker relay (initial status %d)", domain, status)
                cf_url, cf_headers = self._build_target_url_and_headers(url, headers, use_cf_worker=True)
                self.record_external_call(url)
                status, text, resp_headers = await self._execute_get(cf_url, cf_headers, proxy, timeout)

            # 2. Built-in zero-config edge HTML relay fallback for datacenter IPs (GitHub Actions Azure IPs)
            blocked_markers = (
                "403 Forbidden",
                "Warning: Target URL returned error 403",
                "Dbamy o Twoje bezpieczeństwo",
                "potwierdzenie, że nie jesteś robotem",
                "<title>Just a moment...</title>",
                "Attention Required! | Cloudflare",
            )
            if (
                status in (0, 403, 429, 503)
                and use_jina_html_on_block
                and not url.startswith("https://r.jina.ai/")
            ):
                jina_url = f"https://r.jina.ai/{url}"
                jina_headers = {
                    "User-Agent": USER_AGENTS[0],
                    "X-Return-Format": "html",
                }
                if settings.jina_api_key:
                    jina_headers["Authorization"] = f"Bearer {settings.jina_api_key}"
                logger.info("Retrying %s via edge HTML relay (initial status %d)", domain, status)
                await self._throttle_domain("r.jina.ai")
                self.record_external_call(jina_url)
                j_status, j_text, j_hdrs = await self._execute_get(
                    jina_url, jina_headers, proxy, timeout=22.0, retry_on_429=True
                )
                if (
                    j_status == 200
                    and j_text
                    and len(j_text) > 1200
                    and not any(m in j_text[:2500] for m in blocked_markers)
                ):
                    status, text, resp_headers = 200, j_text, j_hdrs
                else:
                    logger.info(
                        "Edge HTML relay returned HTTP %d (len=%d) for %s",
                        j_status,
                        len(j_text or ""),
                        domain,
                    )

            # 3. Secondary free relay fallback for smyk.com when both direct and r.jina.ai fail
            if (
                status in (0, 403, 429, 503)
                and use_jina_html_on_block
                and domain in ("www.smyk.com", "smyk.com")
            ):
                ao_url = f"https://api.allorigins.win/raw?url={quote(url, safe='')}"
                logger.info("Retrying %s via AllOrigins relay (previous status %d)", domain, status)
                await self._throttle_domain("api.allorigins.win")
                self.record_external_call(ao_url)
                ao_status, ao_text, ao_hdrs = await self._execute_get(
                    ao_url,
                    {"User-Agent": USER_AGENTS[0], "Accept": "text/html,*/*;q=0.8"},
                    proxy,
                    timeout=20.0,
                )
                if (
                    ao_status == 200
                    and ao_text
                    and len(ao_text) > 1200
                    and not any(m in ao_text[:2500] for m in blocked_markers)
                ):
                    status, text, resp_headers = 200, ao_text, ao_hdrs

            if use_cache:
                self._set_cached(url, use_jina_html_on_block, status, text, resp_headers)
            return status, text, resp_headers

    async def get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 18.0,
    ) -> tuple[int, dict | list | None]:
        json_headers = {"Accept": "application/json, text/plain, */*"}
        if headers:
            json_headers.update(headers)
        # Never route JSON API requests through HTML-only edge relays (r.jina.ai)
        status, text, _ = await self.get_text(
            url,
            headers=json_headers,
            timeout=timeout,
            use_jina_html_on_block=False,
        )
        if status >= 400 or not text:
            return status, None
        import json
        try:
            return status, json.loads(text)
        except Exception:
            return status, None

    async def _execute_get(
        self,
        url: str,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
        retry_on_429: bool = False,
    ) -> tuple[int, str, dict[str, str]]:
        transient_statuses = {0, 500, 502, 503, 504}
        if retry_on_429:
            transient_statuses.add(429)

        last_result: tuple[int, str, dict[str, str]] = (0, "", {})
        for attempt in range(2):
            last_result = await self._execute_get_once(url, headers, proxy, timeout)
            status, _, resp_hdrs = last_result
            if status not in transient_statuses or attempt == 1:
                return last_result
            backoff = random.uniform(1.5, 3.0)
            if status == 429:
                retry_after = resp_hdrs.get("retry-after", "")
                try:
                    backoff = min(max(float(retry_after), 4.0), 12.0)
                except ValueError:
                    backoff = 6.0
            logger.debug(
                "Transient HTTP %d for %s; retrying in %.1fs (attempt %d/2)",
                status,
                url,
                backoff,
                attempt + 2,
            )
            await asyncio.sleep(backoff)
        return last_result

    async def _execute_get_once(
        self,
        url: str,
        headers: dict[str, str],
        proxy: str | None,
        timeout: float,
    ) -> tuple[int, str, dict[str, str]]:
        if HAS_CURL_CFFI:
            # Always pair impersonate profile with its exact matching User-Agent
            profiles = list(BROWSER_PROFILES)
            # Prefer safari17_0 first as it passes Polish retail WAFs (MediaExpert, Empik, Ceneo)
            for idx, (impersonate, matching_ua) in enumerate(profiles[:2]):
                req_hdrs = dict(headers)
                req_hdrs["User-Agent"] = matching_ua
                try:
                    async with CurlAsyncSession(
                        impersonate=impersonate,
                        proxy=proxy,
                        timeout=timeout,
                    ) as session:
                        resp = await session.get(url, headers=req_hdrs, allow_redirects=True)
                        hdrs = {k.lower(): v for k, v in resp.headers.items()}
                        if resp.status_code not in (403, 405) or idx == 1:
                            return resp.status_code, resp.text, hdrs
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("curl_cffi (%s) failed for %s (%s)", impersonate, url, exc)

        try:
            async with httpx.AsyncClient(
                proxy=proxy,
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url, headers=headers)
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status_code, resp.text, hdrs
        except Exception as exc:
            logger.warning("HTTP request failed for %s: %s", url, exc)
            return 0, "", {}


http_client = AntiBlockHttpClient()
