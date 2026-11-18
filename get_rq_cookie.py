#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机 Chrome 的 Cookie 库里取出 RQ 的网页会话（RQWEBSESSID）。

原理：Chrome 用 Keychain 里的 "Chrome Safe Storage" 密钥做 AES-128-CBC 加密，
密钥可以本地读取，因此能在不接触密码的情况下拿到已登录的会话 Cookie。
只读取 host 含 rq.run 的记录，其它站点一律不碰。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
KEYCHAIN_SERVICE = "Chrome Safe Storage"

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    sys.exit("缺少依赖：python3 -m pip install cryptography")


def chrome_key() -> bytes:
    pw = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return hashlib.pbkdf2_hmac("sha1", pw.encode(), b"saltysalt", 1003, 16)


def decrypt(ev: bytes, key: bytes) -> str:
    if ev[:3] in (b"v10", b"v11"):
        ev = ev[3:]
    d = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    out = d.update(ev) + d.finalize()
    pad = out[-1]
    if 0 < pad <= 16:
        out = out[:-pad]
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return out[32:].decode("utf-8", "ignore")  # 新版带 32 字节 host hash


def profiles() -> list[Path]:
    out = []
    if not CHROME_ROOT.exists():
        return out
    for p in CHROME_ROOT.iterdir():
        if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile")):
            if (p / "Cookies").exists():
                out.append(p)
    return out


def read_rq(profile: Path, key: bytes) -> dict[str, str]:
    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    for suffix in ("", "-wal", "-shm"):
        src = profile / f"Cookies{suffix}"
        if src.exists():
            shutil.copy2(src, str(tmp) + suffix)
    db = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    rows = db.execute(
        "select host_key,name,encrypted_value,value from cookies where host_key like '%rq.run%'"
    ).fetchall()
    db.close()
    out = {}
    for host, name, ev, plain in rows:
        out[name] = plain if plain else (decrypt(ev, key) if ev else "")
    return out


def main() -> int:
    key = chrome_key()
    found = {}
    for p in profiles():
        cks = read_rq(p, key)
        if cks:
            print(f"[{p.name}] 发现 {len(cks)} 条 rq.run cookie: {', '.join(cks)}")
            found.update(cks)
    if not found:
        print("没有找到 rq.run 的 Cookie，请先在 Chrome 登录 www.rq.run")
        return 1

    sid = found.get("RQWEBSESSID")
    if not sid:
        print("找到 rq.run 域名 Cookie，但没有 RQWEBSESSID，登录可能未完成")
        return 1

    cookie = "; ".join(f"{k}={v}" for k, v in found.items())
    cfg_path = HERE / "config.json"
    cfg = {}
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8") or "{}")
    cfg["cookie"] = cookie
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg_path.chmod(0o600)
    print(f"已写入 {cfg_path}（RQWEBSESSID={sid[:8]}...）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
