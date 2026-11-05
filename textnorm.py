"""Text normalization: Cyrillic (Russian + Uzbek) -> Uzbek Latin, lowercase, alnum tokens only."""
import re

_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h",
}
# all apostrophe-like characters are dropped, not turned into spaces (o‘ -> o, g‘ -> g)
_APOS = "'‘’ʻʼ`´ʹ′"
_TABLE = str.maketrans({**_MAP, **{c: "" for c in _APOS}})
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def norm(text: str | None) -> str:
    if not text:
        return ""
    s = text.lower().translate(_TABLE)
    return _NON_ALNUM.sub(" ", s).strip()


if __name__ == "__main__":
    for t in ['"ҲАРАЙЎМ КИМЁ" МЧЖ', "Go‘qe", "Хлопок-сырец", "Farg‘ona viloyati"]:
        print(repr(t), "->", repr(norm(t)))
