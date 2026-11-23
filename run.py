"""Command-line entry point (the Telegram bot does all of this too).

  python run.py check                 verify Sheets, Shopify, Gemini and Chrome setup
  python run.py add <meesho-url>...   scrape + AI images + price -> sheet (status READY)
  python run.py approve <id>...       mark READY products as APPROVED
  python run.py push [<id>...]        push given ids, or every APPROVED row
  python run.py resync                re-check Meesho price/stock for all PUSHED rows
"""
import argparse
import json
import sys

from pipeline.config import setup_logging


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    setup_logging("run")
    parser = argparse.ArgumentParser(description="Meesho -> Shopify pipeline")
    parser.add_argument("command", choices=["check", "add", "approve", "push", "resync"])
    parser.add_argument("args", nargs="*")
    a = parser.parse_args()

    if a.command == "check":
        return check()

    from pipeline.workflow import Pipeline
    p = Pipeline()
    try:
        if a.command == "add":
            for url in a.args:
                print(json.dumps(p.process(url, "cli"), indent=1, ensure_ascii=False))
        elif a.command == "approve":
            for mid in a.args:
                p.set_status(mid, "APPROVED")
                print(f"Approved {mid}")
        elif a.command == "push":
            if a.args:
                for mid in a.args:
                    print(json.dumps(p.push(mid), indent=1))
            else:
                done, failed = p.push_approved()
                for d in done:
                    print("OK  ", d["meesho_id"], d["admin_url"])
                for f in failed:
                    print("FAIL", f)
                if failed:
                    sys.exit(1)
        elif a.command == "resync":
            for line in p.resync() or ["No changes."]:
                print(line)
    finally:
        p.close()


def check():
    ok = True

    def step(name, fn):
        nonlocal ok
        try:
            print(f"[ OK ] {name}: {fn()}")
        except Exception as e:
            ok = False
            print(f"[FAIL] {name}: {e}")

    def sheets():
        from pipeline.sheet import Sheet
        s = Sheet()
        return f"'{s.ws.spreadsheet.title}' / {s.ws.title}, {len(s.all())} rows"

    def shopify():
        from pipeline.shopify import Shopify
        s = Shopify()
        return f"{s.shop_name()}, location {s.location_id()}"

    def gemini():
        from pipeline.ai import GeminiAI
        from pipeline.config import CFG
        g = GeminiAI()
        r = g.client.models.generate_content(model=CFG["gemini"]["text_model"], contents="Reply with: ok")
        return f"{CFG['gemini']['text_model']} replied '{r.text.strip()[:20]}'"

    def telegram():
        import requests
        from pipeline.config import env
        r = requests.get(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/getMe", timeout=20).json()
        if not r.get("ok"):
            raise RuntimeError(r)
        allowed = env("TELEGRAM_ALLOWED_USER_IDS", required=False)
        return f"@{r['result']['username']}, allowed users: {allowed or 'NONE (send /start to get your ID)'}"

    def chrome():
        from pathlib import Path
        from pipeline.config import CFG
        path = Path(CFG["scraper"]["chrome_path"])
        if not path.exists():
            raise RuntimeError(f"Chrome not found at {path}")
        return str(path)

    step("Google Sheet", sheets)
    step("Shopify", shopify)
    step("Gemini", gemini)
    step("Telegram", telegram)
    step("Chrome", chrome)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
