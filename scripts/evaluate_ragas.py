"""RAGAS 检索质量评测脚本（命令行）。

用法（项目根目录下）：

    .venv\\Scripts\\python.exe scripts\\evaluate_ragas.py
    .venv\\Scripts\\python.exe scripts\\evaluate_ragas.py --scope public --limit 10 --k 10
    .venv\\Scripts\\python.exe scripts\\evaluate_ragas.py --min-score 0.7   # 质量门禁，可接 CI

前置条件：
    1. MySQL / ES / Redis / Chroma 都已启动，且目标 scope 里已经有文档；
    2. 已安装评测依赖，见 requirements-eval.txt；
    3. .env / rag_config.txt 里配好了 ZHIPU_API_KEY（评测用 glm-4-flash 当裁判）

⚠️ 不要试图把 ragas 装进主 .venv，两条路都堵死了（已实测）：
    * 新版 ragas（≥0.3.5）：经 instructor 要求 openai<2，
      会把 openai 3.29.0 → 1.109.1、langchain-openai 1.7.0 → 1.1.9、rich 15.0.0 → 13.9.4，
      砸的正是线上生成链路用的 LLM SDK；
    * 旧版 ragas（0.2.15 / 0.3.0）：安装无冲突，但 import 直接失败——
      ragas/llms/base.py 顶层 import 了 langchain_community.chat_models.vertexai，
      而该类在 langchain-community 0.4.x 已被删除。
    所以评测依赖必须装进独立的 .venv-eval，见 requirements-eval.txt 顶部说明。
"""
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from services import eval_service


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="用 RAGAS 给 RAG 链路打质量分")
    parser.add_argument(
        "--scope", default="public",
        help="评测哪个知识库，如 public 或 private:<uuid>",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题（省 token）")
    parser.add_argument("--k", type=int, default=10, help="单路召回数量 k")
    parser.add_argument(
        "--golden-set", default=None,
        help="黄金测试集路径，默认 evaluation/golden_set.py（也支持外部 .json）",
    )
    parser.add_argument(
        "--min-score", type=float, default=None,
        help="质量门禁：任一指标低于该值时退出码为 1（适合 CI）",
    )
    parser.add_argument("--no-report", action="store_true", help="不写 evaluation_output/ 下的 JSON 报告")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    if not eval_service.ragas_available():
        print("❌ 未安装 RAGAS 依赖（主环境刻意没装，原因见下）。")
        print("")
        print("   请另建一个专跑评测的虚拟环境，别装进主 .venv：")
        print("     python -m venv .venv-eval")
        print(r"     .venv-eval\Scripts\pip install -r requirements.txt -r requirements-eval.txt")
        print(r"     .venv-eval\Scripts\python.exe scripts\evaluate_ragas.py --scope public --limit 5")
        print("")
        print("   原因（已实测确认）：")
        print("     * ragas ≥0.3.5 经 instructor 要求 openai<2，装进主环境会把")
        print("       openai 3.29.0 降级到 1.109.1，连带 langchain-openai / rich 一起降级；")
        print("     * ragas 0.2.15 / 0.3.0 虽然不降级，但 import 会失败：")
        print("       ModuleNotFoundError: langchain_community.chat_models.vertexai")
        print("       （该类在 langchain-community 0.4.x 已被删除）")
        return 2

    print("=" * 68)
    print("📊 RAGAS 自动评测")
    print("=" * 68)
    print(f"  scope        : {args.scope}")
    print(f"  黄金测试集    : {args.golden_set or eval_service.DEFAULT_GOLDEN_SET}")
    print(f"  limit / k    : {args.limit or '全部'} / {args.k}")
    print(f"  ragas 版本    : {eval_service.ragas_version()}")
    print("=" * 68)

    try:
        report = eval_service.run_ragas_evaluation(
            scope=args.scope,
            limit=args.limit,
            k=args.k,
            golden_set_path=args.golden_set,
            write_report=not args.no_report,
        )
    except Exception as e:
        print(f"❌ 评测失败：{e}")
        return 1

    print(f"\n样本数：{report['sample_count']}    裁判模型：{report['judge_model']}")
    print("-" * 68)
    for line in eval_service.format_scores(report["scores"]):
        print(line)
    print("-" * 68)

    if report.get("report_path"):
        print(f"完整报告：{report['report_path']}")

    if args.min_score is not None:
        bad = {name: v for name, v in report["scores"].items() if v < args.min_score}
        if bad:
            print(f"\n❌ 以下指标低于门禁 {args.min_score}：{bad}")
            return 1
        print(f"\n✅ 全部指标均不低于门禁 {args.min_score}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
