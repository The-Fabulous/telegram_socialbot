import asyncio
import os
import re
import logging
from typing import Any, Dict, List, Optional, TypedDict

from dotenv import load_dotenv
from openai import BadRequestError
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from matching import rank, threshold

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEBUG_PROMPTS = os.getenv("DEBUG_PROMPTS", "").lower() in ("1", "true", "yes")

# ==========================================
# 1. ПРОМПТЫ
# ==========================================

HUMAN_SYSTEM_PROMPT = (
    "Ты — приятный, свойский и живой собеседник. Общаешься легко, с юмором и без занудства.\n\n"
    "Правила стиля:\n"
    "1. Общайся наравне, подстраивайся под вайб человека (можно использовать разговорные слова, сленг, легкую иронию).\n"
    "2. Не будь занудным допросчиком: не нужно после каждой фразы выдавать стандартные вопросы из анкеты.\n"
    "3. Отвечай коротко (1-2 предложения), как пишут в мессенджерах.\n"
    "4. Не спамь эмодзи (никаких картиночных эмодзи; максимум — обычная текстовая скобка ')' если к месту)."
)

WELCOME_SYSTEM_PROMPT = (
    "Ты живой и свойский собеседник. Напиши одно короткое легкое приветствие в телеграм-стиле. "
    "Без картиночных эмодзи."
)

# ==========================================
# 1.1 ЯЗЫКИ
# ==========================================

LANGS = {
    "auto": "Авто (язык собеседника)",
    "ru": "Русский",
    "en": "English",
    "kk": "Қазақша",
}

LANG_RULES = {
    "auto": "Отвечай на том языке, на котором пишет собеседник. Если язык непонятен — отвечай по-русски.",
    "ru": "Отвечай только на русском языке.",
    "en": "Отвечай только на английском языке (English), даже если собеседник пишет на другом.",
    "kk": "Отвечай только на казахском языке (қазақ тілі), даже если собеседник пишет на другом.",
}

I18N = {
    "opener_fallback": {
        "ru": "Отлично, давай про {name}. Что тебе тут больше всего нравится?",
        "en": "Nice, let's talk about {name}. What do you like most about it?",
        "kk": "Жақсы, {name} туралы сөйлесейік. Саған мұнда не көбірек ұнайды?",
    },
    "clarify": {
        "ru": "Не совсем понял тему. Выбери кнопкой ниже или напиши, что тебе интересно ({names}).",
        "en": "Didn't quite get the topic. Pick a button below or tell me what you're into ({names}).",
        "kk": "Тақырыпты анық түсінбедім. Төмендегі батырманы таңда немесе не қызықтыратыныңды жаз ({names}).",
    },
    "topic_ok": {
        "ru": "Отлично, давай про это. Что тебе тут больше всего нравится?",
        "en": "Great, let's talk about it. What do you like most about it?",
        "kk": "Керемет, осы туралы сөйлесейік. Саған не көбірек ұнайды?",
    },
    "topic_fail": {
        "ru": "Не совсем понял тему. Выбери кнопкой ниже.",
        "en": "Didn't quite get the topic. Pick a button below.",
        "kk": "Тақырыпты түсінбедім. Төмендегі батырманы таңда.",
    },
}


def lang_rule(lang: Optional[str]) -> str:
    return LANG_RULES.get(lang or "auto", LANG_RULES["auto"])


def tr(key: str, lang: Optional[str], **kw) -> str:
    table = I18N[key]
    return table.get(lang or "ru", table["ru"]).format(**kw)


# ==========================================
# 1.2 ТЕМЫ ДЛЯ МЭТЧИНГА
# ==========================================

TOPICS = {
    "anime": "🎌 Аниме", "football": "⚽ Футбол", "games": "🎮 Игры", "music": "🎵 Музыка",
    "movies": "🎬 Кино", "series": "📺 Сериалы", "tech": "💻 Технологии", "books": "📚 Книги",
    "sports": "🏀 Другие виды спорта", "fitness": "🏋️ Фитнес", "travel": "✈️ Путешествия",
    "food": "🍳 Еда и кулинария", "art": "🎨 Творчество и дизайн", "cars": "🚗 Авто и мото",
    "science": "🔬 Наука и космос", "memes": "😂 Мемы и юмор", "languages": "🌍 Языки", "other": "✨ Другое",
}

# быстрый поиск темы по ключевым словам (до обращения к модели)
TOPIC_KEYWORDS = {
    "anime": ["аниме", "anime", "манга", "manga", "наруто", "ван пис", "one piece"],
    "football": ["футбол", "football", "лига чемпионов", "месси", "роналду", "messi", "ronaldo"],
    "games": ["игр", "game", "гейм", "dota", "дота", "minecraft", "майнкрафт", "cs2", "кс2", "playstation"],
    "music": ["музык", "песн", "трек", "рэп", "music", "концерт"],
    "movies": ["фильм", "кино"],
    "series": ["сериал", "сезон", "netflix", "нетфликс"],
    "tech": ["программ", "python", "технолог", "гаджет", "нейросет", "айти"],
    "books": ["книг", "роман", "читаю"],
    "sports": ["баскетбол", "теннис", "хоккей", "бокс", "mma", "ufc"],
    "travel": ["путешеств", "поездк", "отпуск"],
    "cars": ["машин", "автомоб", "мотоцикл"],
}


def topic_name(key: str) -> str:
    """'🎌 Аниме' -> 'Аниме' (без эмодзи, для промптов)."""
    label = TOPICS.get(key, key)
    return label.split(" ", 1)[1] if " " in label else label


def _match_topic_keywords(text: str) -> Optional[str]:
    low = (text or "").lower()
    for key, words in TOPIC_KEYWORDS.items():
        if any(w in low for w in words):
            return key
    return None


# ==========================================
# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def _text(message: Any) -> str:
    """Достаёт текст из ответа модели (content бывает строкой или списком блоков)."""
    content = getattr(message, "content", message)
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "").strip()


def _clean(text: str) -> str:
    """Убирает markdown, который Gemma любит добавлять."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[\*\-•]\s+", "", text)
    return text.replace("*", "").replace("`", "").strip()


def _flatten(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Запасной формат для серверов, не принимающих system-роль:
    инструкция вшивается в первое сообщение пользователя, роли чередуются, первым идёт user."""
    sys_text = "\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
    rest = [m for m in messages if not isinstance(m, SystemMessage)]
    if not rest:
        return [HumanMessage(content=sys_text)]

    if isinstance(rest[0], AIMessage):
        rest.insert(0, HumanMessage(content=sys_text or "Привет"))
        sys_text = ""

    merged: List[BaseMessage] = []
    for m in rest:
        kind = AIMessage if isinstance(m, AIMessage) else HumanMessage
        if merged and type(merged[-1]) is kind:
            merged[-1] = kind(content=f"{merged[-1].content}\n{m.content}")
        else:
            merged.append(kind(content=str(m.content)))

    if sys_text:
        merged[0] = HumanMessage(content=f"{sys_text}\n\n{merged[0].content}")
    return merged


# ==========================================
# 3. СОСТОЯНИЯ ГРАФОВ
# ==========================================

class ChatState(TypedDict, total=False):
    messages: List[BaseMessage]
    reply: str
    lang: str
    user_id: Optional[int]
    chat_id: Optional[int]


class TopicState(TypedDict, total=False):
    user_text: str
    topic: Optional[str]
    reply: str
    lang: str
    user_id: Optional[int]
    chat_id: Optional[int]


class MatchState(TypedDict, total=False):
    target: int
    candidates: Dict[int, Dict[str, Any]]   # chat_id -> {"topic", "interests", "subs", "t"}
    ranked: list                            # [(chat_id, score)] выше порога
    best: Optional[int]


# ==========================================
# 4. КЛАСС-АДАПТЕР
# ==========================================

class SocialBridgeAI:
    def __init__(self):
        api_key = (os.getenv("OPENAI_API_KEY") or os.getenv("ALEM_API_KEY") or "").strip()
        base_url = (os.getenv("OPENAI_BASE_URL") or "https://llm.alem.ai/v1").strip()
        self.model_name = (os.getenv("OPENAI_MODEL") or "gemma4").strip()

        self.llm = ChatOpenAI(
            model=self.model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=0.6,
            timeout=20,
            max_retries=1,
            max_tokens=300,
        )
        self._flat_mode = False  # включится сам, если сервер ответит 400 на system-роль
        self._sem = asyncio.Semaphore(int(os.getenv("LLM_CONCURRENCY", "8")))  # макс. одновременных запросов к модели

        self.chat_app = self._build_chat_graph()
        self.topic_app = self._build_topic_graph()
        self.match_app = self._build_match_graph()

    # ---------- вызов модели ----------

    async def _invoke(self, messages: List[BaseMessage]) -> str:
        """Единая точка вызова модели: очередь через семафор + жёсткий таймаут на весь вызов."""
        async with self._sem:
            return await asyncio.wait_for(self._call(messages), timeout=30)

    async def _call(self, messages: List[BaseMessage]) -> str:
        """На 400 (system-роль / чередование ролей) переходит на запасной формат."""
        if not self._flat_mode:
            try:
                return _text(await self.llm.ainvoke(messages))
            except BadRequestError as e:
                logger.warning(f"400 от модели, переключаюсь на запасной формат сообщений: {e}")
                self._flat_mode = True
        return _text(await self.llm.ainvoke(_flatten(messages)))

    # ---------- граф ответа ----------
    #
    #   START -> set_language -> respond -> finalize -> END

    def _build_chat_graph(self):
        async def set_language(state: ChatState) -> Dict[str, Any]:
            """Дописывает в системный промпт правило языка, выбранного пользователем."""
            rule = lang_rule(state.get("lang"))
            msgs = list(state["messages"])
            for i, m in enumerate(msgs):
                if isinstance(m, SystemMessage):
                    msgs[i] = SystemMessage(content=f"{m.content}\n\n{rule}")
                    break
            else:
                msgs.insert(0, SystemMessage(content=rule))
            return {"messages": msgs}

        async def respond(state: ChatState) -> Dict[str, Any]:
            msgs = state["messages"]
            if DEBUG_PROMPTS:
                print(f"\n🚀 [PROMPT] Chat ID: {state.get('chat_id')} | User ID: {state.get('user_id')} | lang={state.get('lang')}")
                for i, m in enumerate(msgs, 1):
                    print(f" [{i}] {type(m).__name__}: {m.content}")
            return {"reply": await self._invoke(msgs)}

        async def finalize(state: ChatState) -> Dict[str, Any]:
            return {"reply": _clean(state.get("reply", ""))}

        graph = StateGraph(ChatState)
        graph.add_node("set_language", set_language)
        graph.add_node("respond", respond)
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "set_language")
        graph.add_edge("set_language", "respond")
        graph.add_edge("respond", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    # ---------- публичные методы ----------

    async def generate_welcome(self, user_name: str, user_id: int = None, chat_id: int = None,
                               lang: str = "auto") -> str:
        """Генерирует естественное короткое приветствие."""
        try:
            print(f"👋 [WELCOME] Chat ID: {chat_id} | User ID: {user_id} | Name: {user_name}")
            result = await self.chat_app.ainvoke({
                "messages": [
                    SystemMessage(content=WELCOME_SYSTEM_PROMPT),
                    HumanMessage(content=f"Поприветствуй пользователя по имени '{user_name}'."),
                ],
                "lang": lang,
                "user_id": user_id,
                "chat_id": chat_id,
            })
            return result.get("reply") or f"Привет, {user_name}! Как сам?"
        except Exception as e:
            print(f"❌ Ошибка generate_welcome (Chat ID: {chat_id}, User ID: {user_id}): {e!r}")
            return f"Привет, {user_name}! Как сам?"

    async def get_comfort_response(self, history: list, user_text: str, user_id: int = None,
                                   chat_id: int = None, lang: str = "auto") -> Optional[str]:
        """Ответ в режиме общения с ИИ. При сбое возвращает None (без заглушки)."""
        try:
            messages: List[BaseMessage] = [SystemMessage(content=HUMAN_SYSTEM_PROMPT)]
            for m in history:
                role, content = m.get("role"), m.get("content", "")
                if role == "user":
                    messages.append(HumanMessage(content=content))
                elif role == "assistant":
                    messages.append(AIMessage(content=content))
            messages.append(HumanMessage(content=user_text))

            result = await self.chat_app.ainvoke({
                "messages": messages, "lang": lang, "user_id": user_id, "chat_id": chat_id,
            })
            reply = result.get("reply")
            if DEBUG_PROMPTS:
                print(f"📥 [RESPONSE] Chat ID: {chat_id} | User ID: {user_id}:\n{reply}\n")
            return reply or None
        except Exception as e:
            print(f"❌ Ошибка get_comfort_response (Chat ID: {chat_id}, User ID: {user_id}): {e!r}")
            return None

    # ---------- граф выбора темы ----------
    #
    #   START -> keyword_match --(тема найдена)--> opener -> END
    #                 |
    #                 +--(нет)--> llm_classify --(тема найдена)--> opener -> END
    #                                  |
    #                                  +--(нет)--> clarify -> END

    def _build_topic_graph(self):
        async def keyword_match(state: TopicState) -> Dict[str, Any]:
            if state.get("topic") in TOPICS:  # тема уже выбрана кнопкой
                return {}
            return {"topic": _match_topic_keywords(state.get("user_text", ""))}

        def after_keywords(state: TopicState) -> str:
            return "opener" if state.get("topic") in TOPICS else "llm_classify"

        async def llm_classify(state: TopicState) -> Dict[str, Any]:
            text = state.get("user_text", "")
            keys = ", ".join(TOPICS)
            prompt = (
                "Определи, к какой теме разговора относится сообщение пользователя.\n"
                f"Возможные темы (ключи): {keys}.\n"
                "Ключ other — если человек называет конкретное увлечение, которого нет в списке "
                "(рыбалка, коллекционирование и т.п.).\n"
                "Если сообщение вообще не про тему (приветствие, болтовня без темы) — ответь NONE.\n\n"
                f'Сообщение: """{text}"""\n\n'
                "Ответь строго одним словом: ключ темы или NONE."
            )
            try:
                res = (await self._invoke([HumanMessage(content=prompt)])).lower()
                for key in TOPICS:
                    if re.search(rf"\b{key}\b", res):
                        return {"topic": key}
            except Exception as e:
                print(f"❌ Ошибка llm_classify: {e!r}")
            return {"topic": None}

        def after_llm(state: TopicState) -> str:
            return "opener" if state.get("topic") in TOPICS else "clarify"

        async def opener(state: TopicState) -> Dict[str, Any]:
            lang = state.get("lang")
            name = topic_name(state["topic"])
            user_text = (state.get("user_text") or "").strip()
            extra = f" Человек написал: «{user_text}»." if user_text else ""
            msgs = [
                SystemMessage(content=f"{HUMAN_SYSTEM_PROMPT}\n\n{lang_rule(lang)}"),
                HumanMessage(
                    content=f"Человек выбрал тему для разговора: {name}.{extra} "
                            "Начни разговор по этой теме: одна короткая реплика и один лёгкий вопрос."
                ),
            ]
            try:
                reply = _clean(await self._invoke(msgs))
            except Exception as e:
                print(f"❌ Ошибка opener: {e!r}")
                reply = ""
            return {"reply": reply or tr("opener_fallback", lang, name=name.lower())}

        async def clarify(state: TopicState) -> Dict[str, Any]:
            names = ", ".join(topic_name(k) for k in TOPICS if k != "other")
            return {"topic": None, "reply": tr("clarify", state.get("lang"), names=names)}

        graph = StateGraph(TopicState)
        graph.add_node("keyword_match", keyword_match)
        graph.add_node("llm_classify", llm_classify)
        graph.add_node("opener", opener)
        graph.add_node("clarify", clarify)
        graph.add_edge(START, "keyword_match")
        graph.add_conditional_edges("keyword_match", after_keywords,
                                    {"opener": "opener", "llm_classify": "llm_classify"})
        graph.add_conditional_edges("llm_classify", after_llm,
                                    {"opener": "opener", "clarify": "clarify"})
        graph.add_edge("opener", END)
        graph.add_edge("clarify", END)
        return graph.compile()

    # ---------- граф мэтчинга ----------
    #
    #   START -> score --(никого)--> END
    #              |--(явный лидер)--> top -> END
    #              +--(несколько близких)--> tie (модель решает) -> END

    def _build_match_graph(self):
        async def score(state: MatchState) -> Dict[str, Any]:
            cands, target = state["candidates"], state["target"]
            me = cands[target]
            ranked = [(c, s) for c, s in rank(target, cands) if s >= threshold(me, cands[c], len(cands))]
            print(f"📊 [SCORE] {target}: {[(c, round(s, 2)) for c, s in ranked[:4]]}")
            return {"ranked": ranked, "best": None}

        def route(state: MatchState) -> str:
            r = state.get("ranked") or []
            if not r:
                return "none"
            return "top" if len(r) == 1 or r[0][1] - r[1][1] > 0.06 else "tie"

        async def top(state: MatchState) -> Dict[str, Any]:
            return {"best": state["ranked"][0][0]}

        async def tie(state: MatchState) -> Dict[str, Any]:
            r, cands = state["ranked"], state["candidates"]
            near = [c for c, s in r if r[0][1] - s <= 0.06][:4]
            me = cands[state["target"]]
            lines = "\n".join(f"ID {c}: {cands[c].get('interests', '')}" for c in near)
            prompt = (
                f"Тема: {topic_name(me.get('topic', ''))}.\nИнтересы пользователя: \"{me.get('interests', '')}\"\n\n"
                f"Кандидаты:\n{lines}\n\nВыбери ID одного, чьи интересы ближе всего. Ответь ТОЛЬКО числом."
            )
            try:
                res = await self._invoke([HumanMessage(content=prompt)])
                for m in re.findall(r"\d+", res):
                    if int(m) in near:
                        return {"best": int(m)}
            except Exception as e:
                print(f"❌ Ошибка tie: {e!r}")
            return {"best": near[0]}

        graph = StateGraph(MatchState)
        graph.add_node("score", score)
        graph.add_node("top", top)
        graph.add_node("tie", tie)
        graph.add_edge(START, "score")
        graph.add_conditional_edges("score", route, {"none": END, "top": "top", "tie": "tie"})
        graph.add_edge("top", END)
        graph.add_edge("tie", END)
        return graph.compile()

    # ---------- публичные методы выбора темы и мэтчинга ----------

    async def start_topic(self, topic_key: Optional[str] = None, text: str = "",
                          user_id: int = None, chat_id: int = None, lang: str = "auto") -> Dict[str, Any]:
        """Определяет тему (по кнопке или по тексту) и возвращает {"topic": key|None, "reply": str}."""
        try:
            print(f"🧭 [TOPIC] Chat ID: {chat_id} | User ID: {user_id} | lang={lang} | button={topic_key} | text={text[:40]!r}")
            res = await self.topic_app.ainvoke({
                "topic": topic_key, "user_text": text, "lang": lang, "user_id": user_id, "chat_id": chat_id,
            })
            return {"topic": res.get("topic"), "reply": res.get("reply", "")}
        except Exception as e:
            print(f"❌ Ошибка start_topic (Chat ID: {chat_id}, User ID: {user_id}): {e!r}")
            if topic_key in TOPICS:
                return {"topic": topic_key, "reply": tr("topic_ok", lang)}
            return {"topic": None, "reply": tr("topic_fail", lang)}

    async def extract_interests(self, user_messages: list, topic: Optional[str] = None,
                                user_id: int = None, chat_id: int = None) -> str:
        """Ключевые слова интересов через запятую (для формул подбора)."""
        text = " ".join(m.get("content", "") for m in user_messages if m.get("role") == "user")
        if not text.strip():
            return ""
        topic_part = f"Тема разговора: {topic_name(topic)}.\n" if topic in TOPICS else ""
        prompt = (
            f"{topic_part}Из текста пользователя выпиши 3-8 коротких ключевых слов или названий "
            "(тайтлы, команды, игры, исполнители, технологии, хобби) через запятую. "
            f"Только слова, без пояснений и нумерации.\n\n\"{text}\""
        )
        try:
            return await self._invoke([HumanMessage(content=prompt)])
        except Exception as e:
            print(f"❌ Ошибка extract_interests (Chat ID: {chat_id}, User ID: {user_id}): {e!r}")
            return ""

    async def find_best_match(self, target_user_id: int, waiting_candidates: dict,
                              chat_id: int = None) -> Optional[int]:
        """Подбирает лучшего кандидата с той же темой по формулам из matching.py.

        waiting_candidates: {id: {"topic", "interests", "subs", "t"}}, где id — идентификатор чата в очереди.
        """
        print(f"🎯 [MATCHMAKING] Chat ID: {chat_id} | Поиск совпадений для {target_user_id}")
        try:
            result = await self.match_app.ainvoke({
                "target": target_user_id, "candidates": waiting_candidates,
            })
            best = result.get("best")
            if best is not None:
                print(f"✅ [MATCHMAKING] Найдена пара: {target_user_id} <-> {best}")
            else:
                print(f"⏳ [MATCHMAKING] Для {target_user_id} пока нет подходящего собеседника")
            return best
        except Exception as e:
            print(f"❌ Ошибка find_best_match (Target: {target_user_id}): {e!r}")
            return None

    async def check_toxicity(self, text: str, user_id: int = None, chat_id: int = None) -> bool:
        """Проверка сообщения на токсичность."""
        prompt = (
            "Ты модератор диалогов между людьми. Твоя единственная задача — выявлять ПРЯМОЙ БУЛЛИНГ И ОСКОРБЛЕНИЯ.\n\n"
            "ЧТО СЧИТАЕТСЯ TOXIC:\n"
            "- Прямые оскорбления собеседника, маты, угрозы, хамство.\n\n"
            "ЧТО НЕ СЧИТАЕТСЯ TOXIC (отвечай OK):\n"
            "- Выражение мнения о спорте, аниме, играх, фильмах, обычный юмор и сленг.\n\n"
            f'Текст сообщения: """{text}"""\n\n'
            "Ответь строго 'TOXIC' или 'OK'."
        )
        try:
            res = (await self._invoke([HumanMessage(content=prompt)])).upper()
            is_toxic = "TOXIC" in res and "OK" not in res.replace("TOXIC", "")
            if is_toxic:
                print(f"⚠️ [TOXICITY] Chat ID: {chat_id} | User ID: {user_id}: '{text}'")
            return is_toxic
        except Exception as e:
            print(f"❌ Ошибка check_toxicity (Chat ID: {chat_id}, User ID: {user_id}): {e!r}")
            return False