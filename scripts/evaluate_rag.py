"""
轻量版 RAG 评估脚本（不依赖 ragas / datasets，只用一次 LLM 打分）。

⚠️ 正式评测请用 ``scripts/evaluate_ragas.py``（RAGAS 四指标：
   faithfulness / answer_relevancy / context_precision / context_recall，
   评测集在 evaluation/golden_set.py）。本脚本只作为"不想装额外依赖时"
   的快速冒烟手段保留，它的问题是：只靠关键词命中率判断，人工可控性差、
   也拿不到召回/精确率这类检索侧指标。

用法：python scripts/evaluate_rag.py
它会拿一组问题去调用你的检索+生成逻辑，然后用 LLM 自动打分。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_openai import ChatOpenAI
from config import settings
from services import vector_service, retrieve_service, rag_service

# 黄金测试集（你可以根据实际文档内容自己补充）
GOLDEN_SET = [
    {
        "question": "张嘉亮的专业技能是什么？",
        "expected_keywords": ["LangChain", "RAG", "BM25", "Rerank"]
    },
    {
        "question": "SAGE AI 的在线问答流程是什么？",
        "expected_keywords": ["身份", "检索", "重排", "生成", "反馈"]
    },
    {
        "question": "新员工信息表里有哪些字段？",
        "expected_keywords": ["工号", "姓名", "紧急联系人", "劳动合同"]
    },
]

JUDGE_PROMPT = """你是一个严格的RAG评估员。请根据【标准答案关键词】和【模型生成的回答】进行打分。

评分维度：
1. 忠实度（Faithfulness，1-5分）：回答是否完全基于检索到的资料，没有编造？
2. 相关性（Relevancy，1-5分）：回答是否切题，是否覆盖了关键词？

【标准答案关键词】：{keywords}
【模型回答】：{answer}

请只输出 JSON，格式：{{"faithfulness": 4, "relevancy": 5, "reason": "简短理由"}}
"""


def evaluate():
    judge_llm = ChatOpenAI(
        model="glm-4-flash", temperature=0,
        api_key=settings.zhipu_api_key,
        base_url=settings.openai_api_base,
    )

    scope = "public"
    vs = vector_service.get_vectorstore(scope)

    total_faith, total_rel = 0, 0
    print(f"\n{'='*60}")
    print(f"📊 RAG 自动评估开始（黄金集 {len(GOLDEN_SET)} 条）")
    print(f"{'='*60}\n")

    for idx, item in enumerate(GOLDEN_SET, 1):
        q = item["question"]
        keywords = "、".join(item["expected_keywords"])

        # 1. 走完整检索链路
        retrieved_docs = retrieve_service.get_hybrid_retriever_with_es(
            query=q, scope=scope, vectorstore=vs, k=10,
            enable_rerank=True,
            model_name="cross-encoder/ms-marco-MiniLM-L-6-v2"
        )
        answer = rag_service.generate_answer(q, retrieved_docs)

        # 2. LLM 打分
        judge_input = JUDGE_PROMPT.format(keywords=keywords, answer=answer)
        try:
            result = judge_llm.invoke(judge_input).content
            import json
            scores = json.loads(result.strip().strip("```json").strip("```"))
            faith = scores.get("faithfulness", 0)
            rel = scores.get("relevancy", 0)
        except Exception as e:
            print(f"⚠️ 第 {idx} 题打分失败：{e}")
            faith, rel = 0, 0

        total_faith += faith
        total_rel += rel

        print(f"【第 {idx} 题】{q}")
        print(f"  忠实度：{faith}/5 | 相关性：{rel}/5")
        print(f"  回答预览：{answer[:120]}...\n")

    n = len(GOLDEN_SET)
    print(f"{'='*60}")
    print(f"📈 平均忠实度：{total_faith/n:.2f}/5")
    print(f"📈 平均相关性：{total_rel/n:.2f}/5")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    evaluate()