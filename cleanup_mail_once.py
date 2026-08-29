import json, time
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
import requests
from core.cf_temp_mail_client import _request
from config import image_api as image_cfg
from core import db

def local_success_emails():
    out = set()
    rows = db.list_accounts()
    if isinstance(rows, dict):
        rows = rows.get("items", [])
    for row in rows or []:
        email = str(row.get("email") or "").strip().lower()
        if email and "@" in email:
            out.add(email)
    return out

def image_emails():
    r = requests.get(
        image_cfg.IMAGE_API_BASE.rstrip("/") + "/api/accounts",
        headers={"Authorization": "Bearer " + str(image_cfg.IMAGE_API_AUTH_KEY or "").strip(), "Accept": "application/json"},
        timeout=float(image_cfg.IMAGE_API_TIMEOUT or 20),
    )
    r.raise_for_status()
    return {
        str(item.get("email") or "").strip().lower()
        for item in (r.json().get("items") or [])
        if isinstance(item, dict) and "@" in str(item.get("email") or "")
    }

def mail_addresses():
    rows, offset = [], 0
    while True:
        page = _request("GET", f"/admin/address?limit=100&offset={offset}").get("results") or []
        rows.extend(
            {"id": item.get("id"), "email": str(item.get("name") or "").strip().lower()}
            for item in page
            if isinstance(item, dict) and "@" in str(item.get("name") or "")
        )
        if len(page) < 100:
            break
        offset += len(page)
    return rows

local = local_success_emails()
image = image_emails()
mail = mail_addresses()
preserve = local | image
candidates = sorted({item["email"] for item in mail if item["email"] not in preserve})
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = Path(f"mail-delete-candidates-{stamp}.json")
backup.write_text(json.dumps({
    "created_at": stamp,
    "mail_address_count": len({item["email"] for item in mail}),
    "local_success_count": len(local),
    "image_account_count": len(image),
    "preserved_count": len(set(item["email"] for item in mail) & preserve),
    "candidates": candidates,
}, ensure_ascii=False, indent=2), encoding="utf-8")

ok, failed = 0, []
for idx, email in enumerate(candidates, 1):
    try:
        _request("DELETE", "/admin/delete_address/" + quote(email, safe=""))
        ok += 1
    except Exception as exc:
        failed.append({"email": email, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    if idx % 25 == 0:
        print(f"progress={idx}/{len(candidates)} ok={ok} failed={len(failed)}", flush=True)
    time.sleep(0.08)
print(json.dumps({
    "backup": str(backup),
    "mail_addresses": len({item["email"] for item in mail}),
    "local_success": len(local),
    "image_accounts": len(image),
    "preserved": len(set(item["email"] for item in mail) & preserve),
    "candidates": len(candidates),
    "deleted": ok,
    "failed": failed,
}, ensure_ascii=False), flush=True)
