"""Instagram post pipeline (images + caption; publishing comes once the Meta app is connected).

  python post.py queue                 add pushed products to the Posts tab (in Products-tab order)
  python post.py generate [N]          make carousels for the next N QUEUED rows (default: posts_per_day)
  python post.py one <meesho_id>       (re)generate one product's carousel, any status
  python post.py plan <meesho_id>      print the prompts that would be used, without generating

Kaggle (free GPU, used by the bot every night):
  python post.py kaggle-run [N]        plan the next N QUEUED posts and start the Kaggle GPU job
  python post.py kaggle-status         RUNNING / COMPLETE / ERROR
  python post.py kaggle-fetch          download the images and mark the posts GENERATED
"""
import json
import logging
import sys

from pipeline.config import setup_logging
from pipeline import kaggle_posts
from pipeline.posts import (PostSheet, QuotaExceeded, audience, caption, make_post, plan, post_images,
                            product_link, styles)
from pipeline.sheet import Sheet

log = logging.getLogger("post")


def queue(products: Sheet, posts: PostSheet) -> int:
    have = {str(r["meesho_id"]) for r in posts.all()}
    skip = [k.lower() for k in styles().get("skip_keywords") or []]
    rows = []
    for row in products.all():
        mid = str(row["meesho_id"])
        if row["status"] != "PUSHED" or mid in have:
            continue
        title = str(row.get("shopify_title", ""))
        rows.append(dict(meesho_id=mid, title=title, audience=audience(mid, row), price=row.get("my_price", ""),
                         post_status="SKIP" if any(k in title.lower() for k in skip) else "QUEUED"))
    posts.append(rows)
    return len(rows)


def generate_one(mid: str, products: Sheet, posts: PostSheet) -> dict:
    row = products.get(mid) or {}
    aud = audience(mid, row)
    posts.upsert(mid, post_status="GENERATING", error="")
    try:
        shots = make_post(mid, aud, posts.recent_scenes())
    except QuotaExceeded:
        posts.upsert(mid, post_status="QUEUED", error="HF GPU quota used up for today")
        raise
    except Exception as e:
        log.exception("post %s failed", mid)
        posts.upsert(mid, post_status="ERROR", error=str(e)[:300])
        return {}
    link = product_link(row)
    text = caption(mid, aud, row.get("my_price", ""), link)
    posts.upsert(mid, post_status="GENERATED", audience=aud, link=link,
                 images=", ".join(p.name for p in post_images(mid)),
                 shots=" | ".join(f"{s['scene']}: {s['face'][:40]}" for s in shots), caption=text)
    return {"mid": mid, "shots": shots, "caption": text}


def main():
    setup_logging("post")
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else "help"), sys.argv[2:]
    if cmd == "plan":
        mid = args[0]
        aud = audience(mid, Sheet().get(mid))
        print("audience:", aud)
        for s in plan(mid, aud, []):
            print(f"\n[{s['name']}] {s['scene']}\n{s['prompt']}")
        return
    products, posts = Sheet(), None
    posts = PostSheet(products.ws.spreadsheet)
    if cmd == "queue":
        print(f"queued {queue(products, posts)} products")
    elif cmd == "generate":
        n = int(args[0]) if args else styles()["posts_per_day"]
        for r in posts.next("QUEUED", n):
            try:
                out = generate_one(str(r["meesho_id"]), products, posts)
                print("OK" if out else "FAILED", r["meesho_id"])
            except QuotaExceeded:
                print("Hugging Face GPU quota used up for today; the rest stay QUEUED")
                break
    elif cmd == "kaggle-run":
        if (kaggle_posts.current_batch() or {}).get("pushed"):
            print("A batch is already running/unfetched: use kaggle-status / kaggle-fetch first")
            return
        batch = kaggle_posts.current_batch()
        if not batch:  # else: re-push a batch that was prepared but failed to upload
            n = int(args[0]) if args else styles()["posts_per_day"]
            batch = kaggle_posts.prepare(posts.next("QUEUED", n), posts.recent_scenes(), products)
            for mid in batch["posts"]:
                posts.upsert(mid, post_status="GENERATING", error="")
        print(kaggle_posts.push())
        print(f"started Kaggle job for {len(batch['posts'])} posts")
    elif cmd == "kaggle-status":
        print(kaggle_posts.status())
    elif cmd == "kaggle-fetch":
        print(json.dumps(kaggle_posts.fetch(posts, products), indent=1))
    elif cmd == "one":
        try:
            out = generate_one(args[0], products, posts)
        except QuotaExceeded:
            print("Hugging Face GPU quota used up for today; try again tomorrow")
            return
        print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
