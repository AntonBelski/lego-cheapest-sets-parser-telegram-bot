from __future__ import annotations

import html
import json
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import settings
from bot.db import Database
from bot.keyboards import main_menu_keyboard, set_actions_keyboard
from bot.models import (
    format_retirement_status_and_countdown,
    format_source_label,
    sanitize_url_for_telegram,
)
from bot.services.chart import render_deal_card_with_chart
from bot.services.currency import currency_converter
from bot.services.http_client import http_client
from bot.services.monitor import LegoMonitorService

logger = logging.getLogger(__name__)


class SubscriptionForm(StatesGroup):
    waiting_for_set_number = State()
    waiting_for_price_from = State()
    waiting_for_price_to = State()


async def safe_callback_answer(callback: CallbackQuery, text: Optional[str] = None) -> None:
    """Answer inline callback query without failing if the query is >60s old (e.g. GitHub Actions cron mode)."""
    try:
        if text:
            await callback.answer(text)
        else:
            await callback.answer()
    except TelegramBadRequest as exc:
        logger.debug("Ignoring stale callback answer error: %s", exc)
    except Exception as exc:
        logger.debug("Callback answer failed: %s", exc)


def parse_optional_bound(raw: str, is_min: bool) -> Optional[float]:
    cleaned = (raw or "").strip().lower().replace("pln", "").replace("zł", "").strip()
    if cleaned in ("", "-", "skip", "none", "null", "inf", "infinity", "∞", "pomiń"):
        return 0.0 if is_min else None
    val = float(cleaned.replace(",", "."))
    if val <= 0:
        return 0.0 if is_min else None
    return round(val, 2)


def build_dispatcher(db: Database, monitor: LegoMonitorService) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    router = Router()
    dp.include_router(router)

    def _is_authorized(chat_id: int) -> bool:
        if settings.telegram_chat_id is None:
            return True
        return chat_id == settings.telegram_chat_id

    @router.message(Command("start", "help"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        if not message.chat or not _is_authorized(message.chat.id):
            return
        await state.clear()
        text = (
            "🧱 <b>LEGO Poland & EU Cheapest Set Tracker Bot</b>\n\n"
            "I check <b>13 Polish & European marketplaces/retailers</b> every <b>5 minutes</b> "
            "for <b>New (Sealed)</b> and <b>Open Box (Sealed Bags)</b> original LEGO sets, "
            "convert all currencies to <b>PLN</b> (NBP real-time rates), include cheapest delivery, "
            "and track set <b>retirement dates</b> every 5 days.\n\n"
            "<b>Monitored Sites:</b>\n"
            "• <code>olx.pl</code>, <code>vinted.pl</code>, <code>allegrolokalnie.pl</code>, <code>allegro.pl</code>\n"
            "• <code>promoklocki.pl</code>, <code>zklockow.pl</code>, <code>ceneo.pl</code>\n"
            "• <code>brickmerge.de</code> (only EU stores shipping to PL), <code>ebay.pl</code>, <code>bricklink.com</code>\n"
            "• <code>smyk.com</code>, <code>empik.com</code>, <code>mediaexpert.pl</code>\n\n"
            "<b>Commands:</b>\n"
            "• /add — Add a set interactively (with <b>Skip</b> for 0 or ∞)\n"
            "• <code>/add 21348 - 1500</code> — Quick add (Price From = 0 PLN, Price To = 1500 PLN)\n"
            "• /list — View & manage your subscribed sets\n"
            "• <code>/deals 21348</code> — Show cheapest current offers across all sites\n"
            "• <code>/chart 21348</code> — View set image + daily lowest price chart\n"
            "• <code>/check [set]</code> — Force an immediate scan\n"
            "• /retire — Check retirement dates & countdowns\n"
            "• /logs — View latest 5-min run logs & scraped URLs stored in DB\n"
            "• /status — View scraper health & NBP FX rates\n"
            "• <code>/remove 21348</code> — Unsubscribe from a set"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())

    @router.callback_query(F.data == "menu:main")
    async def cb_menu_main(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback)
        await cmd_start(callback.message, state)

    # -------------------------------------------------------------------------
    # /add Command & Interactive Wizard (Price From / Price To with Skip)
    # -------------------------------------------------------------------------
    @router.message(Command("add"))
    async def cmd_add(message: Message, command: CommandObject, state: FSMContext) -> None:
        if not message.chat or not _is_authorized(message.chat.id):
            return

        args = (command.args or "").strip().split()
        if args:
            set_number = args[0].strip()
            if not set_number.isdigit():
                await message.answer("❌ Set number must be digits (e.g. <code>21348</code>).", parse_mode="HTML")
                return
            try:
                p_from = parse_optional_bound(args[1], is_min=True) if len(args) >= 2 else 0.0
                p_to = parse_optional_bound(args[2], is_min=False) if len(args) >= 3 else None
            except ValueError:
                await message.answer(
                    "❌ Invalid price format. Example: <code>/add 21348 - 1500</code> or <code>/add 21348 1100 1500</code>",
                    parse_mode="HTML",
                )
                return

            await _finalize_subscription(message, message.chat.id, set_number, p_from or 0.0, p_to)
            return

        await state.set_state(SubscriptionForm.waiting_for_set_number)
        await message.answer(
            "🧱 <b>Step 1/3:</b> Send the <b>LEGO Set Number</b> you want to track (e.g. <code>21348</code> or <code>10316</code>):",
            parse_mode="HTML",
        )

    @router.callback_query(F.data == "menu:add")
    async def cb_menu_add(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback)
        await state.set_state(SubscriptionForm.waiting_for_set_number)
        await callback.message.answer(
            "🧱 <b>Step 1/3:</b> Send the <b>LEGO Set Number</b> you want to track (e.g. <code>21348</code>):",
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("set:edit:"))
    async def cb_edit_set(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback)
        set_number = (callback.data or "").split(":")[-1]
        await state.update_data(set_number=set_number)
        await state.set_state(SubscriptionForm.waiting_for_price_from)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Skip Price From (0 PLN)", callback_data="wiz:skip_from")]
            ]
        )
        await callback.message.answer(
            f"✏️ Editing <b>LEGO {html.escape(set_number)}</b>\n\n"
            f"💰 <b>Step 2/3:</b> Enter <b>Price From (PLN)</b>, or click <b>Skip (0 PLN)</b>:",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @router.message(SubscriptionForm.waiting_for_set_number)
    async def process_set_number(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if not raw.isdigit():
            await message.answer("❌ Please enter a numeric LEGO set number (e.g. <code>21348</code>):", parse_mode="HTML")
            return
        await state.update_data(set_number=raw)
        await state.set_state(SubscriptionForm.waiting_for_price_from)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Skip Price From (0 PLN)", callback_data="wiz:skip_from")]
            ]
        )
        await message.answer(
            f"✅ Set <b>LEGO {html.escape(raw)}</b> selected.\n\n"
            f"💰 <b>Step 2/3:</b> Enter minimum price <b>Price From (PLN)</b>, or press <b>Skip (0 PLN)</b>:",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @router.callback_query(F.data == "wiz:skip_from")
    async def cb_skip_price_from(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.message:
            return
        await safe_callback_answer(callback, "Price From set to 0 PLN")
        await state.update_data(price_from=0.0)
        await state.set_state(SubscriptionForm.waiting_for_price_to)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Skip Price To (∞ Infinity)", callback_data="wiz:skip_to")]
            ]
        )
        await callback.message.answer(
            "💰 <b>Step 3/3:</b> Enter maximum price <b>Price To (PLN)</b> (e.g. <code>1500</code>), "
            "or press <b>Skip (∞ Infinity)</b>:",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @router.message(SubscriptionForm.waiting_for_price_from)
    async def process_price_from(message: Message, state: FSMContext) -> None:
        try:
            p_from = parse_optional_bound(message.text or "", is_min=True) or 0.0
        except ValueError:
            await message.answer("❌ Please enter a valid number in PLN or <code>-</code> to skip:", parse_mode="HTML")
            return

        await state.update_data(price_from=p_from)
        await state.set_state(SubscriptionForm.waiting_for_price_to)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Skip Price To (∞ Infinity)", callback_data="wiz:skip_to")]
            ]
        )
        await message.answer(
            f"✅ Price From: <b>{p_from:.0f} PLN</b>.\n\n"
            f"💰 <b>Step 3/3:</b> Enter maximum price <b>Price To (PLN)</b>, or press <b>Skip (∞ Infinity)</b>:",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @router.callback_query(F.data == "wiz:skip_to")
    async def cb_skip_price_to(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.message:
            return
        await safe_callback_answer(callback, "Price To set to ∞ Infinity")
        data = await state.get_data()
        await state.clear()
        set_number = data.get("set_number", "")
        p_from = float(data.get("price_from", 0.0) or 0.0)
        await _finalize_subscription(callback.message, callback.message.chat.id, set_number, p_from, None)

    @router.message(SubscriptionForm.waiting_for_price_to)
    async def process_price_to(message: Message, state: FSMContext) -> None:
        try:
            p_to = parse_optional_bound(message.text or "", is_min=False)
        except ValueError:
            await message.answer("❌ Please enter a valid number in PLN or <code>-</code> to skip:", parse_mode="HTML")
            return

        data = await state.get_data()
        await state.clear()
        set_number = data.get("set_number", "")
        p_from = float(data.get("price_from", 0.0) or 0.0)
        if p_to is not None and p_to < p_from:
            p_from, p_to = p_to, p_from
        await _finalize_subscription(message, message.chat.id, set_number, p_from, p_to)

    async def _finalize_subscription(
        msg: Message,
        chat_id: int,
        set_number: str,
        price_from: float,
        price_to: Optional[float],
    ) -> None:
        sub = await db.upsert_subscription(
            chat_id=chat_id,
            set_number=set_number,
            price_from=price_from,
            price_to=price_to,
        )
        status_msg = await msg.answer(
            f"⏳ Subscribed to <b>LEGO {html.escape(set_number)}</b> "
            f"(Range: <b>{html.escape(sub.formatted_range)}</b>).\n"
            f"Fetching LEGO.pl MSRP, retirement date, and historical prices...",
            parse_mode="HTML",
        )
        meta = await monitor.enrich_set_on_subscribe(set_number)
        updated_sub = await db.get_subscription(chat_id, set_number) or sub
        stats = await db.get_price_stats(set_number)

        msrp_str = f"{updated_sub.msrp_pln:.2f} PLN" if updated_sub.msrp_pln else "Fetching on next scan"
        low30_str = f"{stats.lowest_30d_pln:.2f} PLN" if stats.lowest_30d_pln else "Scanning..."
        lowall_str = f"{stats.lowest_all_time_pln:.2f} PLN" if stats.lowest_all_time_pln else "Scanning..."
        ret_str = format_retirement_status_and_countdown(
            updated_sub.retirement_date, updated_sub.retirement_source
        )

        await status_msg.edit_text(
            f"✅ <b>Subscription Active: LEGO {html.escape(set_number)}</b>\n"
            f"🧱 <b>Name:</b> {html.escape(updated_sub.set_name or (meta.name if meta else 'LEGO Set'))}\n"
            f"🎯 <b>Your Price Range:</b> <b>{html.escape(updated_sub.formatted_range)}</b>\n"
            f"🏷 <b>LEGO.pl MSRP:</b> {msrp_str}\n"
            f"📉 <b>Lowest (30d):</b> {low30_str}\n"
            f"🏆 <b>Lowest (All-Time):</b> {lowall_str}\n"
            f"⏳ <b>Retirement Status:</b> {html.escape(ret_str)}\n\n"
            f"<i>First scan will perform a deep multi-page scan across all 13 sites, and then check every 5 minutes for new offers & price drops!</i>",
            parse_mode="HTML",
            reply_markup=set_actions_keyboard(set_number),
        )

    # -------------------------------------------------------------------------
    # /list & Subscription Management
    # -------------------------------------------------------------------------
    @router.message(Command("list"))
    async def cmd_list(message: Message) -> None:
        if not message.chat or not _is_authorized(message.chat.id):
            return
        await _send_subscriptions_list(message, message.chat.id)

    @router.callback_query(F.data == "menu:list")
    async def cb_menu_list(callback: CallbackQuery) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback)
        await _send_subscriptions_list(callback.message, callback.message.chat.id)

    async def _send_subscriptions_list(msg: Message, chat_id: int) -> None:
        subs = await db.list_subscriptions(chat_id=chat_id)
        if not subs:
            await msg.answer(
                "📭 You don't have any active LEGO set subscriptions yet.\n"
                "Click <b>➕ Add LEGO Set</b> or use <code>/add 21348 - 1500</code>.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )
            return

        await msg.answer(
            f"📋 <b>Your Subscribed LEGO Sets ({len(subs)}):</b>",
            parse_mode="HTML",
        )
        for sub in subs:
            stats = await db.get_price_stats(sub.set_number)
            msrp_txt = f"{sub.msrp_pln:.2f} PLN" if sub.msrp_pln else "N/A"
            low30_txt = f"{stats.lowest_30d_pln:.2f} PLN" if stats.lowest_30d_pln else "N/A"
            lowall_txt = f"{stats.lowest_all_time_pln:.2f} PLN" if stats.lowest_all_time_pln else "N/A"
            ret_txt = format_retirement_status_and_countdown(
                sub.retirement_date, sub.retirement_source
            )

            card_text = (
                f"🧱 <b>LEGO {html.escape(sub.set_number)} — {html.escape(sub.set_name or 'LEGO Set')}</b>\n"
                f"🎯 <b>Target Price Range:</b> <b>{html.escape(sub.formatted_range)}</b>\n"
                f"🏷 <b>LEGO.pl Catalog Price:</b> {msrp_txt}\n"
                f"📉 <b>Lowest (Last 30 Days):</b> {low30_txt}\n"
                f"🏆 <b>Lowest (All Time):</b> {lowall_txt}\n"
                f"⏳ <b>Retirement Status:</b> {html.escape(ret_txt)}"
            )
            await msg.answer(
                card_text,
                parse_mode="HTML",
                reply_markup=set_actions_keyboard(sub.set_number),
            )

    # -------------------------------------------------------------------------
    # /remove Command & Callback
    # -------------------------------------------------------------------------
    @router.message(Command("remove"))
    async def cmd_remove(message: Message, command: CommandObject) -> None:
        if not message.chat or not _is_authorized(message.chat.id):
            return
        set_number = (command.args or "").strip()
        if not set_number:
            await message.answer("Usage: <code>/remove 21348</code>", parse_mode="HTML")
            return
        removed = await db.remove_subscription(message.chat.id, set_number)
        if removed:
            await message.answer(
                f"🗑 Removed <b>LEGO {html.escape(set_number)}</b> from your subscriptions.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await message.answer(
                f"⚠️ Set <b>{html.escape(set_number)}</b> was not in your subscriptions.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )

    @router.callback_query(F.data.startswith("set:remove:"))
    async def cb_remove_set(callback: CallbackQuery) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        set_number = (callback.data or "").split(":")[-1]
        await db.remove_subscription(callback.message.chat.id, set_number)
        await safe_callback_answer(callback, f"Removed LEGO {set_number}")
        await callback.message.edit_text(
            f"🗑 Unsubscribed from <b>LEGO {html.escape(set_number)}</b>.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )

    # -------------------------------------------------------------------------
    # /chart Command & Callback
    # -------------------------------------------------------------------------
    @router.message(Command("chart"))
    async def cmd_chart(message: Message, command: CommandObject) -> None:
        if not message.chat or not _is_authorized(message.chat.id):
            return
        set_number = (command.args or "").strip()
        if not set_number:
            await message.answer("Usage: <code>/chart 21348</code>", parse_mode="HTML")
            return
        await _send_chart_for_set(message, message.chat.id, set_number)

    @router.callback_query(F.data.startswith("set:chart:"))
    async def cb_chart_set(callback: CallbackQuery) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback, "Generating price chart...")
        set_number = (callback.data or "").split(":")[-1]
        await _send_chart_for_set(callback.message, callback.message.chat.id, set_number)

    async def _send_chart_for_set(msg: Message, chat_id: int, set_number: str) -> None:
        sub = await db.get_subscription(chat_id, set_number)
        if not sub:
            sub = await db.upsert_subscription(chat_id, set_number, 0.0, None)
            await monitor.enrich_set_on_subscribe(set_number)
            sub = await db.get_subscription(chat_id, set_number) or sub

        stats = await db.get_price_stats(set_number)
        latest = await db.get_latest_offers(set_number, limit=1)
        curr_best = float(latest[0]["total_price_pln"]) if latest else stats.lowest_30d_pln

        png_bytes = await render_deal_card_with_chart(sub, stats, current_best_pln=curr_best)
        photo = BufferedInputFile(png_bytes, filename=f"lego_{set_number}_chart.png")

        ret_str = format_retirement_status_and_countdown(
            sub.retirement_date, sub.retirement_source
        )
        low30_src = format_source_label(stats.lowest_30d_source)
        lowall_src = format_source_label(stats.lowest_all_time_source)
        caption = (
            f"📈 <b>Daily Lowest Price Dynamics: LEGO {html.escape(set_number)}</b>\n"
            f"🧱 <b>{html.escape(sub.set_name or '')}</b>\n"
            f"🏷 <b>Official LEGO Catalog Price:</b> {f'{sub.msrp_pln:.2f} PLN' if sub.msrp_pln else 'N/A'}\n"
            f"📉 <b>Lowest (Last 30 Days):</b> {f'{stats.lowest_30d_pln:.2f} PLN ({low30_src})' if stats.lowest_30d_pln else 'N/A'}\n"
            f"🏆 <b>Lowest (All Time):</b> {f'{stats.lowest_all_time_pln:.2f} PLN ({lowall_src})' if stats.lowest_all_time_pln else 'N/A'}\n"
            f"⏳ <b>Retirement Status:</b> {html.escape(ret_str)}\n"
            f"🎯 <b>Your Range:</b> {html.escape(sub.formatted_range)}"
        )
        await msg.answer_photo(
            photo=photo,
            caption=caption,
            parse_mode="HTML",
            reply_markup=set_actions_keyboard(set_number),
        )

    # -------------------------------------------------------------------------
    # /deals Command & Callback
    # -------------------------------------------------------------------------
    @router.message(Command("deals"))
    async def cmd_deals(message: Message, command: CommandObject) -> None:
        if not message.chat or not _is_authorized(message.chat.id):
            return
        set_number = (command.args or "").strip()
        if not set_number:
            await message.answer("Usage: <code>/deals 21348</code>", parse_mode="HTML")
            return
        await _send_top_deals(message, set_number)

    @router.callback_query(F.data.startswith("set:deals:"))
    async def cb_deals_set(callback: CallbackQuery) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback)
        set_number = (callback.data or "").split(":")[-1]
        await _send_top_deals(callback.message, set_number)

    async def _send_top_deals(msg: Message, set_number: str) -> None:
        rows = await db.get_latest_offers(set_number, limit=20)
        if not rows:
            wait_m = await msg.answer(f"🔍 Scanning live offers for <b>LEGO {html.escape(set_number)}</b>...", parse_mode="HTML")
            await monitor.scan_single_set(set_number)
            rows = await db.get_latest_offers(set_number, limit=20)
            await wait_m.delete()

        if not rows:
            await msg.answer(
                f"No offers found yet for <b>LEGO {html.escape(set_number)}</b>.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )
            return

        stats = await db.get_price_stats(set_number)
        lines = [
            f"🛒 <b>Top Cheapest Current Offers: LEGO {html.escape(set_number)}</b>",
            f"📉 30d Low: <b>{f'{stats.lowest_30d_pln:.2f} PLN' if stats.lowest_30d_pln else 'N/A'}</b> | "
            f"🏆 All-Time Low: <b>{f'{stats.lowest_all_time_pln:.2f} PLN' if stats.lowest_all_time_pln else 'N/A'}</b>",
            "",
        ]
        for idx, r in enumerate(rows, start=1):
            total_p = float(r["total_price_pln"])
            prev_p = r.get("previous_price_pln")
            deliv_known = bool(r["delivery_known"])
            deliv_p = r["delivery_price_pln"]
            if not deliv_known or deliv_p is None:
                p_str = f"{total_p:.2f} PLN (delivery price unknown)"
            elif float(deliv_p) == 0:
                p_str = f"{total_p:.2f} PLN (free delivery)"
            else:
                item_p = float(r.get("item_price_pln") or round(total_p - float(deliv_p), 2))
                p_str = f"{total_p:.2f} PLN ({item_p:.2f} PLN + {float(deliv_p):.2f} PLN delivery)"

            if prev_p is not None and abs(float(prev_p) - total_p) >= 1.0:
                p_str = f"<s>{float(prev_p):.2f} PLN</s> ➔ {p_str}"

            safe_href = html.escape(sanitize_url_for_telegram(str(r["url"])), quote=True)
            lines.append(
                f"{idx}. <a href=\"{safe_href}\"><b>{html.escape(str(r['store_name']))}</b></a> — "
                f"<a href=\"{safe_href}\"><b>{p_str} ↗</b></a> [<i>{html.escape(str(r['condition']))}</i>]"
            )

        await msg.answer(
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=set_actions_keyboard(set_number),
        )

    # -------------------------------------------------------------------------
    # /check Command & Callbacks
    # -------------------------------------------------------------------------
    @router.message(Command("check"))
    async def cmd_check(message: Message, command: CommandObject) -> None:
        if not message.chat or not _is_authorized(message.chat.id):
            return
        set_number = (command.args or "").strip()
        if set_number:
            wait = await message.answer(f"🔍 Scanning all 13 sites for <b>LEGO {html.escape(set_number)}</b>...", parse_mode="HTML")
            offers = await monitor.scan_single_set(set_number)
            best_str = offers[0].formatted_price if offers else "No offers found"
            await wait.edit_text(
                f"✅ Scan finished for <b>LEGO {html.escape(set_number)}</b>: "
                f"found <b>{len(offers)}</b> valid offers.\n"
                f"💰 Cheapest right now: <b>{html.escape(best_str)}</b>",
                parse_mode="HTML",
                reply_markup=set_actions_keyboard(set_number),
            )
        else:
            wait = await message.answer("🔄 Scanning all subscribed LEGO sets across all 13 sites...", parse_mode="HTML")
            res = await monitor.scan_all_subscribed_sets()
            total_found = sum(len(v) for v in res.values())
            await wait.edit_text(
                f"✅ Full scan complete! Checked <b>{len(res)}</b> sets, recorded <b>{total_found}</b> valid offers.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )

    @router.callback_query(F.data == "menu:check_all")
    async def cb_check_all(callback: CallbackQuery) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback, "Starting scan...")
        wait = await callback.message.answer("🔄 Scanning all subscribed LEGO sets across all 13 sites...", parse_mode="HTML")
        res = await monitor.scan_all_subscribed_sets()
        total_found = sum(len(v) for v in res.values())
        await wait.edit_text(
            f"✅ Full scan complete! Checked <b>{len(res)}</b> sets, recorded <b>{total_found}</b> valid offers.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )

    @router.callback_query(F.data.startswith("set:check:"))
    async def cb_check_single(callback: CallbackQuery) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback, "Scanning set...")
        set_number = (callback.data or "").split(":")[-1]
        wait = await callback.message.answer(f"🔍 Scanning 13 sites for <b>LEGO {html.escape(set_number)}</b>...", parse_mode="HTML")
        offers = await monitor.scan_single_set(set_number)
        best_str = offers[0].formatted_price if offers else "No offers found"
        await wait.edit_text(
            f"✅ Scan complete for <b>LEGO {html.escape(set_number)}</b>: "
            f"found <b>{len(offers)}</b> offers.\n"
            f"💰 Cheapest: <b>{html.escape(best_str)}</b>",
            parse_mode="HTML",
            reply_markup=set_actions_keyboard(set_number),
        )

    # -------------------------------------------------------------------------
    # /retire Command & Callback
    # -------------------------------------------------------------------------
    @router.message(Command("retire"))
    async def cmd_retire(message: Message) -> None:
        if not message.chat or not _is_authorized(message.chat.id):
            return
        wait = await message.answer("⏳ Checking retirement dates on Brickset, BrickEconomy & Promoklocki...", parse_mode="HTML")
        summaries = await monitor.check_all_retirement_dates(force_report_chat_id=message.chat.id)
        if not summaries:
            await wait.edit_text(
                "📭 No subscribed sets to check. Use /add first!",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )
            return
        await wait.edit_text(
            "⏳ <b>Subscribed LEGO Sets Retirement Status:</b>\n\n" + "\n".join(summaries),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )

    @router.callback_query(F.data == "menu:retire")
    async def cb_menu_retire(callback: CallbackQuery) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback, "Checking retirement dates...")
        wait = await callback.message.answer("⏳ Checking retirement dates on Brickset, BrickEconomy & Promoklocki...", parse_mode="HTML")
        summaries = await monitor.check_all_retirement_dates(force_report_chat_id=callback.message.chat.id)
        if not summaries:
            await wait.edit_text(
                "📭 No subscribed sets to check.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )
            return
        await wait.edit_text(
            "⏳ <b>Subscribed LEGO Sets Retirement Status:</b>\n\n" + "\n".join(summaries),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )

    # -------------------------------------------------------------------------
    # /logs Command & Callback (Persistent 5-min Run Logs from DB)
    # -------------------------------------------------------------------------
    @router.message(Command("logs"))
    async def cmd_logs(message: Message) -> None:
        if not message.chat or not _is_authorized(message.chat.id):
            return
        await _send_recent_logs(message)

    @router.callback_query(F.data == "menu:logs")
    async def cb_menu_logs(callback: CallbackQuery) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback)
        await _send_recent_logs(callback.message)

    async def _send_recent_logs(msg: Message) -> None:
        runs = await db.get_recent_scan_runs(limit=1)
        if not runs:
            await msg.answer("📭 No 5-minute scan runs logged in the database yet. Run /check first.", parse_mode="HTML")
            return

        r = runs[0]
        status_icon = "✅" if r["overall_status"] == "SUCCESS" else "❌"
        lines = [
            f"📜 <b>Latest 5-Minute Run Log (DB Run #{r['id']})</b>",
            f"🕒 <b>Time:</b> {html.escape(str(r['started_at']))}",
            f"{status_icon} <b>Overall Run Status:</b> <b>{html.escape(str(r['overall_status']))}</b>",
            f"📦 <b>Sets Scanned:</b> {r['sets_scanned']} | <b>Offers Saved:</b> {r['total_offers_found']} | <b>Failed Sites:</b> {r['failed_sites_count']}",
            "",
        ]
        for sl in r.get("site_logs", []):
            s_icon = "✅" if sl["status"] == "SUCCESS" else "❌"
            urls = json.loads(sl.get("urls_scraped_json") or "[]")
            first_url = urls[0] if urls else "N/A"
            extra_urls = f" (+{len(urls) - 1} more)" if len(urls) > 1 else ""
            err_note = f" — <i>{html.escape(str(sl['error_message']))}</i>" if sl.get("error_message") else ""
            lines.append(
                f"{s_icon} <b>[{html.escape(str(sl['set_number']))}] {html.escape(str(sl['source']))}</b>: "
                f"{sl['offers_found']} offers{err_note}\n"
                f"   ↳ <code>{html.escape(first_url)}</code>{extra_urls}"
            )

        # Fit safely within Telegram's 4096-char message limit without slicing HTML tags
        output_lines: list[str] = []
        total_len = 0
        for line in lines:
            if total_len + len(line) + 1 > 3900:
                output_lines.append("<i>... (truncated; full log in GitHub Actions & db-state/data/scraper_runs.log)</i>")
                break
            output_lines.append(line)
            total_len += len(line) + 1

        await msg.answer(
            "\n".join(output_lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=main_menu_keyboard(),
        )

    # -------------------------------------------------------------------------
    # /status Command & Callback
    # -------------------------------------------------------------------------
    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if not message.chat or not _is_authorized(message.chat.id):
            return
        await _send_status_report(message)

    @router.callback_query(F.data == "menu:status")
    async def cb_menu_status(callback: CallbackQuery) -> None:
        if not callback.message or not _is_authorized(callback.message.chat.id):
            return
        await safe_callback_answer(callback)
        await _send_status_report(callback.message)

    async def _send_status_report(msg: Message) -> None:
        await currency_converter.refresh_if_needed()
        health_rows = await db.get_scraper_health()

        lines = [
            "📡 <b>System, Anti-Block & FX Rate Status</b>",
            "",
            "💱 <b>Real-Time NBP Exchange Rates (to PLN):</b>",
            f"• 1 EUR = <b>{currency_converter.get_rate('EUR'):.4f} PLN</b>",
            f"• 1 USD = <b>{currency_converter.get_rate('USD'):.4f} PLN</b>",
            f"• 1 GBP = <b>{currency_converter.get_rate('GBP'):.4f} PLN</b>",
            f"• 1 CZK = <b>{currency_converter.get_rate('CZK'):.4f} PLN</b>",
            "",
            f"🛡 <b>Anti-Block Client:</b> curl_cffi TLS Impersonation | Requests made: <code>{http_client.total_requests_made}</code>",
            "",
            "🌐 <b>Marketplace Parsers Health:</b>",
        ]
        if not health_rows:
            lines.append("<i>No scan runs recorded yet. Run /check to test all sites.</i>")
        else:
            for r in health_rows:
                icon = "✅" if r["last_status"] == "OK" else "❌"
                lines.append(
                    f"{icon} <code>{html.escape(str(r['source']))}</code>: "
                    f"<b>{r['offers_count']} offers</b> ({r['last_run_at']})"
                )

        await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_menu_keyboard())

    return dp
