from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from config import settings

rewrite_llm = ChatOpenAI(
    model="glm-4-flash",
    temperature=0,
    api_key=settings.zhipu_api_key,
    base_url=settings.openai_api_base,
)

REWRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "你是一个查询重写助手。你的唯一任务是判断是否需要把用户的【追问】改写为一个独立问题。\n"
     "⚠️ 绝对红线（极其重要）：\n"
     "1. 如果用户的问题已经是一个完整、独立的问题（如'张嘉亮是谁？'），必须【原样输出】，绝对不允许添加背景、不允许总结、更不允许回答！\n"
     "2. 只有当用户的提问包含代词（'他'、'她'、'它'、'这个'、'那个'）或省略句时，才允许结合历史对话改写。\n"
     "3. 你只负责改写问题，绝对禁止输出任何答案内容！\n"
     "4. 只输出改写后的问题，不要解释，不要加引号。"),
    ("human", "【历史对话】\n{history}\n\n【追问】\n{question}\n\n【改写后的问题】")
])


def rewrite_query(question: str, history: list) -> str:
    if not history:
        return question

    recent = history[-8:]
    history_text = "\n".join(
        [f"{item['role']}: {item['content']}" for item in recent]
    )

    try:
        chain = REWRITE_PROMPT | rewrite_llm
        response = chain.invoke({"history": history_text, "question": question})
        rewritten = response.content.strip().strip('"').strip("'")
        print(f"🔄 Query Rewrite: '{question}' -> '{rewritten}'")
        return rewritten if rewritten else question
    except Exception as e:
        print(f"❌ Query Rewrite 失败，降级使用原问题: {e}")
        return question