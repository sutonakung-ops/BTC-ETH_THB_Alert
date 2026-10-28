"""
ตู้เซฟข้อมูลบอท (vault) — เข้ารหัสไฟล์ข้อมูลบัญชีทั้งหมดก่อนขึ้น GitHub

repo นี้เป็น Public (เพื่อใช้ GitHub Actions ได้ฟรี) แต่ไฟล์ยอดเงิน ประวัติเทรด ค่าตั้งกลยุทธ์
และรายงานทั้งหมดจะถูกเก็บใน repo เป็นไฟล์ .enc ที่เข้ารหัสแล้วเท่านั้น
ไฟล์ตัวจริง (ไม่เข้ารหัส) มีอยู่แค่ในเครื่องที่รันบอทระหว่างทำงาน และ .gitignore กันไม่ให้หลุดขึ้นไป

รหัสผ่านอยู่ใน GitHub Secrets ชื่อ DATA_PASSWORD (ตัวเดียวกับที่ใช้ปลดล็อกหน้า dashboard)

วิธีใช้ (ใน workflow):
    python tools/vault.py unlock          # หลัง checkout: ถอดรหัส .enc -> ไฟล์ตัวจริง
    python tools/vault.py lock            # ก่อน commit: เข้ารหัสไฟล์ตัวจริง -> .enc และ stage ให้ git
    python tools/vault.py run --log NAME -- python script.py
                                          # รันสคริปต์โดยเก็บ output ลงไฟล์ log ที่เข้ารหัส
                                          # (log ของ Actions ใน repo Public ใครก็เปิดดูได้)

ถ้ายังไม่ได้ตั้ง DATA_PASSWORD และยังไม่มีไฟล์ .enc เลย -> ทำงานแบบเดิมทุกอย่าง (ไม่เข้ารหัส)
ถ้ามีไฟล์ .enc แล้วแต่ไม่มีรหัส/รหัสผิด -> หยุดทันที (กันบอทเริ่มใหม่จากสถานะว่างแล้วลืมไม้ที่ถืออยู่)

รูปแบบไฟล์ .enc:  b"SSV1" + nonce 12 ไบต์ + AES-256-GCM(gzip(ข้อมูล))  โดยใช้ path ของไฟล์เป็น AAD
กุญแจ: PBKDF2-HMAC-SHA256 จากรหัสผ่าน + salt ใน vault.json (index.html ถอดรหัสแบบเดียวกันด้วย WebCrypto)
"""
import base64
import gzip
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULT_FILE = os.path.join(ROOT, "vault.json")
MARKER = os.path.join(ROOT, ".vault_unlocked")
MAGIC = b"SSV1"
PASSWORD_ENV = "DATA_PASSWORD"

# ไฟล์/โฟลเดอร์ที่ถือว่าเป็นข้อมูลบัญชี ต้องเข้ารหัสทั้งหมด
PROTECTED_ROOT_FILES = ["latest.json", "backtest_result.json"]
PROTECTED_DIRS = ["state", "analysis", "reports"]


def _is_public(rel):
    """ไฟล์ในโฟลเดอร์ที่ปกป้อง แต่เปิดเผยได้ (ไม่ใช่ข้อมูลบัญชี)"""
    name = os.path.basename(rel)
    if name == ".gitkeep" or name.endswith(".tmp"):
        return True
    # ราคาย้อนหลังจาก Bitkub (ข้อมูลตลาดสาธารณะ ไฟล์ใหญ่) ไม่ต้องเข้ารหัส
    if rel.startswith("analysis/") and rel.count("/") == 1 and name.startswith("history_") and name.endswith(".json"):
        return True
    return False


def _rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def _walk(suffix_enc):
    """คืนรายการ path แบบ relative ของไฟล์ตัวจริง (suffix_enc=False) หรือไฟล์ .enc (True)"""
    out = []
    for f in PROTECTED_ROOT_FILES:
        p = f + ".enc" if suffix_enc else f
        if os.path.isfile(os.path.join(ROOT, p)):
            out.append(p)
    for d in PROTECTED_DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                rel = _rel(os.path.join(dirpath, fn))
                if rel.endswith(".enc") != suffix_enc:
                    continue
                plain = rel[:-4] if suffix_enc else rel
                if _is_public(plain):
                    continue
                out.append(rel)
    return sorted(out)


# ---------------------------------------------------------------- กุญแจ
_key_cache = {}


def _vault_params():
    with open(VAULT_FILE, encoding="utf-8") as f:
        v = json.load(f)
    return base64.b64decode(v["salt"]), int(v["iterations"])


def _password():
    return os.environ.get(PASSWORD_ENV, "")


def _key():
    pw = _password()
    if not pw:
        return None
    if pw not in _key_cache:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        salt, iterations = _vault_params()
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
        _key_cache.clear()
        _key_cache[pw] = kdf.derive(pw.encode("utf-8"))
    return _key_cache[pw]


def encrypt_bytes(data, rel_path, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    packed = gzip.compress(data, mtime=0)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, packed, rel_path.encode("utf-8"))


def decrypt_bytes(blob, rel_path, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIC):
        raise ValueError(f"{rel_path}: ไม่ใช่ไฟล์ vault")
    nonce, ct = blob[4:16], blob[16:]
    return gzip.decompress(AESGCM(key).decrypt(nonce, ct, rel_path.encode("utf-8")))


class VaultError(RuntimeError):
    pass


# ---------------------------------------------------------------- unlock / lock
def unlock(quiet=False):
    """ถอดรหัสไฟล์ .enc ทั้งหมดเป็นไฟล์ตัวจริง (ทับของเดิม เพราะ .enc คือข้อมูลล่าสุดจาก repo)"""
    encs = _walk(suffix_enc=True)
    key = _key()
    if not encs:
        if not quiet:
            print("vault: ยังไม่มีไฟล์เข้ารหัส — ใช้ไฟล์ตามเดิม" + ("" if key else " (ยังไม่ได้ตั้ง DATA_PASSWORD)"))
        if key:
            _touch_marker()
        return 0
    if not key:
        raise VaultError("มีไฟล์ข้อมูลที่เข้ารหัสอยู่ แต่ไม่ได้ตั้ง DATA_PASSWORD ใน GitHub Secrets — หยุดเพื่อกันบอทลืมสถานะ")
    from cryptography.exceptions import InvalidTag
    count = 0
    for rel in encs:
        plain_rel = rel[:-4]
        with open(os.path.join(ROOT, rel), "rb") as f:
            blob = f.read()
        try:
            data = decrypt_bytes(blob, plain_rel, key)
        except InvalidTag:
            raise VaultError(f"ถอดรหัส {rel} ไม่ได้ — DATA_PASSWORD ไม่ตรงกับรหัสที่ใช้เข้ารหัส")
        dest = os.path.join(ROOT, plain_rel)
        os.makedirs(os.path.dirname(dest) or ROOT, exist_ok=True)
        tmp = dest + ".vaulttmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        count += 1
    _touch_marker()
    if not quiet:
        print(f"vault: ถอดรหัส {count} ไฟล์เรียบร้อย")
    return count


def _touch_marker():
    with open(MARKER, "w") as f:
        f.write(str(time.time()))


def _git(*args, check=False):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def lock(quiet=False):
    """เข้ารหัสไฟล์ตัวจริงเป็น .enc แล้ว stage ให้ git (ไฟล์ตัวจริงถูกถอดออกจาก git แต่ยังอยู่ในเครื่อง)"""
    plains = _walk(suffix_enc=False)
    key = _key()
    if not key:
        if _walk(suffix_enc=True):
            raise VaultError("มีไฟล์ข้อมูลที่เข้ารหัสอยู่ แต่ไม่ได้ตั้ง DATA_PASSWORD — ไม่บันทึก")
        # โหมดเดิม (ยังไม่ได้ตั้งรหัส): stage ไฟล์ตัวจริงตามเดิม (-f เพราะ .gitignore กันไว้แล้ว)
        stage_all()
        if plains:
            _git("add", "-f", "--", *plains)
        if not quiet:
            print("vault: ยังไม่ได้ตั้ง DATA_PASSWORD — บันทึกแบบไม่เข้ารหัสไปก่อน")
        return 0

    written = 0
    for rel in plains:
        with open(os.path.join(ROOT, rel), "rb") as f:
            data = f.read()
        enc_path = os.path.join(ROOT, rel + ".enc")
        if os.path.exists(enc_path):
            try:
                with open(enc_path, "rb") as f:
                    if decrypt_bytes(f.read(), rel, key) == data:
                        continue  # ข้อมูลไม่เปลี่ยน ไม่ต้องเข้ารหัสใหม่ (กัน commit/conflict ไม่จำเป็น)
            except Exception:
                pass
        tmp = enc_path + ".vaulttmp"
        with open(tmp, "wb") as f:
            f.write(encrypt_bytes(data, rel, key))
        os.replace(tmp, enc_path)
        written += 1

    # ไฟล์ที่บอทลบทิ้งไปแล้ว -> ลบ .enc ด้วย (ทำเฉพาะเมื่อ unlock ในรอบนี้สำเร็จ ไม่งั้นจะเผลอลบข้อมูลทั้งหมด)
    removed = 0
    if os.path.exists(MARKER):
        plain_set = set(plains)
        for rel in _walk(suffix_enc=True):
            if rel[:-4] not in plain_set:
                os.remove(os.path.join(ROOT, rel))
                removed += 1

    # ถอดไฟล์ตัวจริงออกจาก git (ถ้าเคยถูก track) แล้ว stage ไฟล์ .enc ทั้งหมด
    tracked = _git("ls-files", "--", *PROTECTED_ROOT_FILES, *PROTECTED_DIRS).stdout.split()
    to_untrack = [p for p in tracked if not p.endswith(".enc") and not _is_public(p)]
    if to_untrack:
        _git("rm", "--cached", "-q", "--", *to_untrack)
    stage_all()
    if not quiet:
        print(f"vault: เข้ารหัสใหม่ {written} ไฟล์, ลบ {removed} ไฟล์, ถอดไฟล์ไม่เข้ารหัสออกจาก git {len(to_untrack)} ไฟล์")
    return written


def stage_all():
    """stage ทุกอย่างในโฟลเดอร์ข้อมูล (ไฟล์ .enc ใหม่/แก้/ลบ + ไฟล์สาธารณะ) — ไฟล์ตัวจริงถูก .gitignore กันไว้"""
    paths = [p for p in PROTECTED_DIRS if os.path.isdir(os.path.join(ROOT, p))]
    tracked = set(_git("ls-files").stdout.split())
    for p in PROTECTED_ROOT_FILES:
        enc = p + ".enc"
        if os.path.exists(os.path.join(ROOT, enc)) or enc in tracked:
            paths.append(enc)
    if paths:
        _git("add", "-A", "--", *paths)
    _git("add", "--", *[f for f in ("vault.json", ".gitignore", ".gitattributes") if os.path.exists(os.path.join(ROOT, f))])


# ---------------------------------------------------------------- run (ซ่อน output จาก log สาธารณะ)
LOG_DIR = "state/logs"
LOG_MAX_LINES = 3000


def run(log_name, cmd):
    """รันคำสั่งแล้วเก็บ output ลง state/logs/<log_name>.log (ไฟล์นี้ถูกเข้ารหัสตอน lock)
    ในหน้า log ของ GitHub Actions จะเห็นแค่ว่าสำเร็จหรือไม่"""
    log_path = os.path.join(ROOT, LOG_DIR, f"{log_name}.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    hide = bool(_key())
    started = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    label = " ".join(os.path.basename(c) if i == 1 else c for i, c in enumerate(cmd))
    print(f"▶ {label}", flush=True)
    if not hide:
        # ยังไม่ได้ตั้งรหัส -> แสดง output ตามเดิม
        return subprocess.run(cmd, cwd=ROOT).returncode
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n===== {started} :: {label} =====\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env)
        log.write(f"===== exit {proc.returncode} =====\n")
    _trim(log_path)
    print(("✅ เสร็จ" if proc.returncode == 0 else f"❌ ผิดพลาด (exit {proc.returncode})")
          + f" — รายละเอียดเก็บใน {LOG_DIR}/{log_name}.log (เข้ารหัส ดูได้จาก dashboard)", flush=True)
    return proc.returncode


def _trim(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) > LOG_MAX_LINES:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-LOG_MAX_LINES:])
    except OSError:
        pass


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    try:
        if argv[0] == "unlock":
            unlock()
            return 0
        if argv[0] == "lock":
            lock()
            return 0
        if argv[0] == "run":
            log_name = "run"
            rest = argv[1:]
            if rest[:1] == ["--log"]:
                log_name, rest = rest[1], rest[2:]
            if rest[:1] == ["--"]:
                rest = rest[1:]
            return run(log_name, rest)
    except VaultError as e:
        print(f"::error::vault: {e}")
        return 1
    print(f"ไม่รู้จักคำสั่ง {argv[0]}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
