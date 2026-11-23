# The Noor Naksh — Meesho → Shopify Pipeline: Build Log & Remaining Work

As of 9 Oct 2026. For setup steps see `README.md`.

## Summary

The pipeline works end to end: a Meesho link shared on Telegram becomes a priced, AI-written Shopify
product in about a minute, with the Google Sheet as the record. The store has **98 Navratri products
as Drafts**, a clothing-brand theme waiting in preview, and a Navratri-first homepage. Facebook and
Instagram are not linked yet because Meta is temporarily blocking it.

| Area | Status |
| --- | --- |
| Link → Shopify pipeline (scrape, AI copy, price, push) | Working |
| Telegram bot (review, approve, activate, collections) | Built; currently **stopped** |
| Google Sheet tracker | Working |
| Daily price and stock re-check | Built, runs from the bot at 03:00 |
| Free studio photos (CPU background removal) | Working, default when AI images are off |
| AI images (Gemini) | Off until billing is enabled |
| 98 Navratri products | On Shopify as Draft, not yet live |
| Clothing-brand theme | In preview, not yet published |
| Facebook and Instagram | Blocked by Meta, retry in 1–2 days |

## How it works

1. **Intake** — share a Meesho link with the Telegram bot (`bot.py`).
2. **Scrape** — a minimized Chrome reads the product page (`pipeline/scraper.py`): title, price,
   MRP, sizes, stock, photos. Meesho's supplier code is cropped off the bottom of every photo.
3. **Write and photograph** — Gemini writes title, description, tags and SEO (`pipeline/ai.py`);
   studio photos are made on the CPU (`pipeline/studio.py`).
4. **Price** — Meesho price × 1.40 + ₹70, rounded up to end in 9; MRP becomes the crossed-out
   price (`pipeline/pricing.py`).
5. **Review and push** — the bot shows a preview with Approve / New images / Reject / Collections;
   Approve pushes a Draft to Shopify with collections and Online Store publishing
   (`pipeline/workflow.py`, `pipeline/shopify.py`).
6. **Re-sync** — every day at 03:00 the bot re-checks Meesho price and stock and updates Shopify;
   delisted products go to Draft; products you deleted in Shopify stop syncing.

Every product is a row in the Google Sheet (`pipeline/sheet.py`). Settings: `config.yaml`.
Bulk tools: `discover.py` (find candidates on Meesho) and `bulk_add.py` (select / run / restudio).

### Files

| Path | Purpose |
| --- | --- |
| `bot.py` | Telegram bot + scheduled push and daily re-sync |
| `run.py` | Command line: `check`, `add`, `approve`, `push`, `resync` |
| `discover.py` | Searches Meesho and ranks candidates (rating, reviews, price) |
| `bulk_add.py` | `select` a shortlist, `run` it to Shopify, `restudio` existing products |
| `pipeline/` | scraper, ai, studio, pricing, sheet, shopify, workflow |
| `config.yaml` | Margin, rounding, stock, collections, Gemini models, studio look |
| `.env` | Secrets (Shopify, Gemini, Telegram, Google) — never share |
| `meesho-shopify-4793cfd2d722.json` | Google service-account key — never share |
| `data/<id>/` | Scraped product, AI copy, Shopify IDs per product |
| `images/<id>/` | `orig` (Meesho), `studio` (free photos), `ai` (Gemini photos) |
| `data/discovery/` | Navratri search results and the selected 100 |
| `logs/` | `bot.log`, `bulk.log`, `run.log` |
| `start_bot.bat`, `install_autostart.ps1` | Start the bot / start it at login |

## Accounts and setup

| Service | What was set up | Notes |
| --- | --- | --- |
| Shopify | Dev Dashboard app **Meesho Sync**, installed on thenoornaksh.myshopify.com | Scopes: products, inventory, locations, files, publications. Token via client credentials (auto-renews every 24 h) |
| Google Sheets | Sheet "meesho-shopify sync", tab **Products**; service account meesho-shopify-sync@meesho-shopify.iam.gserviceaccount.com | Key file in the project folder |
| Gemini | API key (free tier) | Text: 6 models tried in turn, ~20 requests/day each. Images: need billing |
| Telegram | Bot @noornaksh_import_bot | Only user ID 935890666 is allowed |
| Cloudflare WARP | Installed and on, full tunnel | Needed because the ISP drops most connections to Telegram |
| Meta Business | Portfolio "The Noor Naksh" (Instagram login) | Page not in portfolio yet; see Facebook and Instagram |

The Gemini key and Telegram token were pasted in chat during setup; rotating both is on the to-do list.

## Shopify store changes

The store was repositioned from jewellery to a clothing brand, with jewellery as an accessory line.

### Live now

| Change | Detail |
| --- | --- |
| Collections | New: New Arrivals (default for every push), Kids Ethnic Wear, Women's Ethnic Wear, Under ₹499 (automatic, price < 500). Renamed Nighty → Nightwear (link `/collections/nighty` unchanged). Kept Jewellery, Home page, The Navratri Collection (automatic, tag `navratri`) |
| Collection covers | Set for New Arrivals, Kids Ethnic Wear, Jewellery, Under ₹499; supplier code cropped off |
| Main menu | Home, Navratri Special, Kids Ethnic Wear, Jewellery, New Arrivals, Nightwear, Under ₹499, Shop All, Help (About, Contact, FAQ, Shipping, Returns) |
| About Us and Contact pages | Rewritten as a clothing brand; existing promises and email kept |
| Jewellery product template | 3 jewellery items use a `jewellery` template that keeps the Jewellery care tab |
| Products | 98 Navratri products as Draft (31 women's ethnic, 38 kids', 19 jewellery, 10 dupattas), tag `navratri`; the 2 earlier lehengas are also tagged `navratri` |

### In the theme preview "Noor Naksh - Clothing v3 (preview)"

Publish from Online Store → Themes when happy; the old theme stays saved.
Preview: https://thenoornaksh.myshopify.com/?preview_theme_id=191430492472

- Homepage: Navratri hero ("Nine nights. Your best Garba looks."), then Navratri Special grid (12),
  Kids' Navratri wear, Jewellery to complete your Garba look, Shop by category, New Arrivals,
  brand story, Steals under ₹499.
- Logo without the "Timeless Jewellery" tagline.
- Announcement bar: Navratri Festive Edit + free shipping, COD, 7-day returns.
- Clothing product page: Size & fit, Shipping & COD, Returns, Fabric care tabs; sizes as tap buttons.
- Product cards show "67% OFF" instead of "SALE".
- Footer: Shop and Help links, contact line, payment icons.

## Problems and fixes

| Problem | Cause | Fix |
| --- | --- | --- |
| Meesho returns 403 "Access Denied" | Akamai bot protection blocks automated and headless browsers | Scraper starts normal Chrome (minimized) and attaches to it; visits the homepage first; on repeated blocks it force-closes Chrome and starts a fresh profile |
| Meesho blocked after ~150 pages in one night | All traffic went through the shared Cloudflare WARP IP | A fresh browser cleared it; if it returns, exclude meesho.com, www.meesho.com, images.meesho.com in WARP split tunnels |
| Telegram bot could not connect (2 in 20 attempts) | ISP (Auriganet) drops connections to api.telegram.org; IPv6 fully broken | Cloudflare WARP; bot retries every outgoing message up to 5 times |
| A shared link was lost | First reply failed while WARP was switching on | Status message no longer blocks processing; lost messages are logged |
| Gemini images refused | Free tier has 0 image quota | Free studio photos (rembg on CPU, brand cream background) |
| Gemini text stopped after 13 products | Free tier allows ~20 requests/day per model | 6 models tried in turn; template copy from Meesho data if all are out |
| Supplier code (e.g. S-775771012) on photos | Meesho prints it at the bottom edge | Bottom 5% of every downloaded photo is trimmed |
| Bulk load and bot were stopped | PC ran low on memory with Chrome, rembg and the bot together | Run the bulk load alone, the bot after |
| 6 products failed | Delisted on Meesho (HTTP 410) | Skipped; total is 98, not 100 |
| Meesho typo "Occassion" in specs | Supplier data | Corrected for new products |

## Facebook and Instagram

Every linking route returns Meta's temporary "Linking isn't available right now" block. Next step:
wait 1–2 days and retry from the Instagram app.

**What exists**
- Facebook Page **The Noor Naksh** (ID 1368056269722078); the Facebook profile **Noor Naksh** has full control.
- Instagram **@thenoornaksh.in**, owned by business portfolio **The Noor Naksh** (ID 1471510648123215), whose admin logs in through Instagram.
- All three are in the same Accounts Center, so Meta treats both logins as one person.

**What was tried**

| Attempt | Result |
| --- | --- |
| Add the Page to the portfolio (Instagram login) | "Only people with full control can add it" |
| Invite rohitgande0705@gmail.com, accept as Noor Naksh | "Already in the business portfolio" — invite still pending, unused |
| Add the Page again after that | Same full-control error |
| Connect Instagram from Business Suite (Facebook login) | "Isn't available at the moment" |
| Connect Instagram from the Page's own settings | "Linking isn't available right now" |

**Goal once linked:** Shopify's Facebook & Instagram sales channel syncs the catalog (Instagram
product tags, Facebook Shop), and a 📣 Post button in the Telegram bot publishes normal posts to both.

## AI social-post images (research)

Model: **Qwen-Image-Edit-2511** (4-bit `ovedrive/Qwen-Image-Edit-2511-4bit` + Lightning 8-step LoRA).

| Where | Speed | Free limit | Result |
| --- | --- | --- | --- |
| HF Space `LPX55/...-Turbo-Lightning` (`qwen_test.py`, `persona_test.py`) | 11–21 s/image | ~5 GPU-min/day | Good; 2-reference persona shots lose garment detail |
| Kaggle T4 x2 (`kaggle/qwen_job/`) | 14 min/image (25 min with 2 refs) | 30 GPU-h/week | Step A excellent (`images/_kaggle_v10/out/A-haveli.png`) |

- Garment-on-model in a real scene (one garment reference) is excellent: patchwork, mirror dots and dupatta all kept.
- Two-step persona (dress first, then swap face to Noor) **failed**: in a full-body frame the face is too small and the model leaves it unchanged (`images/_kaggle_v11/out/B-haveli-noor-s3.png`).
- Kaggle T4 setup that works: transformer on GPU 0, text encoder on GPU 1, bf16 only (fp16 gives black images), chunked attention (T4 has no bf16 fast attention). `device_map="balanced"` and CPU offload both fail with 4-bit weights.
- Run a job: `kaggle kernels push -p kaggle/qwen_job --accelerator NvidiaTeslaT4`; put input images in `kaggle/inputs` and `kaggle datasets version -p kaggle/inputs -m "..."`.

## Catalog growth: winning products (10 Oct 2026)

`catalog.py` adds Meesho's best sellers per category, pushed **Active** with cleaned photos:
- `discover` (+ `discover more`) searches ~110 queries with Meesho's own "4.0 And Above" rating filter,
  10 pages each; `select 300` ranks by rating x log10(1 + ratings), assigns the group by Meesho's
  category (women / kids / jewellery / nightwear), and drops same-catalog duplicates (identical review
  pool) and near-identical titles/images. Result: 1,008 picks (women 300, jewellery 300, kids 274,
  nightwear 134; kids/nightwear have fewer distinct winners).
- `run` is resumable; `start_catalog.bat` runs it minimized (own Chrome on port 9334, so it never
  clashes with the bot). ~100 s per product. Progress: `logs/catalog_run.out`, `python catalog.py status`.
- Listing text: Gemini free quota covers ~120/day; the rest get the template description.
- Price/stock re-check retuned for ~1,300 products: every 12 h, 20 per 10-minute round.
- **Daily growth (11 Oct 2026):** `daily_catalog.bat` = `python catalog.py daily 100`. Each day it
  searches the next 25 queries per category (base items x modifiers like fabric/colour/occasion/age;
  women ~34 days of new queries, nightwear ~11 before the cycle repeats), merges them into
  `data/discovery/catalog.json`, picks the top 100 *new* winners per category into
  `data/discovery/daily/<date>.json` and pushes them Active. Same-day re-run resumes; a lock file stops
  two runs overlapping. Same quality bar as before (4.0★+, 30+ ratings, price range), so a category
  gets fewer than 100 when there aren't enough new winners. Log: `logs/catalog_daily.out`.

## Listing photos & theme (10 Oct 2026)

- **Background removal is off** (`studio.use_in_listing: false`): rembg erased white/cream/sheer fabric and
  dupattas. Listings use the original Meesho photos, cleaned by `pipeline/cleanup.py`
  (`listing_cleanup` in config.yaml): supplier code ("S-775771012") painted out with OpenCV inpainting,
  and every photo padded to 4:5 (plain background colour, or a soft blur for scenes) so product cards
  never crop it. Built into `images/<id>/listing/`. All 104 products re-pushed (`repush_originals.py`).
- **Live theme: "Noor Naksh - Clothing v5 (layout tidy)"**: product page 2-photo grid (no crop, no empty
  bars), solid header on product/collection pages (transparent only on the homepage), portrait 4:5 cards
  everywhere, collection grid 4 per row (2 on mobile), hero portrait image at 40% width, tighter section
  spacing, Navratri section 8 products. Theme edits must go to an unpublished copy, then Publish.
- **Navratri collection**: clothing-first order (women, kids, then jewellery), new short description.

## Instagram post pipeline (in progress)

One carousel post per product, 3 AI images each, 5 posts/day, worked top to bottom from the
**Posts** tab of the sheet. Style modelled on @_.theglowcart_: real Indian locations (a different one
in every photo), simple poses, face always hidden (hand over face, looking down, back view, turned
away, phone), outfit sharp and full-length, one waist-up detail shot, small faint handle watermark.

- `post_styles.yaml` — **all prompt options, editable**: scenes, poses, face-hiding methods, models per
  audience (women / kids / jewellery), shot plan, watermark, caption template, comment-reply texts.
- `pipeline/posts.py` — prompt planner, image generation (HF Space), watermark, caption, Posts tab.
- `post.py` — `queue` (add pushed products), `generate [N]`, `one <id>`, `plan <id>` (dry run).
- Posts tab statuses: QUEUED → GENERATING → GENERATED → APPROVED → POSTED (or SKIP / ERROR).
  Nightwear is SKIP by default (`skip_keywords`). Reorder rows to change posting order.
- Audience is detected automatically (kids sizes → child model; jewellery → close-ups).

**Image generation runs on Kaggle (free):** `kaggle/post_job/post_job.py`, dataset
`rohitgande/noornaksh-post-inputs`. Both T4s generate in parallel (~11 min per image per GPU, so
5 posts / 15 images ≈ 1.5 h, ~10 of the 30 free GPU hours a week). First batch (ctvgqs, h6o6p4):
garment reproduced very well; 2 of 6 showed the face and the "detail" shot came out full-body, so the
prompt was tightened (face rule at the end, chin-to-hips close-up).
- `python post.py kaggle-run [N]` / `kaggle-status` / `kaggle-fetch` (the bot does this automatically).
- Bot: every night at `kaggle.run_at` (00:30) it queues new pushed products, starts the batch, checks
  every 10 min, then sends each post (3 images + caption) with ✅ Approve / 🔄 Regenerate / ⏭ Skip.
  `/posts`, `/makeposts [n]`, `/postshow <id>`.

**Instagram (connected 9 Oct 2026):** Meta app **Noor Naksh Posts** (app ID 1106098335623268,
Instagram app ID 2084980388796902, development mode), "Instagram API with Instagram Login".
@thenoornaksh.in is an Instagram Tester; token in `.env` `IG_ACCESS_TOKEN` (refreshed weekly into
`data/ig_token.json`). Publishing quota 100 posts/day.
- `pipeline/instagram.py` (carousel publish, token refresh, comment → public reply + private-reply DM),
  `pipeline/publisher.py` (next APPROVED post with an Active product; images hosted on Shopify Files,
  centre-cropped to 4:5).
- Bot: posts at each `post_times` entry, checks comments every 2 min (only posts whose comment count
  changed), `/postnow` publishes the next approved post immediately.
- Products that aren't Active are posted too (since 11 Oct, see "Posting products that aren't Active"
  below); with `no_store_link.post_anyway: false` they're skipped and reported instead.
- Development mode: DMs/replies may only work for accounts with a role on the app until the app passes
  Meta App Review (advanced access for `instagram_business_manage_messages` / `_manage_comments`).

**Facebook Page (connected 10 Oct 2026; non-expiring Page token in `data/fb_token.json`, Page id
1368056269722078; app use cases "Manage everything on your Page" + "Messenger" added):** every carousel is also posted to
the Page (one multi-photo post) and keyword comments there get the same public reply + Messenger
private-reply DM with the product link (`comment_automation` in `post_styles.yaml` covers both).
- `pipeline/facebook.py`; Posts tab gained `fb_post_id`, `fb_permalink` (added at the end automatically).
- Token: `python fb_token.py` (needs FB_APP_ID / FB_APP_SECRET / FB_USER_TOKEN from Graph API Explorer
  with pages_show_list, pages_read_engagement, pages_read_user_content, pages_manage_posts,
  pages_manage_engagement, pages_messaging) → non-expiring Page token in `data/fb_token.json`.
  Until it exists Facebook is skipped silently. `facebook.enabled: false` turns it off.
- Same development-mode limit: Page DMs to the public need App Review (advanced `pages_messaging`).

**Hand-made carousels (11 Oct 2026):** `/originals <name or id>` sends the product's cleaned original
photos as files, opens a draft and sends one ready-to-paste Gemini prompt per slide (same shot plan /
scenes / face-hiding / outfit lock as the Kaggle posts, from `post_styles.yaml`; 🔄 New prompts re-rolls
them); you make AI images in the Gemini app and reply to that message with
them (album or one by one, up to 10). The bot fits each to 4:5 (blurred fill, nothing cropped), shows
the carousel + caption with buttons: Post now / Add to queue / Real photo first / Edit caption /
Start over / Cancel. It uses the same Posts row, publisher and comment→DM as Kaggle posts
(`source = manual`; an edited caption is kept, `{link}` is replaced with the store link).
- `pipeline/manual_posts.py`; uploads in `images/<id>/post/src/`, draft routing in
  `data/carousel_drafts.json` (reply to any bot message of the draft, or put the product ID in the caption).
- Re-posting a product moves comment→DM tracking to the new post.

**Gemini app in Chrome (15 Oct 2026):** `/gemini <name or id>`, or the 🤖 button under the `/originals` prompts,
makes the carousel images in the Gemini app automatically, then shows the usual carousel preview.
- How it works:
  - `pipeline/gemini_web.py` drives a visible Chrome window (Playwright, real Chrome) with its own profile
    `data/gemini_chrome/`. Sign in once by hand: `.venv\Scripts\python -m pipeline.gemini_web login`.
    This opens plain Chrome on that profile, because Google refuses sign-in in a Playwright-driven browser
    ("This browser or app may not be secure"). Runs then reuse the session.
  - For each slide it opens a new chat, attaches the reference photo, picks "Create image", pastes the prompt,
    waits up to 4 min, and saves the image with "Download full-sized image" (falls back to the preview).
- Safe limits:
  - 5 images per run and 20 a day (`PER_RUN`/`PER_DAY`), with a 40-90 s pause between prompts.
  - Runs by itself at `gemini_web.run_at` in `post_styles.yaml` (10:00 and 15:00): it takes the next QUEUED
    post whose product is PUSHED+ACTIVE with original photos (`_gemini_pick`; the 00:30 Kaggle batch has
    already taken the first ones). It can also be started by hand. Every carousel still waits for your approval.
  - Tested 15 Oct on 3wy5a (one image): the outfit matched well, 928x1152 full size. Gemini puts its small
    ✦ mark in the bottom-right corner.
  - It stops and stays off until the next day on: a sign-in page, a robot/"unusual traffic" check, a usage
    limit, or two slides in a row without an image. It never tries to get past a check.
  - `python -m pipeline.gemini_web unblock` clears the pause. State is kept in `data/gemini_web.json`.
- **Single-image posts from the prompt library (15 Oct 2026):** every Gemini run makes 1 image per post.
  - `pipeline/prompt_library.py` picks the folder `prompts/women|men|kid` from the product (jewellery is skipped)
    and the least recently used style (`data/prompt_usage.json`).
  - The free Gemini API, with the product photo, fills the [PLACEHOLDERS]; the photo wins over Meesho's colour data.
    A busy model is skipped without retries.
  - Config in `post_styles.yaml` under `gemini_web`: `products_per_run: 2`, `faces: hidden|visible`.
  - Download quirk: clicking download right after generating can close Chrome. So the shown image is copied
    first (canvas), then the full-size download is tried, reopening the chat in a fresh browser if needed.
  - Tested on 150i7j (kid/05-pastel-studio-hero): prompt ready in 24 s, image in 45 s at 928x1152, close match.
  - The Kaggle nightly batch still makes 5-image carousels.
  - The weekly colour carousel is built: see below.
- Risk accepted by the user: Google's terms are aimed at people, not scripts, and the account could be flagged.
- Page selectors (checked 15 Oct 2026; Google changes them):
  - `rich-textarea .ql-editor`, the "Upload and tools" button, `[data-test-id=local-images-files-uploader-button]`;
  - `generated-image img.loaded`, `button[aria-label^="Download full"]` ("full size" in the script's Chrome, "full-sized" in yours).
  - The menu button is "Upload & tools" in the script's Chrome and "Upload and tools" in yours, so the regex accepts both.

**Reels made on this laptop (13 Oct 2026, CPU only):** `/reel <name or id> [style]` in Telegram.
`pipeline/reels.py` draws every frame with PIL (zoom/pan/punch/drift, cut-out "3D float" via rembg,
cut/flash/fade/whip transitions, hook/title/price/CTA text with colour emoji) and pipes them to FFmpeg
(1080x1920, 30 fps, H.264/AAC). Cuts land on the beats of a random track in `music/` (librosa beat
tracking, starts at the loudest stretch; 120 BPM grid + silence if the folder is empty). Styles in
`reel_templates.yaml`: angle_snap, slow_reveal, price_reveal, float_3d, your_sign. Render time on the
Ryzen 3 3200U: ~25 s for a 10-12 s reel, ~45-85 s with the 3D float (first cut-out per photo is slow).
- Preview buttons: Post reel / Other style / Re-roll / Send file (post by hand with a trending sound) / Discard.
- Posting: mp4 hosted on Shopify Files (`Shopify.host_video`) → Instagram REELS container from the URL
  (`instagram.publish_reel`, verified up to processing) → Facebook Page reel (`facebook.publish_reel`,
  video_reels start/upload-by-URL/finish; not yet tried live). Logged in the **Reels** tab; comment→DM
  covers reels too. Drafts/renders: `data/reels.json`, `images/<id>/reels/`.
- Next (GPU, not on this laptop): Kaggle Wan2.2 clips (360 spin, fabric motion) and Gemini-app Veo
  clips as extra slot types.
- **NEXT ITEM (agreed 13 Oct 2026):** motion clips on the SAME Kaggle account (no second account:
  Kaggle allows one per person). Image-to-video (Wan 2.2 5B / LTX-2 distilled) from AI model photos,
  5 s 9:16, queued behind photo jobs (photos keep priority; ~20 spare GPU-h/week). Clip = reel opener.
  First step: one test clip from an existing AI photo to check quality + time on T4.
- User feedback 13 Oct: current reels look bad and the CC music is not viral. Seen in a review:
  the same supplier photo repeated, blurred letterbox bands, a long raw product title as the hook, and the
  CTA+price covering half the reel. CC tracks are random stock music picked only by tempo.
  Fixed the same day (`pipeline/reels.py`):
  - Photos fill the screen, cropped around the model (`photo_rect`). The shrink + blur `make_room` was removed.
  - The opener is the first AI shot, and a photo is never shown twice; slots with no photo left are dropped,
    so products with few photos get shorter reels.
  - `{short}` is now the kind ("lehenga"), and the title is a short name ("Cream Lehenga").
  - The closing price + CTA appear only in the last `CTA_SECONDS` (2.4 s).
  - A top line that would cover the face (`FACE`, top 20% of the model) moves to the bottom band.

**Copying viral reels (13 Oct 2026):** send the bot a reel video (≤20 MB, e.g. Instagram app → Share →
Download) or a reel/Shorts link (yt-dlp; Instagram often needs a login, then send the file).
`pipeline/viral.py` finds the cuts (PySceneDetect + whip pans by phase correlation + dissolve check),
each shot's camera move (ORB + affine fit → zoom_in/out, punch, pan, drift, still) and transition
(flash/fade/whip/cut), and the beat (librosa). If ≥60% of cuts are on the beat, shot lengths are saved
in beats (half-beats allowed) so our version cuts on our music, else in seconds. Gemini (video input,
one request) reads the on-screen text, its role/height and timing, shot framing (detail shots → zoomed
fabric "detail" effect), the audio, and rewrites each line for our product with {short}/₹{price};
lines quoting made-up prices are dropped, and our "Comment LINK" CTA is always kept for comment→DM.
Saved as a new style `v_<name>` in `reel_templates_copied.yaml` (merged into the style list); the
analysis and source video are in `data/viral/<time>/`. Only the structure is reused, never the footage
or audio. Tested on our own rendered reels (known structure): cut times exact, whips/flash/fade/punch
read correctly, beat sync 6/6; ~10-30 s per reel on this laptop plus the Gemini call.

**Meesho sync messages (13 Oct 2026):** price/stock changes are no longer sent one by one; they're
counted in `data/sync_summary.json` and sent once a day at 21:00 ("N products updated: x price, y
stock, z removed → Draft"). Overrides that fall below Meesho's cost are still named in that message,
and the "Meesho is blocking the checker" warning still comes once a day when it happens.

**Auto reels (13 Oct 2026):** settings under `auto:` in `reel_templates.yaml` (on, 2/day). Each day
after 07:30 (or when the bot next runs) the bot picks live, in-stock products without a reel in the
last 30 days (skip_keywords respected; products whose AI carousel was made in the last 4 days go
first, then products with any AI images are 4x as likely; with 3+ AI images the reel leads with AI
scenes, the hero stays the real photo), the least-used style
(including copied viral ones) and an unused-lately music track, renders, and sends previews. ✅ Approve
queues a reel; one approved reel posts at each of 12:15 and 20:30 (Instagram + Facebook, comment→DM,
Reels tab). `auto_approve: true` posts without asking. `/reelbatch [n]` makes today's now. State:
`data/reels.json` (DRAFT/APPROVED/POSTED/DISCARDED), `data/reels_auto.json` (last batch day).

**Licensed music (13 Oct 2026):** `pipeline/music.py` adds 15 tracks every Sunday 09:30 (`/music` for
now) from Openverse (Jamendo etc.), only CC BY / CC0 / public domain (business use + editing allowed;
ND/NC/SA skipped), each checked for a steady 70-170 BPM beat and loudness, Indian-sounding searches
first. Credits in `music/credits.json`; CC BY credit ("🎵 Music: X by Y (CC BY 3.0)") is added to the
reel caption automatically. 14 tracks so far. The Indian CC pool is small (~40), so later weeks lean
on general fashion/upbeat tracks.

**Weekly trending songs (13 Oct 2026):** every Monday 10:00 (and `/songs`) the bot sends 15 new
Hindi/Punjabi songs for reels. Source: Shazam's public India / Mumbai / Delhi / Discovery charts (CSV,
past 7 days); Gemini (no web search: the free key has no Google Search quota) rates language, fit for
fashion reels, mood and how to use each; ranking = chart rank, fit and week-on-week climb.
`pipeline/trending.py`, history in `data/trending_songs.json` (no repeats). Songs are NOT downloaded
(copyright: Instagram mutes/blocks label music baked into a business account's video, and only songs
picked in the app get the trending reach): post with "Send file" and add the song in Instagram.

**Posting products that aren't Active (11 Oct 2026):** `no_store_link.post_anyway: true` in
`post_styles.yaml` (applies to scheduled Kaggle posts and hand-made carousels). The caption links to
the store home page, "Link" commenters get `no_store_link.dm_template` ("coming soon, reply to order");
the bot re-checks the product every 30 min and, once it's Active, saves its link and DMs send that.
Set `post_anyway: false` to go back to waiting for Active products.

## Remaining work

### Now (you)
- [ ] Review the 98 Navratri drafts: Shopify admin → Products → filter tag `navratri`, status Draft; check photos and prices, then select → Set as active
- [ ] Open the theme preview and publish "Noor Naksh - Clothing v3 (preview)"
- [ ] Set price format to ₹: Settings → General → Store currency → Change formatting → `₹{{amount_no_decimals}}`
- [ ] Confirm the Butterfly Stainless Steel Chain price (currently ₹5,000)
- [ ] Delete the unused file `index-navratri.json` in Content → Files

### Soon
- [ ] Restart the Telegram bot (`start_bot.bat`, or ask Claude); optionally run `install_autostart.ps1`
- [ ] Enable Gemini billing for AI photos (~₹3 per image, ~₹9 per product)
- [ ] Rotate the Gemini API key and Telegram bot token (both were pasted in chat)
- [ ] Install free apps: Judge.me (reviews), a WhatsApp chat button, Search & Discovery (size/colour filters)

### In 1–2 days (Meta)
- [ ] Instagram app → @thenoornaksh.in → Edit profile → Page → Connect existing Page → The Noor Naksh
- [ ] Then (Claude): add the Page to the portfolio, connect Instagram, set up Shopify's Facebook & Instagram sales channel, add the Post button to the bot
- [ ] Remove the unused portfolio invite to rohitgande0705@gmail.com

### Later
- [ ] After Navratri: switch the homepage to a Diwali edit (new hero, Diwali collection)
- [ ] Create collections as products arrive: Sarees, Diwali Edit, Wedding Edit
- [ ] Orders are not automated: each Shopify order is placed manually on Meesho with the customer's address

## Daily use

Keep the PC on with Cloudflare WARP connected and the bot running.

| Task | How |
| --- | --- |
| Add a product | Share the Meesho link to @noornaksh_import_bot |
| Approve, redo photos, reject | Buttons under the preview |
| Go live / hide | 🟢 Make Active / ⏸ Back to Draft buttons, or `/activate <id>`, `/draft <id>` |
| Collections | 📁 Collections button, or `/collection <id> Name` |
| Change price | `/price <id> 699` (or `auto`) |
| Check status | `/pending`, `/show <id>`, `/status` |
| Push or re-check now | `/push`, `/resync` |
| Start the bot | Double-click `start_bot.bat` |
| Check all connections | `.venv\Scripts\python run.py check` |
| Bulk add from Meesho | `.venv\Scripts\python discover.py`, then `bulk_add.py select` and `bulk_add.py run` |

Costs: Shopify Basic plan; Gemini text free (~120 listings/day across models); AI images ~₹3 each
if billing is enabled; studio photos free.

**Weekly colour carousel (15 Oct 2026):** each Sunday at 11:00 (`colour_carousel` in `post_styles.yaml`),
or with `/colours`, the bot posts one design in all its colours, after approval.
- Finding a design (`pipeline/colour_posts.py`):
  - Candidates are live, in-stock products with the same supplier and category, price within 20%, and titles
    that match once colour words are removed (or near-matching titles with close IDs). One product per colour.
  - Gemini then checks the photos strictly: same cut, neckline, border and motifs, only the colour differs.
    Groups that fail are stored as NOT_SAME and never re-checked.
  - Tested 15 Oct: it rejected 3 look-alike groups and accepted a kurta-sharara set in Pink/Teal Blue/Purple
    (86d76o/q/r).
- Slides:
  - A cover grid "1 design · N colours" with name chips, then one 4:5 slide per colour.
  - Each slide uses the AI shot if the product has one, else the ORIGINAL Meesho photo with the bottom 6%
    (supplier code) cut off; the listing copies have blur padding baked in.
  - Wide photos are cropped around the model (rembg). Any remaining space is filled with the photo's edge
    colour, never blur.
- Telegram preview buttons: 🚀 Post now / 🔄 Another design (that one is passed over for 30 days) /
  ❌ Discard (never again). A posted design isn't suggested again for 60 days.
- "Link" comments get one DM listing every colour's link (`CommentTargets`, hooked into
  `publisher.check_comments`). State is in `data/colour_posts.json`, slides in `data/colour_posts/<id>/`.
