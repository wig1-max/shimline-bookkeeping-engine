"""Inline SVG icons, defined once and referenced by name.

The workspace used the full Tabler icon webfont: 462 KB and roughly 5,900
glyphs to draw the twenty-five icons we actually use. Over a long-haul link
that was the single largest thing on the page.

These are the same Tabler outlines (MIT licence), inlined as path data. The
whole set below is about 8 KB and needs no font, no second stylesheet, and no
`font-src` in the CSP.

Adding an icon: paste its path data into `PATHS` with a semantic name. That is
the only edit — every template reaches them through the same `icon()` helper,
so nothing else has to change.

Names are chosen for what they *mean* here, not what they depict, so swapping
the artwork later does not mean touching templates.
"""
from __future__ import annotations

from markupsafe import Markup

# Tabler Icons (https://tabler.io/icons), MIT licence. Every path is drawn on a
# 24x24 grid with round caps and joins, stroke set by currentColor.
PATHS: dict[str, str] = {
    # navigation
    "today": "M12 3v1M5.6 5.6l.7 .7M3 12h1M20 12h1M18.4 5.6l-.7 .7M12 20v1M7.5 16.5a5.5 5.5 0 1 1 9 0",
    "pipeline": "M12 3l8 4.5v9L12 21l-8 -4.5v-9L12 3zM12 12l8 -4.5M12 12v9M12 12L4 7.5",
    "clients": "M9 7a4 4 0 1 0 8 0a4 4 0 0 0 -8 0M3 21v-2a4 4 0 0 1 4 -4h8a4 4 0 0 1 4 4v2",
    "work": "M3 7m0 2a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v9a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2zM8 7v-2a2 2 0 0 1 2 -2h4a2 2 0 0 1 2 2v2M12 12v.01M3 13a20 20 0 0 0 18 0",
    "calendar": "M4 7a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v12a2 2 0 0 1 -2 2h-12a2 2 0 0 1 -2 -2zM16 3v4M8 3v4M4 11h16M11 15h1M12 15v3",
    "history": "M12 8l0 4l2 2M3.05 11a9 9 0 1 1 .5 4m-.5 5v-5h5",
    "security": "M5 13a2 2 0 0 1 2 -2h10a2 2 0 0 1 2 2v6a2 2 0 0 1 -2 2h-10a2 2 0 0 1 -2 -2zM11 16a1 1 0 1 0 2 0a1 1 0 0 0 -2 0M8 11v-4a4 4 0 1 1 8 0v4",
    "sign-out": "M14 8v-2a2 2 0 0 0 -2 -2h-7a2 2 0 0 0 -2 2v12a2 2 0 0 0 2 2h7a2 2 0 0 0 2 -2v-2M9 12h12l-3 -3M18 15l3 -3",
    "menu": "M4 6h16M4 12h16M4 18h16",
    # movement
    "arrow-right": "M5 12h14M13 18l6 -6M13 6l6 6",
    "arrow-left": "M5 12h14M5 12l6 6M5 12l6 -6",
    "chevron-right": "M9 6l6 6l-6 6",
    "chevron-left": "M15 6l-6 6l6 6",
    # state
    "check": "M5 12l5 5l10 -10",
    "circle": "M3 12a9 9 0 1 0 18 0a9 9 0 0 0 -18 0",
    "circle-check": "M3 12a9 9 0 1 0 18 0a9 9 0 0 0 -18 0M9 12l2 2l4 -4",
    "warning": "M12 9v4M10.363 3.591l-8.106 13.534a1.914 1.914 0 0 0 1.636 2.871h16.214a1.914 1.914 0 0 0 1.636 -2.871l-8.106 -13.534a1.914 1.914 0 0 0 -3.274 0zM12 16h.01",
    "shield-check": "M11.46 20.846a12 12 0 0 1 -7.96 -14.846a12 12 0 0 0 8.5 -3a12 12 0 0 0 8.5 3a12 12 0 0 1 -.09 1.71M15 19l2 2l4 -4",
    "shield-lock": "M12 3a12 12 0 0 0 8.5 3a12 12 0 0 1 -8.5 15a12 12 0 0 1 -8.5 -15a12 12 0 0 0 8.5 -3M12 11m-1 0a1 1 0 1 0 2 0a1 1 0 1 0 -2 0M12 12v2.5",
    # objects
    "search": "M10 10m-7 0a7 7 0 1 0 14 0a7 7 0 1 0 -14 0M21 21l-6 -6",
    "document": "M14 3v4a1 1 0 0 0 1 1h4M17 21h-10a2 2 0 0 1 -2 -2v-14a2 2 0 0 1 2 -2h7l5 5v11a2 2 0 0 1 -2 2zM8 11h8M8 15h8",
    "document-alert": "M14 3v4a1 1 0 0 0 1 1h4M17 21h-10a2 2 0 0 1 -2 -2v-14a2 2 0 0 1 2 -2h7l5 5v11a2 2 0 0 1 -2 2zM12 11v3M12 17v.01",
    "import": "M14 3v4a1 1 0 0 0 1 1h4M12 17v-6M9.5 13.5l2.5 -2.5l2.5 2.5M17 21h-10a2 2 0 0 1 -2 -2v-14a2 2 0 0 1 2 -2h7l5 5v11a2 2 0 0 1 -2 2z",
    "download": "M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2 -2v-2M7 11l5 5l5 -5M12 4v12",
    "note": "M13 20l7 -7M13 20v-6a1 1 0 0 1 1 -1h6v-7a2 2 0 0 0 -2 -2h-12a2 2 0 0 0 -2 2v12a2 2 0 0 0 2 2z",
    "activity": "M3 12h4l3 8l4 -16l3 8h4",
}

# Names kept working after a rename, so a template is never wrong for being old.
ALIASES: dict[str, str] = {
    "sun": "today",
    "stack-2": "pipeline",
    "users": "clients",
    "briefcase": "work",
    "logout": "sign-out",
    "menu-2": "menu",
    "alert-triangle": "warning",
    "file-spreadsheet": "document",
    "file-alert": "document-alert",
    "file-import": "import",
    "circle-check-filled": "circle-check",
}


def resolve(name: str) -> str:
    return ALIASES.get(name, name)


def sprite() -> Markup:
    """One hidden <svg> holding every icon, emitted once per page.

    Each icon is then a nine-byte <use> reference, so using the same icon
    thirty times costs thirty references rather than thirty copies.
    """
    symbols = "".join(
        f'<symbol id="i-{name}" viewBox="0 0 24 24"><path d="{path}"/></symbol>'
        for name, path in PATHS.items()
    )
    return Markup(
        '<svg xmlns="http://www.w3.org/2000/svg" class="icon-sprite" aria-hidden="true" '
        'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round">{symbols}</svg>'
    )


def icon(name: str, extra_class: str = "") -> Markup:
    """Render one icon. Decorative by default — meaning belongs in the text."""
    resolved = resolve(name)
    if resolved not in PATHS:
        # A missing icon should be obvious in review, not a silent blank.
        resolved = "warning"
    classes = f"icon {extra_class}".strip()
    return Markup(
        f'<svg class="{classes}" aria-hidden="true" focusable="false">'
        f'<use href="#i-{resolved}"/></svg>'
    )


def register(templates) -> None:
    """Expose the helpers to a Jinja environment."""
    templates.env.globals["icon"] = icon
    templates.env.globals["icon_sprite"] = sprite
