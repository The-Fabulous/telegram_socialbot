import os
import re
import logging
from typing import Any, Dict, List, Optional, TypedDict

from dotenv import load_dotenv
from openai import BadRequestError
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

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
    "1. Общаешься наравне, подстраивайся под вайб человека (можно использовать разговорные слова, сленг, легкую иронию).\n"
    "2. Не будь занудным допросчиком: не нужно после каждой фразы выдавать стандартные вопросы из анкеты.\n"
    "3. Отвечай коротко (1-2 предложения), как пишут в мессенджерах.\n"
    "4. Не спамь эмодзи (никаких картиночных эмодзи; максимум — обычная текстовая скобка ')' если к месту)."
)

# ==========================================
# 1.1 ТЕМЫ ДЛЯ МЭТЧИНГА
# ==========================================

TOPICS = {
    "anime": "🎌 Аниме",
    "football": "⚽ Футбол",
    "games": "🎮 Игры",
    "music": "🎵 Музыка",
    "movies": "🎬 Кино и сериалы",
    "tech": "💻 Технологии",
    "other": "✨ Другое",
}

TOPIC_KEYWORDS = {
    "anime": ["аниме", "anime", "манга", "manga", "наруто", "ван пис", "one piece", "ходячий замок", "клинок", "гиасс"],
    "football": ["футбол", "football", "лига чемпионов", "месси", "роналду", "messi", "ronaldo", "матч", "спартак",
                 "реал"],
    "games": ["игр", "game", "гейм", "dota", "дота", "minecraft", "майнкрафт", "cs2", "кс2", "playstation", "тиммейт",
              "катать", "задрот"],
    "music": ["музык", "песн", "трек", "рэп", "music", "концерт", "альбом", "групп"],
    "movies": ["фильм", "кино", "сериал", "netflix", "нетфликс", "режиссер", "марвел"],
    "tech": ["программ", "python", "технолог", "гаджет", "нейросет", "айти", "код", "разработчик"],
}


def topic_name(key: str) -> str:
    label = TOPICS.get(key, key)
    return label.split(" ", 1)[1] if " " in label else label


def _match_topic_keywords(text: str) -> Optional[str]:
    low = (text or "").lower()
    for key, words in TOPIC_KEYWORDS.items():
        if any(w in low for w in words):
            return key
    return None


def _text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "").strip()


def _clean(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[\*\-•]\s+", "", text)
    return text.replace("*", "").replace("`", "").strip()


def _flatten(messages: List[BaseMessage]) -> List[BaseMessage]:
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


class ChatState(TypedDict, total=False):
    messages: List[BaseMessage]
    reply: str
    user_id: Optional[int]
    chat_id: Optional[int]


class MatchState(TypedDict, total=False):
    target: int
    candidates: Dict[int, Dict[str, str]]
    pool: Dict[int, str]
    best: Optional[int]


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
            timeout=30,
            max_retries=2,
        )
        self._flat_mode = False
        self.chat_app = self._build_chat_graph()
        self.match_app = self._build_match_graph()

    async def _invoke(self, messages: List[BaseMessage]) -> str:
        if not self._flat_mode:
            try:
                return _text(await self.llm.ainvoke(messages))
            except BadRequestError as e:
                logger.warning(f"400 от модели, переключаюсь на запасной формат: {e}")
                self._flat_mode = True
        return _text(await self.llm.ainvoke(_flatten(messages)))

    def _build_chat_graph(self):
        async def respond(state: ChatState) -> Dict[str, Any]:
            return {"reply": await self._invoke(state["messages"])}

        async def finalize(state: ChatState) -> Dict[str, Any]:
            return {"reply": _clean(state.get("reply", ""))}

        graph = StateGraph(ChatState)
        graph.add_node("respond", respond)
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "respond")
        graph.add_edge("respond", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    async def generate_welcome(self, user_name: str, user_id: int = None, chat_id: int = None) -> str:
        return f"Здарова, {user_name}! Чо по интересам? Во что играешь или что смотришь?"

    async def get_comfort_response(self, history: list, user_text: str, user_id: int = None,
                                   chat_id: int = None) -> str:
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
                "messages": messages, "user_id": user_id, "chat_id": chat_id,
            })
            return result.get("reply") or "Слушаю тебя."
        except Exception as e:
            print(f"❌ Ошибка get_comfort_response: {e}")
            return "Слушаю тебя."

    async def extract_interests(self, user_messages: list, user_id: int = None, chat_id: int = None) -> dict:
        """Определяет тему и сводит интересы пользователя в компактный вид для словаря мэтчинга."""
        full_text = " ".join(m.get("content", "") for m in user_messages if m.get("role") in ("user", "me"))
        topic = _match_topic_keywords(full_text)

        if not topic:
            # Запасной вариант через LLM, если по ключевым словам не нашлось
            keys = ", ".join(TOPICS)
            prompt = (
                f"Определи тему из списка ({keys}) по сообщениям:\n{full_text}\n"
                "Ответь строго одним ключом темы (например: games, anime, music и т.д.) или other."
            )
            try:
                res = (await self._invoke([HumanMessage(content=prompt)])).lower().strip()
                for k in TOPICS:
                    if k in res:
                        topic = k
                        break
            except Exception:
                pass

        if not topic:
            topic = "other"

        return {
            "topic": topic,
            "interests": full_text[:300]  # Сохраняем суть для сравнения
        }

    async def check_toxicity(self, text: str, user_id: int = None, chat_id: int = None) -> bool:
        return False

    def _build_match_graph(self):
        async def filter_topic(state: MatchState) -> Dict[str, Any]:
            target = state["target"]
            candidates = state.get("candidates", {})
            target_info = candidates.get(target, {})
            topic = target_info.get("topic")

            pool = {
                cid: info.get("interests", "")
                for cid, info in candidates.items()
                if cid != target and info.get("topic") == topic
            }
            return {"pool": pool, "best": None}

        def route_pool(state: MatchState) -> str:
            n = len(state.get("pool", {}))
            return "none" if n == 0 else ("single" if n == 1 else "pick")

        async def single(state: MatchState) -> Dict[str, Any]:
            return {"best": next(iter(state["pool"]))}

        async def llm_pick(state: MatchState) -> Dict[str, Any]:
            target = state["target"]
            info = state["candidates"][target]
            pool = state["pool"]
            candidates_str = "\n".join(f"ID {cid}: {text}" for cid, text in pool.items())
            prompt = (
                f"Тема: {topic_name(info.get('topic', ''))}.\n"
                f"Главный пользователь:\n\"{info.get('interests', '')}\"\n\n"
                f"Кандидаты с той же темой:\n{candidates_str}\n\n"
                "Выбери ID ОДНОГО кандидата, чьи интересы ближе всего к главному пользователю.\n"
                "Ответ строго числом (ID)."
            )
            try:
                res = await self._invoke([HumanMessage(content=prompt)])
                for m in re.findall(r"\d+", res):
                    if int(m) in pool:
                        return {"best": int(m)}
            except Exception as e:
                print(f"❌ Ошибка llm_pick: {e}")
            return {"best": next(iter(pool))}

        graph = StateGraph(MatchState)
        graph.add_node("filter", filter_topic)
        graph.add_node("single", single)
        graph.add_node("pick", llm_pick)

        graph.add_edge(START, "filter")
        graph.add_conditional_edges("filter", route_pool, {
            "none": END,
            "single": "single",
            "pick": "pick"
        })
        graph.add_edge("single", END)
        graph.add_edge("pick", END)
        return graph.compile()

    async def find_best_match(self, target_user_id: int, waiting_candidates: dict, chat_id: int = None) -> Optional[
        int]:
        try:
            result = await self.match_app.ainvoke({
                "target": target_user_id,
                "candidates": waiting_candidates,
            })
            return result.get("best")
        except Exception as e:
            print(f"❌ Ошибка find_best_match: {e}")
            return None