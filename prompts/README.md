# Image prompts: women / men / kid (10 each)

Every prompt is used with the **product photo attached** in Gemini (Nano Banana). Fill the `[PLACEHOLDERS]` with the product's real details, or delete one if it doesn't apply. Each prompt has an **OUTFIT lock** paragraph so Gemini copies the garment instead of redesigning it.

The `men/meb.txt`, `pr.txt` and `sds.txt` files are your originals and are kept as they were.

## What the research says gets engagement (15 Oct 2026)

| Finding | Source | What we do with it |
|---|---|---|
| Fashion carousels get about 2x the interactions of single images (median 53 vs 25; Reels 47). | Emplifi, Fashion benchmarks Q2 2026 | Post 4–6 slides per product, not one photo. |
| More images in the same post is generally effective. Luxury audiences prefer product-focused images without distracting backgrounds or influencers. | Gao et al., *Journal of Interactive Marketing* (2025), study of fashion brands' Instagram posts | Mix lifestyle shots with one **clean studio hero** and one **detail close-up**. |
| Photos with human faces get 38% more likes and 32% more comments. | Georgia Tech + Yahoo Labs, 1.1M Instagram photos (2014) | Our posts hide faces. Try "natural face, looking away" on some slides (the `[FACE]` placeholder). |
| Carousels average about 0.55% engagement vs 0.37% for single images; single-image engagement fell 18% year over year. | Socialinsider figures, via Stack Influence | Same as the first row. |
| Instagram grids are now 4:5 portrait (1080x1350). | Instagram 2025 change, via Vista Social | Every prompt asks for vertical 4:5. |
| Name the fabric and the exact craft (zardozi, gota-patti, chikankari); use one clear light direction; keep a single style; add one natural imperfection. | Morphed: Nano Banana fashion prompt tests (2026) | Built into every prompt (fabric, light, camera, AVOID lines). |
| Popular Indian Gemini trends: mid-twirl Garba shots, mirror-work portraits, vintage festive posters, golden-hour light with film grain. | Storyboard18 (Navratri / Diwali Nano Banana trends) | `01 twirl`, `07 collage`, festive props. |
| UGC-style mirror selfies, "get ready with me" and outfit reveals are common fashion formats. | Hootsuite carousel guide; Instagram trend roundups | `02 mirror selfie` and `05 getting ready` (women). |

There is **no solid study comparing flat lays with model shots for Indian ethnic wear**. The table above comes from benchmarks, one peer-reviewed study, and practitioner guides, so test what works on our own account by comparing saves, shares and comments per post type.

## Suggested carousel order (5 slides)
1. **Hook:** a motion/lifestyle shot (twirl, heritage, street style, wedding candid).
2. **Clean studio hero:** the whole outfit, clearly shown.
3. **Detail close-up:** embroidery or fabric.
4. **Second lifestyle or UGC mirror selfie:** "how it looks in real life".
5. **Flat lay or split:** the complete look; "Comment LINK" goes in the caption.

## Sources
- Emplifi, Fashion benchmarks Q2 2026: https://emplifi.io/resources/q2-2026-fashion-worldwide-benchmarks/
- Emplifi, Clothing benchmarks Q2 2026: https://emplifi.io/resources/q2-2026-clothing-worldwide-benchmarks/
- Gao et al., *How Does Brand-Generated Content Engage Consumers? A Study on Fashion Brands' Instagram Data*: https://journals.sagepub.com/doi/10.1177/10949968251335616
- Georgia Tech, *Face It: Instagram Pictures With Faces are More Popular*: https://www.news.gatech.edu/news/2014/03/20/face-it-instagram-pictures-faces-are-more-popular
- Stack Influence, Instagram carousels guide: https://stackinfluence.com/blog/what-are-instagram-carousels-2026-guide
- Hootsuite, Instagram carousels: https://blog.hootsuite.com/instagram-carousel/
- Vista Social, Instagram trends 2025: https://vistasocial.com/insights/instagram-trends-in-2025
- Morphed, Nano Banana prompts for fashion: https://morphed.app/blog/nano-banana-prompts-for-fashion
- Storyboard18, Navratri Nano Banana trend: https://www.storyboard18.com/amp/how-it-works/google-gemini-nano-banana-trend-how-to-create-your-ideal-festive-navratri-look-81238.htm
- Storyboard18, Diwali couple prompts: https://www.storyboard18.com/amp/digital/nano-banana-diwali-prompts-create-festive-couple-portraits-indoors-with-stylish-poses-82824.htm
