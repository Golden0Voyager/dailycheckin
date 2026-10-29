"""从本机 Chrome 的 cookie 库里取出 modelscope 的登录态。

为什么需要它：``m_session_id`` 这些是 httpOnly cookie，页面 JS 读不到。但它们在本机
就是一份加密记录 —— Chrome 用 Keychain 里的「Chrome Safe Storage」密码派生密钥做
AES-128-CBC 加密，同一台 Mac 上可以直接解开。

这样 ``config.json`` 里写 ``"cookie": "from-chrome"`` 就能免手工导出；
在 CI 那种没有浏览器的环境里必须填真实 cookie 字符串。
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

CHROME_ROOT = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
CHROME_EPOCH_OFFSET = 11_644_473_600

#: 明确需要保留的 cookie；其余（_ga / source / 本地化偏好等）一律不要，少带少风险。
_WANTED = (
    "m_session_id",
    "csrf_session",
    "csrf_token",
    "cna",
    "t",
    "h_uid",
    "isg",
    "_uab_collina",
    "ssxmod_itna",
    "ssxmod_itna2",
    "acw_tc",
)

_PBKDF2_SALT = b"saltysalt"
_PBKDF2_ROUNDS = 1003
_KEY_BYTES = 16
_AES_IV = b" " * 16
_KEYCHAIN_CMD = (
    "security",
    "find-generic-password",
    "-w",
    "-s",
    "Chrome Safe Storage",
    "-a",
    "Chrome",
)


class CookieError(Exception):
    """读不到或解不开 cookie。消息里绝不包含 cookie 值。"""


def cookie_from_chrome(host_filter: str = "modelscope") -> str:
    """返回可直接放进请求头的 Cookie 字符串。"""
    profiles = [p for p in _candidate_profiles() if (p / "Cookies").exists()]
    if not profiles:
        raise CookieError(f"找不到 Chrome cookie 库：{CHROME_ROOT}")

    last: CookieError | None = None
    for profile in profiles:
        try:
            jar = _read_profile(profile, host_filter)
        except CookieError as exc:
            last = exc
            continue
        picked = [f"{name}={jar[name]}" for name in _WANTED if name in jar]
        if not picked:
            last = CookieError(f"{profile.name} 里没有 modelscope 的登录 cookie")
            continue
        return "; ".join(picked)
    raise last or CookieError("所有 Chrome profile 都没读到 modelscope cookie")


def _candidate_profiles() -> list[Path]:
    if not CHROME_ROOT.is_dir():
        return []
    found = [p for p in sorted(CHROME_ROOT.iterdir()) if p.is_dir()]
    return sorted(found, key=lambda p: (p.name != "Default", p.name))


def _read_profile(profile: Path, host_filter: str) -> dict[str, str]:
    with _snapshot(profile / "Cookies") as con:
        rows = con.execute(
            "select name, host_key, encrypted_value, value, expires_utc from cookies "
            "where host_key like ?",
            (f"%{host_filter}%",),
        ).fetchall()
    if not rows:
        raise CookieError(f"{profile.name} 里没有 modelscope 的 cookie")

    key: bytes | None = None
    jar: dict[str, str] = {}
    for name, host, encrypted, plain, _expires in rows:
        value = plain or ""
        if not value:
            if key is None:
                key = _derived_key()
            value = _decrypt(bytes(encrypted), key, host)
        if value:
            jar[name] = value
    if "m_session_id" not in jar:
        raise CookieError(f"{profile.name} 里没有 m_session_id（可能未登录）")
    return jar


@contextmanager
def _snapshot(db_path: Path):
    """给正在运行的 Chrome 拍一份一致的快照，避免读到写了一半的库。"""
    if not db_path.exists():
        raise CookieError(f"找不到 cookie 库：{db_path}")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "cookies.db"
        try:
            _backup(db_path, target)
        except sqlite3.Error:
            shutil.copy2(db_path, target)
            for suffix in ("-wal", "-shm"):
                sidecar = db_path.with_name(db_path.name + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, target.with_name(target.name + suffix))
        con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            yield con
        finally:
            con.close()


def _backup(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()


def _derived_key() -> bytes:
    try:
        out = subprocess.run(_KEYCHAIN_CMD, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CookieError(
            "读不到 Keychain 里的「Chrome Safe Storage」：首次需在钥匙串访问里授权给 security"
        ) from exc
    return hashlib.pbkdf2_hmac(
        "sha1", out.stdout.rstrip(b"\n"), _PBKDF2_SALT, _PBKDF2_ROUNDS, _KEY_BYTES
    )


def _decrypt(blob: bytes, key: bytes, host: str) -> str:
    if blob[:3] != b"v10":
        raise CookieError(
            f"暂不支持这种 cookie 加密格式（前缀 {blob[:3]!r}），Chrome 可能启用了应用绑定加密"
        )
    try:
        out = subprocess.run(
            ["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", _AES_IV.hex(), "-nopad"],
            input=blob[3:],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CookieError("cookie 解密失败（openssl 报错）") from exc

    raw = out.stdout
    if not raw or not 1 <= raw[-1] <= 16:
        raise CookieError("cookie 解密失败（填充不合法）")
    raw = raw[: -raw[-1]]
    digest = hashlib.sha256(host.encode()).digest()
    if raw.startswith(digest):
        raw = raw[len(digest):]
    try:
        return raw.decode()
    except UnicodeDecodeError as exc:
        raise CookieError("cookie 解密失败（内容不是文本）") from exc


if __name__ == "__main__":
    # 刷新本地 config.json 里 MODELSCOPE 的 cookie（cookie 失效时手动跑一次）。
    # 改完记得把新的 config.json 全文同步到仓库 Secret: DAILYCHECKIN_CONFIG
    import json
    import sys

    _root = Path(__file__).resolve().parents[2]
    _path = _root / "config.json"
    try:
        _cookie = cookie_from_chrome()
    except CookieError as exc:
        print(f"读取失败：{exc}")
        sys.exit(1)
    _data = json.loads(_path.read_text(encoding="utf-8"))
    _data["MODELSCOPE"] = [{"cookie": _cookie}]
    _path.write_text(json.dumps(_data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    _path.chmod(0o600)
    print(f"已用 Chrome 登录态刷新 {_path}（{len(_cookie)} 字符，未回显）")
    print("别忘了把 config.json 全文更新到仓库 Secret：DAILYCHECKIN_CONFIG")
