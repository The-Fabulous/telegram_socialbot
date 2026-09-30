import os
import re
import json
from typing import Annotated, Literal, TypedDict
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

load_dotenv()

# ==========================================
# 1. СИСТЕМНЫЙ ПРОМПТ (СВОЙСКИЙ / ВАЙБОВЫЙ)
# ==========================================

HUMAN_SYSTEM_PROMPT = (
    "Ты — приятный, свойский и живой собеседник. Общаешься легко, с юмором и без занудства.\n\n"
    "Правила стиля:\n"
    "1. Общайся наравне, подстраивайся под вайб человека (можно использовать разговорные слова, сленг, легкую иронию).\n"
    "2. Не будь занудным допросчиком: не нужно после каждой фразы выдавать стандартные вопросы из анкеты.\n"
    "3. Отвечай коротко (1-2 предложения), как пишут в мессенджерах.\n"
    "4. Не спамь эмодзи (никаких картиночных эмодзи; максимум — обычная текстовая скобка ')' если к месту)."
)


# ==========================================
# 2. ТУЛЗЫ (TOOLS)
# ==========================================

@tool
def trigger_math_captcha(user_id: int) -> str:
    """Вызывает математическую капчу для проверки пользователя."""
    return json.dumps({
        "status": "captcha_required",
        "question": "Сколько будет 7 + 3?",
        "answer": 10,
        "user_id": user_id
    })


tools = [trigger_math_captcha]


# ==========================================
# 3. СОСТОЯНИЕ (STATE)
# ==========================================

class BotState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    chat_mode: str  # "bot", "captcha_pending", "human"
    user_message_count: int  # Счётчик сообщений от пользователя
    user_id: int
    partner_id: int | None  # ID собеседника (если найден)


# ==========================================
# 4. ИНИЦИАЛИЗА МОДЕЛИ
# ==========================================

llm = ChatOpenAI(
    model="gemma4",
    api_key=os.getenv("OPENAI_API_KEY") or os.getenv("ALEM_API_KEY"),
    base_url="https://llm.alem.ai/v1",
    temperature=0.6
).bind_tools(tools)


# ==========================================
# 5. УЗЛЫ ГРАФА (NODES)
# ==========================================

async def bot_agent_node(state: BotState):
    """Главный узел бота: считает сообщения, отвечает и переключает режим при пороге в 3 сообщения."""
    current_count = state.get("user_message_count", 0) + 1

    # Быстрый перехват: 3 сообщения от пользователя
    if current_count >= 3:
        print(f"[LANGGRAPH LOG] 🔀 Достигнуто 3 сообщения. Переключение на режим 'human'.")
        return {
            "user_message_count": current_count,
            "chat_mode": "human"
        }

    messages = [SystemMessage(content=HUMAN_SYSTEM_PROMPT)] + state["messages"]
    response = await llm.ainvoke(messages)

    return {
        "messages": [response],
        "user_message_count": current_count
    }


async def human_transfer_node(state: BotState):
    """Узел передачи управления человеку."""
    system_msg = AIMessage(
        content="Отлично поболтали) Переключаю тебя на поиск реального собеседника."
    )
    return {
        "messages": [system_msg],
        "chat_mode": "human"
    }


# ==========================================
# 6. УСЛОВНЫЕ ПЕРЕХОДЫ (CONDITIONAL EDGES)
# ==========================================

def route_after_bot(state: BotState) -> Literal["tools", "human_transfer_node", "__end__"]:
    if state.get("chat_mode") == "human":
        return "human_transfer_node"

    messages = state["messages"]
    if messages:
        last_message = messages[-1]
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

    return END


# ==========================================
# 7. СБОРКА ГРАФА (BUILD GRAPH)
# ==========================================

workflow = StateGraph(BotState)

workflow.add_node("bot_agent", bot_agent_node)
workflow.add_node("tools", ToolNode(tools))
workflow.add_node("human_transfer", human_transfer_node)

workflow.add_edge(START, "bot_agent")

workflow.add_conditional_edges(
    "bot_agent",
    route_after_bot,
    {
        "tools": "tools",
        "human_transfer_node": "human_transfer",
        END: END
    }
)

workflow.add_edge("tools", "bot_agent")
workflow.add_edge("human_transfer", END)

app = workflow.compile()


# ==========================================
# 8. КЛАСС-АДАПТЕР С ПОДРОБНЫМ ЛОГИРОВАНИЕМ
# ==========================================

class SocialBridgeAI:
    def __init__(self):
        self.model_name = "gemma4"

    async def generate_welcome(self, user_name: str) -> str:
        """Генерирует естественное короткое приветствие."""
        try:
            prompt = f"Поприветствуй пользователя по имени '{user_name}'."
            welcome_system_prompt = (
                "Ты живой и свойский собеседник. Напиши одно короткое легкое приветствие в телеграм-стиле. "
                "Без картиночных эмодзи."
            )
            response = await llm.ainvoke([
                SystemMessage(content=welcome_system_prompt),
                HumanMessage(content=prompt)
            ])
            return response.content
        except Exception as e:
            print(f"❌ Ошибка generate_welcome: {e}")
            return f"Привет, {user_name}! Как сам?"

    async def get_comfort_response(self, history: list, user_text: str) -> str:
        """Формирует полный пакет сообщений, выводит лог и запрашивает ответ у Gemma 4."""
        try:
            formatted_messages = []
            for m in history:
                if m.get("role") == "user":
                    formatted_messages.append(HumanMessage(content=m["content"]))
                elif m.get("role") == "assistant":
                    formatted_messages.append(AIMessage(content=m["content"]))

            formatted_messages.append(HumanMessage(content=user_text))

            full_prompt_payload = [SystemMessage(content=HUMAN_SYSTEM_PROMPT)] + formatted_messages

            # ================= LOGS =================
            print("\n" + "=" * 60)
            print("🚀 [GEMMA PROMPT LOG] Отправка полного промпта в Gemma 4:")
            print("-" * 60)
            for idx, msg in enumerate(full_prompt_payload, start=1):
                msg_type = type(msg).__name__
                print(f" [{idx}] {msg_type}: {msg.content}")
            print("=" * 60 + "\n")
            # ========================================

            response = await llm.ainvoke(full_prompt_payload)

            print(f"📥 [GEMMA RESPONSE LOG]: {response.content}\n")
            return response.content

        except Exception as e:
            print(f"❌ Ошибка get_comfort_response: {e}")
            return "Слушаю тебя. Рассказывай."

    async def extract_interests(self, user_messages: list) -> str:
        """Извлекает ключевые интересы пользователя."""
        text = " ".join([m["content"] for m in user_messages if m.get("role") == "user"])
        if not text.strip():
            return ""

        prompt = (
            f"Проанализируй текст пользователя и кратко перечисли его главные интересы/увлечения:\n\n\"{text}\""
        )
        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            return response.content.strip()
        except Exception as e:
            print(f"❌ Ошибка extract_interests: {e}")
            return ""

    async def find_best_match(self, target_user_id: int, waiting_candidates: dict) -> int | None:
        """Подбирает лучшего кандидата из очереди."""
        target_interests = waiting_candidates.get(target_user_id, "")
        other_candidates = {uid: text for uid, text in waiting_candidates.items() if uid != target_user_id}

        if not other_candidates:
            return None

        candidates_str = "\n".join([f"ID {uid}: {text}" for uid, text in other_candidates.items()])

        prompt = (
            f"У нас есть главный пользователь с интересами:\n\"{target_interests}\"\n\n"
            f"И список кандидатов в очереди:\n{candidates_str}\n\n"
            f"Выбери ID ОДНОГО кандидата, чей профиль и интересы максимально совпадают с главным пользователем. "
            f"Ответь СТРОГО только цифрами его ID (без лишнего текста)."
        )

        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            res = response.content.strip()

            match = re.search(r'\b\d+\b', res)
            if match:
                matched_id = int(match.group())
                if matched_id in other_candidates:
                    return matched_id
        except Exception as e:
            print(f"❌ Ошибка find_best_match: {e}")

        return list(other_candidates.keys())[0]

    async def check_toxicity(self, text: str) -> bool:
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
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            res_text = response.content.strip().upper()
            return "TOXIC" in res_text
        except Exception as e:
            print(f"❌ Ошибка check_toxicity: {e}")
            return False