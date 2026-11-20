from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.models import Subscription


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Add LEGO Set", callback_data="menu:add"),
                InlineKeyboardButton(text="📋 My Subscribed Sets", callback_data="menu:list"),
            ],
            [
                InlineKeyboardButton(text="🔄 Check Prices Now", callback_data="menu:check_all"),
                InlineKeyboardButton(text="⏳ Retirement Dates", callback_data="menu:retire"),
            ],
            [
                InlineKeyboardButton(text="📡 Scraper & FX Status", callback_data="menu:status"),
                InlineKeyboardButton(text="📜 Run Logs (DB)", callback_data="menu:logs"),
            ],
        ]
    )


def subscriptions_list_keyboard(subs: Sequence[Subscription]) -> InlineKeyboardMarkup:
    """
    One clickable button per subscribed set (opens detailed set view),
    followed by all main menu buttons below.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for sub in subs:
        label = f"🧱 LEGO {sub.set_number} — {sub.set_name or 'LEGO Set'}"
        if len(label) > 60:
            label = label[:59].rstrip() + "…"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"set:view:{sub.set_number}")]
        )
    rows.extend(main_menu_keyboard().inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def set_actions_keyboard(set_number: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"🔍 Check {set_number}", callback_data=f"set:check:{set_number}"),
                InlineKeyboardButton(text=f"📈 Chart {set_number}", callback_data=f"set:chart:{set_number}"),
                InlineKeyboardButton(text="🛒 Top Deals", callback_data=f"set:deals:{set_number}"),
            ],
            [
                InlineKeyboardButton(text="✏️ Edit Price Range", callback_data=f"set:edit:{set_number}"),
                InlineKeyboardButton(text=f"🗑 Remove {set_number}", callback_data=f"set:remove:{set_number}"),
            ],
            [
                InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:main"),
            ],
        ]
    )
