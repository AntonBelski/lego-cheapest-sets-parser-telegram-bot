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

BROWSER_IMPERSONATIONS = ("chrome124", "chrome120", "safari17_0")

USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
)


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
        self.total_requests_made: int = 0

    def _get_domain_lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._domain_locks:
            self._domain_locks[domain] = asyncio.Lock()
        return self._domain_locks[domain]

    async def _throttle_domain(self, domain: str) -> None:
        lock = self._get_domain_lock(domain)
        async with lock:
            last = self._domain_last_call.get(domain, 0.0)
            now = time.monotonic()
            delay = random.uniform(settings.min_domain_delay_sec, settings.max_domain_delay_sec)
            wait_for = (last + delay) - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._domain_last_call[domain] = time.monotonic()

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
    ) -> tuple[int, str, dict[str, str]]:
        """
        Perform a GET request with TLS fingerprint impersonation and domain jitter.
        Returns (status_code, response_text, response_headers).
        """
        domain = urlparse(url).netloc.lower()
        await self._throttle_domain(domain)
        self.total_requests_made += 1

        proxy = self._choose_proxy()
        target_url, req_headers = self._build_target_url_and_headers(url, headers, use_cf_worker=False)

        status, text, resp_headers = await self._execute_get(target_url, req_headers, proxy, timeout)

        # 1. If blocked (403/429/503) and Cloudflare Worker relay is configured, retry through Worker
        if (
            status in (0, 403, 429, 503)
            and use_cf_worker_on_block
            and settings.cf_worker_proxy_url
        ):
            logger.info("Retrying %s via Cloudflare Worker relay (initial status %d)", domain, status)
            cf_url, cf_headers = self._build_target_url_and_headers(url, headers, use_cf_worker=True)
            self.total_requests_made += 1
            status, text, resp_headers = await self._execute_get(cf_url, cf_headers, proxy, timeout)

        # 2. Built-in zero-config edge HTML relay fallback for datacenter IPs (GitHub Actions Azure IPs)
        if (
            status in (0, 403, 429, 503)
            and use_jina_html_on_block
            and not url.startswith("https://r.jina.ai/")
        ):
            jina_url = f"https://r.jina.ai/{url}"
            jina_headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "X-Return-Format": "html",
            }
            logger.info("Retrying %s via edge HTML relay (initial status %d)", domain, status)
            self.total_requests_made += 1
            j_status, j_text, j_hdrs = await self._execute_get(jina_url, jina_headers, proxy, timeout=22.0)
            if j_status == 200 and j_text and len(j_text) > 1200 and "403 Forbidden" not in j_text[:400]:
                return 200, j_text, j_hdrs

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
        status, text, _ = await self.get_text(url, headers=json_headers, timeout=timeout)
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
    ) -> tuple[int, str, dict[str, str]]:
        if HAS_CURL_CFFI:
            impersonate = random.choice(BROWSER_IMPERSONATIONS)
            try:
                async with CurlAsyncSession(
                    impersonate=impersonate,
                    proxy=proxy,
                    timeout=timeout,
                ) as session:
                    resp = await session.get(url, headers=headers, allow_redirects=True)
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    return resp.status_code, resp.text, hdrs
            except Exception as exc:
                logger.debug("curl_cffi request failed for %s (%s), falling back to httpx", url, exc)

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
