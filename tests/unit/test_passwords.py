"""``utils.passwords`` 的单元测试（纯函数，无需任何外部服务，可离线运行）。

这里锁定的是一组**契约**，而不是实现细节：

1. 哈希可往返校验、同一密码两次哈希结果不同（随机 salt）、哈希串里不含明文；
2. 密码规则**只有一份**：``auth.create_user``（注册）与 ``auth.change_password``（改密码）
   都调用 ``validate_password_strength``——"注册有校验、改密码没校验"是最典型的口令漏洞；
3. **bcrypt 只取前 72 字节**：超长密码必须被显式**拒绝**，而不是静默截断，
   否则两个仅在第 73 字节之后不同的密码可以互相登录；
4. 哈希为空 / 损坏时校验必须返回 ``False`` 而不是抛异常（未捕获异常在 Web 层就是 500）。

运行：``.venv\\Scripts\\python.exe -m pytest tests/unit -v``
"""
import pytest

from utils.passwords import (
    BCRYPT_MAX_BYTES,
    MIN_PASSWORD_LENGTH,
    hash_password,
    validate_password_strength,
    verify_password,
)


class TestHashAndVerify:
    def test_roundtrip(self):
        hashed = hash_password("ChangeMe_123!")
        assert verify_password("ChangeMe_123!", hashed) is True

    def test_wrong_password_rejected(self):
        hashed = hash_password("ChangeMe_123!")
        assert verify_password("ChangeMe_123", hashed) is False      # 少一个字符
        assert verify_password("changeme_123!", hashed) is False     # 大小写不同
        assert verify_password("", hashed) is False

    def test_hash_is_salted(self):
        # 同一个密码两次哈希必须不同，否则哈希表/彩虹表可以直接复用
        assert hash_password("same_password") != hash_password("same_password")

    def test_hash_does_not_contain_plaintext(self):
        plain = "ChangeMe_123!"
        assert plain not in hash_password(plain)

    def test_hash_is_bcrypt_format(self):
        hashed = hash_password("ChangeMe_123!")
        assert hashed.startswith("$2")
        assert len(hashed) == 60

    def test_chinese_password_roundtrip(self):
        hashed = hash_password("中文密码也很安全")
        assert verify_password("中文密码也很安全", hashed) is True
        assert verify_password("中文密码不安全", hashed) is False


class TestValidatePasswordStrength:
    @pytest.mark.parametrize(
        "bad, expected_msg",
        [
            ("", "密码不能为空"),
            (None, "密码不能为空"),
            ("a" * (MIN_PASSWORD_LENGTH - 1), f"密码至少 {MIN_PASSWORD_LENGTH} 位"),
            # 3 个 ASCII + 24 个汉字 = 75 字节，超过 bcrypt 上限
            ("A1!" + "中" * 24, f"密码不能超过 {BCRYPT_MAX_BYTES} 字节"),
        ],
    )
    def test_rejects(self, bad, expected_msg):
        ok, msg = validate_password_strength(bad)
        assert ok is False
        assert msg == expected_msg

    @pytest.mark.parametrize(
        "good",
        [
            "a" * MIN_PASSWORD_LENGTH,     # 刚好 8 位
            "ChangeMe_123!",
            "带空格的 密码 12345",          # 中文 + 空格 + 数字
            "中" * 24,                     # 24 个汉字 = 72 字节，正好卡在上限
        ],
    )
    def test_accepts(self, good):
        ok, msg = validate_password_strength(good)
        assert ok is True
        assert msg == ""


class TestBcryptBoundary:
    def test_ascii_boundary_72_ok_73_rejected(self):
        assert validate_password_strength("x" * BCRYPT_MAX_BYTES)[0] is True
        assert validate_password_strength("x" * (BCRYPT_MAX_BYTES + 1))[0] is False

    def test_two_long_passwords_sharing_prefix_are_both_rejected(self):
        # 这就是"为什么必须拒绝而不是截断"的证据：两者前 72 字节相同但确实是不同密码
        a = "p" * BCRYPT_MAX_BYTES + "AAA"
        b = "p" * BCRYPT_MAX_BYTES + "BBB"
        assert a != b
        assert validate_password_strength(a)[0] is False
        assert validate_password_strength(b)[0] is False

    def test_hash_truncates_at_72_bytes_as_last_resort(self):
        # 兜底行为：证明 bcrypt 确实只吃前 72 字节（所以任何绕过校验的调用点
        # 都会引入"前 72 字节相同即可登录"的漏洞，validate 必须先跑）
        long_pwd = "p" * BCRYPT_MAX_BYTES + "TRAILING"
        prefix = "p" * BCRYPT_MAX_BYTES
        assert verify_password(prefix, hash_password(long_pwd)) is True


class TestHashRobustness:
    @pytest.mark.parametrize("broken", [None, "", "not-a-hash", "$2b$broken", "x" * 59])
    def test_broken_hash_returns_false_not_exception(self, broken):
        assert verify_password("ChangeMe_123!", broken) is False
