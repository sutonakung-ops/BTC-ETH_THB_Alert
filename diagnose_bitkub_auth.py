"""
สคริปต์ชั่วคราวสำหรับวินิจฉัย Bitkub error code 6 "Missing or invalid signature"
ที่เกิดกับทุกคำสั่งซื้อ/ขายมาตลอด แม้จะยืนยันแล้วว่าวิธี sign ตรงกับตัวอย่างทางการ 100%

ทดสอบ GET /api/v4/wallet/balances ซึ่งเป็น private endpoint ที่ body ว่างเปล่า (ไม่มีผลจาก
symbol/amount ใดๆ) เพื่อแยกให้ชัดว่าปัญหาอยู่ที่ชั้น "API key/secret setup" เลยหรือไม่
(ถ้า GET endpoint นี้ยัง error 6 ด้วย แปลว่าไม่เกี่ยวกับ symbol หรือ body เลย)

ไม่พิมพ์ api_secret เต็มๆ ออกมา (ความปลอดภัย) — พิมพ์แค่ความยาว + ตัวอักษรต้น/ท้ายเพื่อเช็ค
ว่า copy-paste มาครบไม่มีช่องว่าง/ตัวอักษรแปลกปลอมติดมา
"""

import json
import os

from bitkub_trade import BitkubAPIError, get_balances

BITKUB_API_KEY = os.environ.get("BITKUB_API_KEY", "").strip()
BITKUB_API_SECRET = os.environ.get("BITKUB_API_SECRET", "").strip()


def mask(s):
    if not s:
        return {"length": 0, "preview": None}
    if len(s) <= 8:
        return {"length": len(s), "preview": "*" * len(s)}
    return {"length": len(s), "preview": f"{s[:4]}...{s[-4:]}"}


def main():
    diag = {
        "api_key_info": mask(BITKUB_API_KEY),
        "api_secret_info": mask(BITKUB_API_SECRET),
    }

    if not BITKUB_API_KEY or not BITKUB_API_SECRET:
        diag["result"] = "ไม่ได้ตั้งค่า BITKUB_API_KEY / BITKUB_API_SECRET ใน environment"
    else:
        try:
            resp = get_balances(BITKUB_API_KEY, BITKUB_API_SECRET)
            diag["raw_response"] = resp
        except BitkubAPIError as e:
            diag["raw_response"] = {"exception": str(e)}

    os.makedirs("state", exist_ok=True)
    with open("state/auth_diag.json", "w", encoding="utf-8") as f:
        json.dump(diag, f, ensure_ascii=False, indent=2)

    print(json.dumps(diag, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
