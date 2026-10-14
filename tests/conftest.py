"""pytest 全局夹具：跑完自动清掉**测试自建的账号**。

为什么需要（用户提的保洁项）：E2E / 集成测试会用 ``POST /api/auth/register`` 建临时
账号（``e2e_scope_*``、``e2e_rerank_*`` …），而账号**没有删除接口**，于是每跑一轮就
攒几个，管理端用户列表越拉越长。清理逻辑必须"跑完必定执行一次"——而 session 级夹具
是 pytest 里唯一能保证这一点的位置（写在某条用例的 teardown 里会漏：中途失败就跳过了）。

⚠️ 三条硬约束（任一不满足，宁可**不清理**）：

1. 只删前缀在白名单里的账号（``scripts.purge_e2e_users.TEST_USER_PREFIXES``），
   绝不按"最近创建 / 没有文档"这类判据删——这是删用户表，误删不可逆；
2. **连不上数据库就静默跳过**：纯离线跑单测（没有 MySQL、没有 ``.env``）的机器上，
   这条夹具不许把测试结果拖红；
3. 清理失败只打印一行，绝不 raise —— 测试的结论不能被"保洁没做好"推翻。
"""

import os

import pytest

#: ``lying_stat`` 夹具让 ``.xlsx`` 的 ``os.stat`` 比**真实可读长度**多报的字节数。
#: 数量级取自 2026-10 排查「Excel 下载失败」时的实测（同一份 12898 字节的 xlsx，
#: ``stat`` 报 20480，即真实长度 + 7582）。
#: 本机 stat 对这类文件本来就会多报（实测写 304 字节的 ``.xlsx``，``stat`` 直接报 8192，
#: 成因未追），夹具把偏差**定死**成"真实长度 + 7582"，用例的断言才不会随文件系统而变。
STAT_OVERREPORT = 7582


@pytest.fixture()
def lying_stat(monkeypatch):
    """让 ``os.stat`` 对 ``.xlsx`` 报"真实可读长度 + ``STAT_OVERREPORT``"，返回多报量。

    为什么要有这么个夹具：Excel 下不来的根因既不是权限、也不是 MIME，而是
    **响应声明的长度取自 ``os.stat``**（``FileResponse`` 内部就是这么算 ``Content-Length`` 的）。
    本机对 ``.xlsx`` 报 20480、实际只发得出 12898 字节 → 浏览器读到最后一段连接断掉，
    下载栏里弹「失败 - 无法下载」。PDF 的 stat 与真实长度恰好一致，所以同一个接口下 PDF 正常。

    patch ``os`` 模块上的 ``stat`` 就够了：Starlette 是**请求时**才去查它
    （``anyio.to_thread.run_sync(os.stat, path)``），``os.path.getsize`` 也只是转发到它；
    非 ``.xlsx`` 的路径一律原样调用真实现，所以这一个用例之外的代码不受影响。

    ⚠️ 它模拟的是**真实存在过的现象**，不是凭空造的：修代码时请保证
    「声明长度 == 真实发出去的字节数」这条不变量仍然成立。
    """
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if not str(path).lower().endswith(".xlsx"):
            return result
        try:
            # 真实可读长度：open + seek(0, SEEK_END) + tell()，全程不碰 stat（否则自己递归）
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                readable = fh.tell()
        except OSError:  # 目录 / 打不开：原样返回，别把别的调用带坏
            return result
        return os.stat_result(
            (
                result.st_mode,
                result.st_ino,
                result.st_dev,
                result.st_nlink,
                result.st_uid,
                result.st_gid,
                readable + STAT_OVERREPORT,
                result.st_atime,
                result.st_mtime,
                result.st_ctime,
            )
        )

    monkeypatch.setattr(os, "stat", fake_stat)
    return STAT_OVERREPORT


@pytest.fixture(scope="session", autouse=True)
def _purge_test_accounts():
    """会话结束后清一次测试账号（详见模块 docstring）。"""
    yield
    try:
        # 延迟 import：`db` 会连带 import `config`（需要 .env），放在函数里
        # 才不会在"收集测试"阶段就炸掉整个会话。
        from scripts.purge_e2e_users import purge_test_users

        removed = purge_test_users()
    except Exception as exc:  # noqa: BLE001 - 保洁失败不该影响测试结论
        print(f"[conftest] 测试账号清理已跳过：{exc}")
        return
    if removed:
        print(f"[conftest] 已清理 {removed} 个测试账号（e2e_ / test_ 前缀）")

