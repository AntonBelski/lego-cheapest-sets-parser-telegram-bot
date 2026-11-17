from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


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
