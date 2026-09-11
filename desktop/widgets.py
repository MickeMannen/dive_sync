import toga
from toga.style import Pack
from toga.style.pack import COLUMN

from desktop.theme import THEME


def card(title, *widgets):
    box = toga.Box(style=Pack(direction=COLUMN, margin=(0, 16, 24, 16)))
    box.add(
        toga.Label(
            title,
            style=Pack(margin_bottom=8, font_weight="bold", color=THEME["nav_active_bg"]),
        )
    )
    for widget in widgets:
        box.add(widget)
    return box
