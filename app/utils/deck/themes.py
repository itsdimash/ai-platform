"""Палитры, шрифты и размеры презентаций — всё в одном модуле.

Фирменный шаблон Kerneu позже заменяет только PALETTES (и при желании FONT): движок и
layout'ы цвета не хардкодят. Контраст каждой пары «текст/фон» проверяется тестами (WCAG AA).
Правило: акцентные цвета на СВЕТЛЫХ слайдах — только заливки и линии (как текст они не
проходят AA); как текст акцент допустим на тёмном фоне (accent_on_dark).
"""

from dataclasses import dataclass

FONT = "Calibri"  # Calibri / Arial / Segoe UI — других шрифтов не используем

# Размеры (pt). Тело никогда не ниже BODY_FLOOR.
TITLE_MAX, TITLE_MIN = 40, 32
BODY_MAX, BODY_PREF_MIN, BODY_FLOOR = 22, 18, 14
BULLET_MAX_SHORT, BULLET_MAX_FIVE = (
    28,
    24,
)  # кегль списка: до 4 пунктов / 5 пунктов (ниже — по месту)
CARD_TITLE = 20
CAPTION = 12  # номер слайда, подписи

DEFAULT_THEME = "graphite"


def _rgb(hex6: str) -> tuple[int, int, int]:
    return int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)


def luminance(hex6: str) -> float:
    def f(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(hex6)
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(fg: str, bg: str) -> float:
    """Коэффициент контраста WCAG 2.x (1..21)."""
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: str, b: str, t: float) -> str:
    """Смесь двух цветов: t=0 -> a, t=1 -> b."""
    ra, rb = _rgb(a), _rgb(b)
    return "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ra, rb, strict=True))


def best_text_on(bg: str, light: str = "FFFFFF", dark: str = "1A1F24") -> str:
    """Белый или тёмный текст — тот, что контрастнее на заданной заливке."""
    return light if contrast(light, bg) >= contrast(dark, bg) else dark


@dataclass(frozen=True)
class Palette:
    name: str
    dark: str  # фон титульного/секционного/заключительного слайдов
    light: str  # фон контентных слайдов
    surface: str  # карточки и панели на светлом фоне
    primary: str  # заголовки на светлом, шапки таблиц, цифры
    accent1: str  # заливки и линии
    accent2: str
    accent_on_dark: str  # акцент, допустимый как ТЕКСТ на dark
    text: str = "1A1F24"  # основной текст на светлом
    muted: str = "4B5563"  # вторичный текст на светлом
    text_on_dark: str = "FFFFFF"
    muted_on_dark: str = "C9D5E3"

    @property
    def chart_colors(self) -> list[str]:
        """Цвета серий/секторов: различимы между собой и на светлом фоне."""
        return [
            self.primary,
            self.accent1,
            self.accent2,
            mix(self.primary, "FFFFFF", 0.45),
            mix(self.accent1, "000000", 0.25),
            mix(self.accent2, "000000", 0.25),
        ]


PALETTES: dict[str, Palette] = {
    "ocean": Palette("ocean", "0B2A43", "F7FAFC", "E6F0F7", "0E5A8A", "F2994A", "2BB3A3", "F2994A"),
    "forest": Palette(
        "forest", "12372A", "F6FAF7", "E4F0E8", "1F6B4A", "E0A526", "8BBF5A", "E0A526"
    ),
    "sunset": Palette(
        "sunset", "3A1C2E", "FFF9F5", "FCEBE0", "A63B28", "F28C28", "E4577A", "F28C28"
    ),
    "graphite": Palette(
        "graphite", "22262B", "F5F6F7", "E9ECEF", "3A4654", "2B6CD6", "F2A33A", "6FA8F5"
    ),
    # Заглушка до фирменного шаблона: спокойный тёмно-синий + один янтарный акцент.
    "kerneu": Palette(
        "kerneu", "0B2545", "F4F7FB", "E3EAF4", "13315C", "F2A900", "5C8DC7", "F2A900"
    ),
}


def get_palette(name: str | None) -> Palette:
    return PALETTES.get((name or "").lower(), PALETTES[DEFAULT_THEME])


def contrast_pairs(p: Palette) -> list[tuple[str, str, str, float]]:
    """(описание, цвет текста, цвет фона, минимальный контраст) — все пары, в которых движок
    реально рисует текст. Используется тестами и self-check."""
    on_acc1, on_acc2 = best_text_on(p.accent1), best_text_on(p.accent2)
    return [
        ("текст на светлом", p.text, p.light, 4.5),
        ("вторичный на светлом", p.muted, p.light, 4.5),
        ("текст на карточке", p.text, p.surface, 4.5),
        ("вторичный на карточке", p.muted, p.surface, 4.5),
        ("primary-текст на светлом", p.primary, p.light, 4.5),
        ("primary-текст на карточке", p.primary, p.surface, 4.5),
        ("текст на тёмном", p.text_on_dark, p.dark, 4.5),
        ("вторичный на тёмном", p.muted_on_dark, p.dark, 4.5),
        ("акцент-текст на тёмном", p.accent_on_dark, p.dark, 4.5),
        ("текст на primary (шапка таблицы)", best_text_on(p.primary), p.primary, 4.5),
        ("текст на accent1", on_acc1, p.accent1, 4.5),
        ("текст на accent2", on_acc2, p.accent2, 4.5),
        ("текст на тёмном-на-primary-панели", p.text_on_dark, p.primary, 4.5),
    ]
