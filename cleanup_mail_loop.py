import json, time
from pathlib import Path
from datetime import datetime
import requests
from core.cf_temp_mail_client import _request
from config import image_api as image_cfg
from core import db

def local_emails():
    rows = db.list_accounts()
    if isinstance(rows, dict): rows = rows.get("items", [])
    return {str(r.get("email") or "").strip().lower() for r in rows or [] if "@" in str(r.get("email") or "")}

def image_emails():
    r = requests.get(image_cfg.IMAGE_API_BASE.rstrip("/") + "/api/accounts", headers={"Authorization":"Bearer " + str(image_cfg.IMAGE_API_AUTH_KEY or "").strip(), "Accept":"application/json"}, timeout=30)
    r.raise_for_status()
    return {str(x.get("email") or "").strip().lower() for x in (r.json().get("items") or []) if isinstance(x, dict) and "@" in str(x.get("email") or "")}

def all_addresses():
    out, offset = [], 0
    while True:
        page = _request("GET", f"/admin/address?limit=100&offset={offset}").get("results") or []
        out.extend(x for x in page if isinstance(x, dict) and "@" in str(x.get("name") or ""))
        if len(page) < 100: return out
        offset += len(page)

preserve = local_emails() | image_emails()
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
log = {"created_at": stamp, "preserve_count": len(preserve), "rounds": [], "deleted_ids": [], "failed": []}
while True:
    rows = all_addresses()
    candidates = [x for x in rows if str(x.get("name") or "").strip().lower() not in preserve]
    log["rounds"].append({"seen": len(rows), "candidates": len(candidates)})
    if not candidates: break
    for item in candidates:
        try:
            _request("DELETE", "/admin/delete_address/" + str(item.get("id")))
            log["deleted_ids"].append(item.get("id"))
        except Exception as exc:
            log["failed"].append({"id": item.get("id"), "email": item.get("name"), "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        time.sleep(0.05)
Path(f"mail-delete-final-{stamp}.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"preserve":len(preserve), "rounds":log["rounds"], "deleted":len(log["deleted_ids"]), "failed":len(log["failed"]), "backup":f"mail-delete-final-{stamp}.json"}, ensure_ascii=False), flush=True)
