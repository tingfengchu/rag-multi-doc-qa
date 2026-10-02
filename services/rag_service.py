from langchain_openai import ChatOpenAI
from config import settings

# 初始化智谱大模型
llm = ChatOpenAI(
    model="glm-4-flash",
    temperature=0,  # 强制最低温度，抑制幻觉
    api_key=settings.zhipu_api_key,
    base_url=settings.openai_api_base,
)

def generate_answer(question: str, retrieved_docs: list) -> str:
    """
    将检索到的文档碎片和用户问题组合，调用大模型生成最终答案。
    """
    if not retrieved_docs:
        return "未在知识库找到相关信息，无法回答。"

    # 1. 将检索到的碎片拼成上下文，并带上引用编号
    context_parts = []
    for i, doc in enumerate(retrieved_docs, 1):
        source = doc.metadata.get('source', '未知')
        page = int(doc.metadata.get('page', 0)) + 1
        piece = f"[{i}] 来源：{source} 第 {page} 页\n{doc.page_content}"
        context_parts.append(piece)
    
    context = "\n\n".join(context_parts)

    # 2. 强约束 Prompt
    prompt = f"""你是一个极其死板、不会变通的文档问答助手。

    ⚠️ 三条绝对红线：
    1. 你只能根据下方提供的【文档资料】进行回答。
    2. 严禁使用你自己的任何内部知识！如果【文档资料】里没有明确写出，你就必须无视你的知识！
    3. 如果找不到答案，你必须一字不差地回答：“文档中未提供该信息，无法回答。”

    📝 输出要求：
    - 在回答的句末，**必须**用方括号标出引用的来源编号，例如：[1]、[2]。
    - 不要编造没有引用的句子。

    【文档资料】：
    {context}

    用户问题：{question}
    最终回答："""

    try:
        response = llm.invoke(prompt)
        return response.content
    except Exception as e:
        return f"❌ 模型调用失败：{e}"