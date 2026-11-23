"""Re-push product photos after an image-pipeline change (e.g. Oct 2026: original photos, then the
cleaned versions with the supplier code removed and square photos padded). Live (ACTIVE) first.
push() only re-uploads images when they changed, so it is safe to re-run."""
import time

from pipeline.config import setup_logging
from pipeline.workflow import Pipeline

setup_logging("repush")
p = Pipeline()
rows = [r for r in p.sheet.all() if r["status"] == "PUSHED"]
rows.sort(key=lambda r: r.get("shopify_status") != "ACTIVE")
print(f"{len(rows)} products to re-push", flush=True)
ok, bad = 0, []
for i, r in enumerate(rows, 1):
    mid = str(r["meesho_id"])
    try:
        p.push(mid)
        ok += 1
        print(f"[{i}/{len(rows)}] {mid} OK", flush=True)
    except Exception as e:
        bad.append(mid)
        print(f"[{i}/{len(rows)}] {mid} FAILED {e}", flush=True)
    time.sleep(2)  # stay under the Sheets write quota
print(f"done: {ok} pushed, failed: {bad}", flush=True)
p.close()
