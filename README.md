# Meesho → Shopify pipeline (The Noor Naksh)

```
Telegram: share Meesho link
   → scrape (real Chrome, minimized)          sheet: PROCESSING
   → Gemini: rewrite title/description, 3 AI images
   → price = cost × 1.40 + 70, rounded up to …9  sheet: READY
   → preview in Telegram  [✅ Approve] [🔄 New images] [❌ Reject]
   → Approve → productSet on Shopify (Draft)  sheet: PUSHED
   → daily 03:00: re-check Meesho price/stock → update Shopify, notify you
```

Everything runs from `bot.py` on this PC. The Google Sheet is the record of every product;
you can also edit `status` / `price_override` there directly.

## One-time setup

### 1. Shopify app (≈5 min)
1. Shopify admin → **Settings → Apps → Develop apps** → it opens the **Dev Dashboard** → **Create app**.
2. In the app's **Versions** / configuration, set Admin API scopes:
   `write_products, read_products, write_inventory, read_inventory, read_locations, write_files`
   then **Release** the version.
3. **Install** the app on *thenoornaksh.myshopify.com*.
4. **App settings → Credentials**: copy **Client ID** and **Client secret** into `.env`.

### 2. Google Sheet (≈10 min)
1. https://console.cloud.google.com → create a project → **APIs & Services → Enable APIs** → enable **Google Sheets API**.
2. **IAM & Admin → Service accounts → Create**. Open it → **Keys → Add key → JSON**.
   Save the file in this folder as `google-service-account.json`.
3. Create a blank Google Sheet. **Share** it (Editor) with the service account's email
   (`…@….iam.gserviceaccount.com`, it's inside the JSON file).
4. Put the sheet ID (from the URL `docs.google.com/spreadsheets/d/<ID>/edit`) in `.env` as `GOOGLE_SHEET_ID`.
   The header row and status dropdown are created automatically.

### 3. Gemini
https://aistudio.google.com/apikey → create key → `GEMINI_API_KEY` in `.env`.
Image generation needs billing enabled on the key's Google Cloud project.

### 4. Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Start the bot once (`start_bot.bat`), send it `/start`; it replies with your user ID.
   Put it in `TELEGRAM_ALLOWED_USER_IDS` (comma-separate for more people) and restart.

### 5. Verify
```
.venv\Scripts\python run.py check
```

### 6. Run
- Now: double-click `start_bot.bat` (a minimized Chrome window will appear when scraping; leave it).
- Auto-start at login: `powershell -ExecutionPolicy Bypass -File install_autostart.ps1`
- The PC must be on and awake. Set **Power → Sleep: Never** while plugged in.

## Daily use

| Do this | How |
|---|---|
| Add product | Share the Meesho link to the bot (from the app or browser) |
| Approve / redo images / reject | Buttons under the preview |
| Override price | `/price ia8ll7 699` (or `/price ia8ll7 auto` to go back to the formula) |
| Go live / hide | **🟢 Make Active** / **⏸ Back to Draft** buttons after a push, or `/activate <id>`, `/draft <id>` |
| Collections | **📁 Collections** button (tap to toggle, ➕ to create), or `/collection <id> Kids Wear` |
| Default collections | `shopify.default_collections` in `config.yaml`: every pushed product is added to them |
| See what's waiting | `/pending`, `/show <id>`, `/status` |
| Push / resync now | `/push`, `/resync` |

Products are created as **Draft**. Review in Shopify admin and set Active (or set
`status_on_create: ACTIVE` in `config.yaml` once you trust the output).

CLI equivalents: `run.py add <url>`, `run.py approve <id>`, `run.py push`, `run.py resync`.

## Settings (`config.yaml`)
- `pricing`: margin %, fixed add-on, rounding (`9` → …9, `99` → …99), compare-at (Meesho MRP).
- `shopify.stock_when_available`: quantity shown on Shopify while Meesho has stock (0 when out).
- `gemini.shots`: the AI image prompts, one image per shot. Edit freely.
- `pipeline.auto_approve`, `push_on_approve`, `push_every_minutes`, `resync_daily_at`.

## How the sync behaves
- Re-pushing a product updates it in place (matched by Shopify product ID and SKU `MSH-<id>-<size>`).
- After the first push, title/description/tags are only re-sent if the AI copy changes, so
  your edits in Shopify admin are kept. Images are only re-uploaded after **New images**.
- Resync: price change → new price; size out of stock → quantity 0 (shows Sold out);
  removed from Meesho → product set to Draft and sheet status UNAVAILABLE.

## Files
- `data/<id>/product.json`: last scrape · `copy.json`: AI copy · `shopify.json`: Shopify IDs
- `images/<id>/orig/` Meesho photos · `images/<id>/ai/` generated photos
- `logs/bot.log`, `logs/run.log`

## Troubleshooting
- **"Meesho blocked the browser"**: Meesho's bot protection. The scraper retries and resets its
  Chrome profile automatically; if it persists, wait 15–30 min. Don't close the minimized
  Chrome window while a scrape is running.
- **Images look different from the product**: tap 🔄 New images, or tighten the prompts in `config.yaml`.
