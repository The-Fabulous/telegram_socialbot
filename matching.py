import math
import re
import time

CATALOG = {
    "anime": {"Жанры": {"shounen": "Сёнэн", "seinen": "Сэйнэн", "romance": "Романтика", "isekai": "Исекай",
                        "slice": "Повседневность", "mecha": "Меха", "horror": "Хоррор"}},
    "football": {"Лиги": {"epl": "АПЛ", "laliga": "Ла Лига", "seriea": "Серия A", "bundes": "Бундеслига",
                          "ucl": "Лига чемпионов", "kpl": "Премьер-лига КЗ"}},
    "games": {"Жанры": {"shooter": "Шутеры", "moba": "MOBA", "rpg": "RPG", "sandbox": "Песочницы",
                        "strategy": "Стратегии", "indie": "Инди"},
              "Платформа": {"pc": "PC", "console": "Консоль", "mobile": "Телефон"}},
    "music": {"Жанры": {"rap": "Рэп", "pop": "Поп", "rock": "Рок", "metal": "Метал", "electro": "Электроника",
                        "classic": "Классика", "kpop": "K-pop", "phonk": "Фонк"}},
    "movies": {"Жанры": {"action": "Экшен", "comedy": "Комедия", "drama": "Драма", "horror": "Хоррор",
                         "scifi": "Фантастика", "thriller": "Триллер", "animation": "Мультфильмы"}},
    "series": {"Жанры": {"drama": "Драма", "comedy": "Комедия", "thriller": "Триллер", "fantasy": "Фэнтези",
                         "crime": "Криминал", "scifi": "Фантастика", "doc": "Документальные"}},
    "tech": {"Направления": {"code": "Программирование", "ai": "ИИ", "web": "Веб", "gadgets": "Гаджеты",
                             "security": "Кибербезопасность", "gamedev": "Геймдев"},
             "Уровень": {"lvl1": "Новичок", "lvl2": "Средний", "lvl3": "Профи"}},
}

# вес подтем и слов + вес каждой группы подтем (у каждой темы своя формула)
FORMULA = {
    "anime":    {"sub": .40, "tag": .60, "groups": {"Жанры": 1}},
    "football": {"sub": .40, "tag": .60, "groups": {"Лиги": 1}},        # клубы пишут словами -> «слова»
    "games":    {"sub": .50, "tag": .50, "groups": {"Жанры": 1, "Платформа": 1.5}},
    "music":    {"sub": .55, "tag": .45, "groups": {"Жанры": 1}},
    "movies":   {"sub": .50, "tag": .50, "groups": {"Жанры": 1}},
    "series":   {"sub": .45, "tag": .55, "groups": {"Жанры": 1}},
    "tech":     {"sub": .60, "tag": .40, "groups": {"Направления": 1, "Уровень": .8}},
}

# «соседние» подтемы дают половину зачёта
NEAR = {frozenset(p) for p in [
    ("shounen", "mecha"), ("isekai", "slice"), ("romance", "slice"), ("seinen", "horror"), ("rap", "phonk"),
    ("pop", "kpop"), ("electro", "phonk"), ("rock", "metal"), ("thriller", "horror"), ("scifi", "action"),
    ("drama", "romance"), ("crime", "thriller"), ("ai", "code"), ("web", "code"), ("security", "code"),
    ("gamedev", "code"), ("shooter", "moba"), ("rpg", "strategy")]}

STOP = set("и в на с по для что это как а но или the a an of to is are мне мой люблю нравится очень тоже".split())


def toks(text: str) -> set:
    return {w[:5] for w in re.findall(r"[a-zа-яёәіңғүұқөһ0-9]{3,}", (text or "").lower()) if w not in STOP}


def tri(text: str) -> set:
    s = re.sub(r"\s+", " ", (text or "").lower())
    return {s[i:i + 3] for i in range(len(s) - 2)}


def jac(a: set, b: set) -> float:
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def cos(a: set, b: set, idf: dict) -> float:
    common = a & b
    if not common:
        return 0.0
    num = sum(idf.get(t, 1) ** 2 for t in common)
    den = math.sqrt(sum(idf.get(t, 1) ** 2 for t in a) * sum(idf.get(t, 1) ** 2 for t in b))
    return num / den if den else 0.0


def group_sim(a: set, b: set, group: str) -> float:
    if group == "Уровень":
        return 1 - abs(int(next(iter(a))[-1]) - int(next(iter(b))[-1])) / 2
    near = 0.5 * sum(1 for x in a - b for y in b - a if frozenset((x, y)) in NEAR)
    return min(1.0, (len(a & b) + near) / len(a | b))


def score(p: dict, q: dict, idf: dict) -> float:
    """Сходство двух профилей {"topic","subs","interests","t"} от 0 до 1."""
    ta, tb = toks(p.get("interests", "")), toks(q.get("interests", ""))
    w = min(1.0, min(len(ta), len(tb)) / 4)          # слов мало -> больше веса у триграмм
    s_tag = w * cos(ta, tb, idf) + (1 - w) * jac(tri(p.get("interests", "")), tri(q.get("interests", "")))

    f, groups = FORMULA.get(p.get("topic")), CATALOG.get(p.get("topic"), {})
    if f and p.get("subs") and q.get("subs"):
        num = den = 0.0
        for g, ids in groups.items():
            a, b = {x for x in p["subs"] if x in ids}, {x for x in q["subs"] if x in ids}
            if a and b:
                wg = f["groups"].get(g, 1)
                num += wg * group_sim(a, b, g)
                den += wg
        if den:
            return f["sub"] * num / den + f["tag"] * s_tag
    return s_tag                                       # темы без каталога и профили без подтем


def rank(target, cands: dict) -> list:
    """Кандидаты с той же темой, от лучшего к худшему: [(id, score)]."""
    me = cands[target]
    pool = {k: v for k, v in cands.items() if k == target or v.get("topic") == me.get("topic")}
    docs = [toks(v.get("interests", "")) for v in pool.values()]
    df = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((len(docs) + 1) / (c + 1)) + 1 for t, c in df.items()}
    return sorted(((k, score(me, v, idf)) for k, v in pool.items() if k != target), key=lambda x: -x[1])


def threshold(me: dict, other: dict, pool_size: int, now: float = None) -> float:
    """Требуемое сходство падает со временем ожидания (через ~3 мин подойдёт любой с той же темой)."""
    t = min(me.get("t") or time.time(), other.get("t") or time.time())
    waited = ((now or time.time()) - t) / 60
    return max(0.0, 0.22 + (0.06 if pool_size >= 8 else 0) - 0.08 * waited)