from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import numpy as np
from PIL import Image

from bot.models import PriceStats, Subscription
from bot.services.http_client import http_client

logger = logging.getLogger(__name__)


async def fetch_set_image_pil(set_number: str, preferred_url: Optional[str] = None) -> Optional[Image.Image]:
    """
    Download the LEGO set image from `preferred_url`, Brickset, or BrickLink
    so it can be embedded into the Telegram notification graphic.
    """
    candidate_urls = []
    if preferred_url and preferred_url.startswith("http"):
        candidate_urls.append(preferred_url)
    candidate_urls.extend(
        [
            f"https://images.brickset.com/sets/images/{set_number}-1.jpg",
            f"https://img.bricklink.com/ItemImage/SN/0/{set_number}-1.png",
        ]
    )

    import httpx
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for url in candidate_urls:
            try:
                resp = await client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                )
                if resp.status_code == 200 and resp.content:
                    img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
                    return img
            except Exception as exc:
                logger.debug("Could not fetch set image from %s: %s", url, exc)
    return None


async def fetch_set_image_bytes(
    set_number: str, preferred_url: Optional[str] = None
) -> Optional[bytes]:
    """
    Download just the official LEGO set image bytes (PNG/JPEG) for deal notifications.
    """
    pil_img = await fetch_set_image_pil(set_number, preferred_url)
    if pil_img is None:
        return None
    buf = io.BytesIO()
    bg = Image.new("RGBA", pil_img.size, (255, 255, 255, 255))
    composite = Image.alpha_composite(bg, pil_img).convert("RGB")
    composite.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


async def render_deal_card_with_chart(
    subscription: Subscription,
    stats: PriceStats,
    current_best_pln: Optional[float] = None,
) -> bytes:
    """
    Render a high-DPI composite PNG image containing:
    1) Top/Left Panel: Official LEGO Set Photo + KPI Summary Badges
       (LEGO.pl Catalog Price, Current Lowest Price, 30-Day Low, All-Time Low)
    2) Right/Main Panel: Daily Lowest Price Dynamics Chart (PLN per day),
       including shaded target price range [price_from, price_to] and MSRP reference line.
    Returns PNG bytes ready to send via Telegram `BufferedInputFile`.
    """
    set_img = await fetch_set_image_pil(subscription.set_number, subscription.image_url)

    fig = plt.figure(figsize=(11.5, 5.4), dpi=150, facecolor="#0f172a")
    gs = GridSpec(1, 12, figure=fig, wspace=0.5)

    # Left panel (4 columns): Set Image + KPI badges
    ax_img = fig.add_subplot(gs[0, :4])
    ax_img.set_facecolor("#1e293b")
    ax_img.axis("off")

    if set_img is not None:
        # Place set image nicely on a clean white card area at the top of left panel
        bg = Image.new("RGBA", set_img.size, (255, 255, 255, 255))
        composite = Image.alpha_composite(bg, set_img).convert("RGB")
        ax_img.imshow(np.asarray(composite))
        ax_img.set_title(
            f"LEGO {subscription.set_number}\n{(subscription.set_name or '')[:32]}",
            color="#f8fafc",
            fontsize=11,
            fontweight="bold",
            pad=10,
        )
    else:
        ax_img.text(
            0.5,
            0.5,
            f"LEGO {subscription.set_number}\n{subscription.set_name or ''}",
            color="#f8fafc",
            fontsize=13,
            fontweight="bold",
            ha="center",
            va="center",
            transform=ax_img.transAxes,
        )

    # Right panel (8 columns): Daily Lowest Price Dynamics Chart
    ax = fig.add_subplot(gs[0, 4:])
    ax.set_facecolor("#1e293b")
    for spine in ax.spines.values():
        spine.set_color("#334155")

    series = list(stats.daily_lowest_series)
    if not series and current_best_pln:
        today_s = datetime.utcnow().strftime("%Y-%m-%d")
        series = [(today_s, current_best_pln)]

    dates: list[datetime] = []
    prices: list[float] = []
    for d_str, p_val in series:
        try:
            dates.append(datetime.strptime(d_str, "%Y-%m-%d"))
            prices.append(float(p_val))
        except ValueError:
            continue

    if dates and prices:
        if len(dates) == 1:
            ax.scatter(dates, prices, color="#38bdf8", s=80, zorder=5, label="Daily Lowest (PLN)")
        else:
            ax.plot(
                dates,
                prices,
                color="#38bdf8",
                linewidth=2.5,
                marker="o",
                markersize=4.5,
                zorder=4,
                label="Daily Lowest Price (PLN)",
            )
            ax.fill_between(dates, prices, min(prices) * 0.94, color="#38bdf8", alpha=0.14)

        # Annotate latest price point
        ax.scatter([dates[-1]], [prices[-1]], color="#22c55e", s=70, zorder=6)
        ax.annotate(
            f"{prices[-1]:.0f} PLN",
            xy=(dates[-1], prices[-1]),
            xytext=(0, 9),
            textcoords="offset points",
            color="#4ade80",
            fontsize=9,
            fontweight="bold",
            ha="center",
        )

    # Draw LEGO.pl MSRP reference line if known
    if subscription.msrp_pln and subscription.msrp_pln > 0:
        ax.axhline(
            subscription.msrp_pln,
            color="#f43f5e",
            linestyle="--",
            linewidth=1.4,
            alpha=0.85,
            label=f"LEGO.pl MSRP: {subscription.msrp_pln:.0f} PLN",
        )

    # Draw User Target Price ceiling if set
    if subscription.price_to is not None and subscription.price_to > 0:
        ax.axhline(
            subscription.price_to,
            color="#22c55e",
            linestyle=":",
            linewidth=1.6,
            alpha=0.9,
            label=f"Target Max: {subscription.price_to:.0f} PLN",
        )
        low_bound = subscription.price_from if subscription.price_from > 0 else 0
        ax.axhspan(low_bound, subscription.price_to, color="#22c55e", alpha=0.07)

    # Draw All-time low line if available
    if stats.lowest_all_time_pln:
        ax.axhline(
            stats.lowest_all_time_pln,
            color="#eab308",
            linestyle="-.",
            linewidth=1.2,
            alpha=0.8,
            label=f"All-Time Low: {stats.lowest_all_time_pln:.0f} PLN",
        )

    ax.set_title(
        f"Daily Lowest Price Dynamics — LEGO {subscription.set_number}",
        color="#f8fafc",
        fontsize=12,
        fontweight="bold",
        pad=10,
    )
    ax.set_ylabel("Total Price with Delivery (PLN)", color="#cbd5e1", fontsize=9.5)
    ax.tick_params(colors="#cbd5e1", labelsize=8.5)
    ax.grid(True, linestyle="--", linewidth=0.5, color="#334155", alpha=0.7)

    if len(dates) > 1:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        fig.autofmt_xdate(rotation=25)

    ax.legend(
        loc="upper right",
        facecolor="#0f172a",
        edgecolor="#334155",
        labelcolor="#f8fafc",
        fontsize=8,
    )

    # Footer summary bar
    msrp_txt = f"{subscription.msrp_pln:.2f} PLN" if subscription.msrp_pln else "N/A"
    low30_txt = f"{stats.lowest_30d_pln:.2f} PLN" if stats.lowest_30d_pln else "N/A"
    lowall_txt = f"{stats.lowest_all_time_pln:.2f} PLN" if stats.lowest_all_time_pln else "N/A"
    curr_txt = f"{current_best_pln:.2f} PLN" if current_best_pln else "N/A"

    footer = (
        f"LEGO.pl MSRP: {msrp_txt}   |   Current Best: {curr_txt}   |   "
        f"30-Day Low: {low30_txt}   |   All-Time Low: {lowall_txt}   |   "
        f"Target Range: {subscription.formatted_range}"
    )
    fig.text(
        0.5,
        0.02,
        footer,
        ha="center",
        va="bottom",
        color="#94a3b8",
        fontsize=8.5,
        fontweight="bold",
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()
