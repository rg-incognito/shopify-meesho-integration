"""Run Instagram post image generation as a Kaggle GPU batch (free T4 x2, ~7 min/image with 2 GPUs).

prepare() plans the next posts and writes kaggle/post_inputs (jobs.json + reference photos),
push() uploads the dataset and starts kaggle/post_job, status() polls it, fetch() downloads the
images and finishes the posts (watermark, caption, sheet -> GENERATED).
"""
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

from .config import DATA_DIR, IMAGES_DIR, ROOT
from .posts import audience, caption, plan, post_dir, product_link, reference_image, styles, watermark

log = logging.getLogger(__name__)

KAGGLE = ROOT / ".venv" / "Scripts" / ("kaggle.exe" if os.name == "nt" else "kaggle")
INPUTS = ROOT / "kaggle" / "post_inputs"
JOB_DIR = ROOT / "kaggle" / "post_job"
KERNEL = "rohitgande/noornaksh-post-images"
DATASET = "rohitgande/noornaksh-post-inputs"
BATCH_FILE = DATA_DIR / "post_batch.json"


def kaggle(*args, timeout=600) -> str:
    r = subprocess.run([str(KAGGLE), *args], capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    out = (r.stdout + r.stderr).strip()
    if r.returncode:
        raise RuntimeError(f"kaggle {' '.join(args[:2])} failed: {out[-500:]}")
    return out


def current_batch() -> dict | None:
    return json.loads(BATCH_FILE.read_text(encoding="utf-8")) if BATCH_FILE.exists() else None


def prepare(rows: list[dict], recent_scenes: list[str], products) -> dict:
    """Plan the posts for `rows` (Posts-tab rows) and write the Kaggle input folder."""
    st = styles()
    for f in INPUTS.glob("*"):
        if f.name != "dataset-metadata.json":
            f.unlink()
    w, h = st.get("image_size", [832, 1104])
    batch = {"created": datetime.now().isoformat(timespec="seconds"), "posts": {}}
    jobs, used = [], list(recent_scenes)
    for r in rows:
        mid = str(r["meesho_id"])
        aud = audience(mid, products.get(mid))
        shots = plan(mid, aud, used, st)
        used += [s["scene"] for s in shots]
        tmp = reference_image(mid, IMAGES_DIR / mid / "post_ref")
        shutil.copy(tmp, INPUTS / f"{mid}.jpg")
        for i, s in enumerate(shots, 1):
            s["id"] = f"{mid}__{i:02d}-{s['name']}"
            jobs.append({"id": s["id"], "ref": f"{mid}.jpg", "prompt": s["prompt"], "seed": s["seed"],
                         "width": w, "height": h})
        batch["posts"][mid] = {"audience": aud, "shots": shots}
    k = st.get("kaggle", {})
    (INPUTS / "jobs.json").write_text(json.dumps({
        "jobs": jobs, "steps": k.get("steps", 8), "max_minutes": k.get("max_minutes", 600),
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    BATCH_FILE.write_text(json.dumps(batch, indent=1, ensure_ascii=False), encoding="utf-8")
    return batch


def push() -> str:
    """Upload the inputs as a new dataset version and start the GPU job."""
    try:
        exists = "ready" in kaggle("datasets", "status", DATASET, timeout=120).lower()
    except RuntimeError:
        exists = False
    if exists:
        kaggle("datasets", "version", "-p", str(INPUTS), "-m", f"batch {datetime.now():%Y-%m-%d %H:%M}")
    else:
        kaggle("datasets", "create", "-p", str(INPUTS))
    for _ in range(60):
        if "ready" in kaggle("datasets", "status", DATASET).lower():
            break
        time.sleep(10)
    out = kaggle("kernels", "push", "-p", str(JOB_DIR), "--accelerator", "NvidiaTeslaT4")
    batch = current_batch() or {}
    batch["pushed"] = datetime.now().isoformat(timespec="seconds")
    BATCH_FILE.write_text(json.dumps(batch, indent=1, ensure_ascii=False), encoding="utf-8")
    return out


def status() -> str:
    """RUNNING / QUEUED / COMPLETE / ERROR / CANCEL..."""
    out = kaggle("kernels", "status", KERNEL, timeout=120)
    m = re.search(r"KernelWorkerStatus\.(\w+)", out) or re.search(r'status "(\w+)"', out)
    return m.group(1).upper() if m else out


def fetch(posts_sheet, products) -> dict:
    """Download the job output and finish every post of the current batch."""
    batch = current_batch()
    if not batch:
        return {"ok": [], "failed": []}
    dest = IMAGES_DIR / "_kaggle_posts" / datetime.now().strftime("%Y%m%d-%H%M")
    dest.mkdir(parents=True, exist_ok=True)
    kaggle("kernels", "output", KERNEL, "-p", str(dest), "-o", timeout=900)
    found = {p.stem: p for p in dest.rglob("*.jpg")}
    st, ok, failed = styles(), [], []
    for mid, info in batch["posts"].items():
        shots = info["shots"]
        missing = [s["id"] for s in shots if s["id"] not in found]
        if missing:
            failed.append(mid)
            posts_sheet.upsert(mid, post_status="QUEUED", error=f"Kaggle made {len(shots) - len(missing)}/"
                                                                 f"{len(shots)} images; will retry")
            continue
        out = post_dir(mid)
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        for i, s in enumerate(shots, 1):
            f = out / f"{i:02d}-{s['name']}.jpg"
            shutil.copy(found[s["id"]], f)
            watermark(f, st)
            s["file"] = f.name
        (out / "plan.json").write_text(json.dumps(shots, indent=1, ensure_ascii=False), encoding="utf-8")
        row = products.get(mid) or {}
        link = product_link(row)
        posts_sheet.upsert(mid, post_status="GENERATED", audience=info["audience"], link=link, error="",
                           images=", ".join(f"{i:02d}-{s['name']}.jpg" for i, s in enumerate(shots, 1)),
                           shots=" | ".join(f"{s['scene']}: {s['face'][:40]}" for s in shots),
                           caption=caption(mid, info["audience"], row.get("my_price", ""), link, st))
        ok.append(mid)
    batch["fetched"] = datetime.now().isoformat(timespec="seconds")
    BATCH_FILE.rename(BATCH_FILE.with_name(f"post_batch_{datetime.now():%Y%m%d-%H%M}.json"))
    log_file = next(dest.rglob("log.json"), None)
    return {"ok": ok, "failed": failed, "log": str(log_file) if log_file else ""}
