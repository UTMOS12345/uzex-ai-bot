"""Product families: map the 229 seller-typed product names to one canonical family.

Matching is done on norm(product) (Uzbek-Latin transliteration), first match wins.
Each family carries the Uzbek words users are likely to type.
"""
import re

from textnorm import norm

# (family, uzbek aliases, regex on norm(product))
FAMILIES: list[tuple[str, str, str]] = [
    ("Хлопок-сырец", "paxta, paxta xomashyosi, chigitli paxta", r"^xlopok syrets"),
    ("Масло хлопковое", "paxta yog‘i, paxta moyi", r"xlopkovoe maslo|maslo xlopkovoe"),
    ("Шрот хлопковый", "paxta shroti, kunjara", r"^shrot xlopkov"),
    ("Пряжа хлопковая", "paxta ipi, ip, kalava", r"^pryaja"),
    ("Масло подсолнечное", "kungaboqar yog‘i", r"podsolnech"),
    ("Пшеница", "bug‘doy", r"^pshenitsa"),
    ("Мука", "un", r"^muka"),
    ("Рис", "guruch", r"^ris\b"),
    ("Сахар", "shakar", r"^saxar"),
    ("Соль пищевая", "tuz, osh tuzi", r"^sol\b"),
    ("Вода питьевая", "ichimlik suvi, suv", r"\bvoda\b"),
    ("Комбикорм", "omuxta yem, yem", r"^kombikorm"),
    ("Кормосмесь", "yem aralashmasi, ozuqa aralashmasi", r"^kormosmes"),
    ("Цемент", "sement, portlandsement", r"tsement"),
    ("Кирпич", "g‘isht", r"^kirpich"),
    ("Гипс", "gips", r"^gips"),
    ("Известь", "ohak", r"^izvest"),
    ("Стекло", "shisha, oyna, float shisha", r"^steklo"),
    ("Пиломатериалы", "yog‘och, taxta, yog‘och-taxta", r"^pilomaterial"),
    ("Арматура", "armatura", r"^armatura"),
    ("Проволока арматурная", "sim, armatura simi", r"^provoloka"),
    ("Лист стальной", "po‘lat list, temir list", r"^list stalnoy"),
    ("Катод медный", "mis, mis katod", r"katod"),
    ("Профиль алюминиевый", "alyuminiy, alyuminiy profil", r"alyuminiev"),
    ("Подшипник", "podshipnik", r"^podshipnik"),
    ("Кабель силовой", "kabel", r"^kabel"),
    ("Бензин", "benzin", r"^benzin"),
    ("Дизельное топливо", "dizel, dizel yoqilg‘isi, solyarka", r"dizeln"),
    ("Сжиженный газ", "suyultirilgan gaz, propan, gaz", r"sjijenn"),
    ("Мазут", "mazut", r"^mazut"),
    ("Битум", "bitum", r"^bitum"),
    ("Карбамид", "karbamid, mochevina, o‘g‘it", r"karbamid"),
    ("Аммиачная селитра", "selitra, ammiakli selitra, o‘g‘it", r"selitra"),
    ("Серная кислота", "sulfat kislota, oltingugurt kislotasi", r"sernaya|kislota"),
    ("Сода каустическая", "kaustik soda", r"^soda"),
    ("Азот", "azot", r"^azot"),
    ("Аргон", "argon", r"^argon"),
    ("Полиэтилен", "polietilen", r"^polietilen"),
    ("Полипропилен", "polipropilen", r"^polipropilen"),
    ("Поливинилхлорид", "PVX, polivinilxlorid", r"^polivinilxlorid"),
    ("Трубы полиэтиленовые", "quvur, truba, PE quvur", r"^trub"),
    ("Эмаль", "emal, bo‘yoq", r"^emal"),
]
_COMPILED = [(fam, re.compile(rx)) for fam, _, rx in FAMILIES]


def family(product: str | None) -> str | None:
    if not product:
        return None
    n = norm(product)
    for fam, rx in _COMPILED:
        if rx.search(n):
            return fam
    return product  # unknown: keep the original name so nothing is lost


ALIASES = {fam: uz for fam, uz, _ in FAMILIES}
