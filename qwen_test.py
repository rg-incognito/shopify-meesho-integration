"""One-off test: Qwen-Image-Edit-2511 (public HF demo) for social-post images.
Outputs go to images/_qwen_test/. Free anonymous quota is small, so tests run one by one."""
import shutil
import sys
import time
from pathlib import Path

from gradio_client import Client, handle_file
from PIL import Image

from pipeline.config import env

ROOT = Path(__file__).parent
SPACE = "LPX55/Qwen-Image-Edit-2511-Turbo-Lightning"  # same model + lightning steps; fits the free GPU quota
OUT = ROOT / "images" / "_qwen_test"
OUT.mkdir(parents=True, exist_ok=True)


def prep(src: Path, name: str, crop_bottom=0.06, size=1024) -> Path:
    """Crop Meesho's supplier code off the bottom and downsize for upload."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    im = im.crop((0, 0, w, int(h * (1 - crop_bottom))))
    im.thumbnail((size, size))
    p = OUT / f"input-{name}.jpg"
    im.save(p, quality=92)
    return p


GARMENT_LOCK = ("Keep the outfit IDENTICAL to the reference image: same colours, prints, patchwork panels, "
                "borders, mirror work and dupatta. Do not invent new patterns.")

TESTS = {
    "1-haveli": dict(
        inputs=[ROOT / "images/ctvgqs/orig/01.jpg"],
        prompt=("Photorealistic fashion photograph. A young Indian woman in her mid-20s with long dark wavy hair and "
                "natural makeup wears exactly the outfit shown in the reference image: the black sleeveless blouse with "
                "small coloured mirror dots, the flared multicolour patchwork lehenga skirt (black, maroon, orange, block "
                "prints, mirror work) and the matching dupatta draped over one shoulder. " + GARMENT_LOCK +
                " She stands in the sunlit courtyard of an old Rajasthani haveli with white arches, red pillars and potted "
                "plants, looking down with a soft smile, one hand touching her hair. Natural daylight, shallow depth of "
                "field, 50mm lens, candid Pinterest style, full body, vertical frame. No text, no watermark."),
    ),
    "2-mirror-selfie": dict(
        inputs=[ROOT / "images/ctvgqs/orig/01.jpg"],
        prompt=("Photorealistic mirror selfie. A slim young Indian woman with long dark hair wears exactly the outfit "
                "shown in the reference image: black sleeveless blouse with coloured mirror dots, flared multicolour "
                "patchwork lehenga skirt and the matching dupatta over one shoulder. " + GARMENT_LOCK +
                " She holds a phone in front of her face, so the face is hidden, standing in front of a tall mirror in a "
                "cosy aesthetic bedroom with fairy lights, a plant and framed art. Soft warm evening light, full body, "
                "vertical frame, Instagram style. No text, no watermark."),
    ),
    "3-kids-garba": dict(
        inputs=[ROOT / "images/h6o6p4/orig/01.jpg"],
        prompt=("Photorealistic photograph. A happy 8-year-old Indian girl wears exactly the lehenga choli and net "
                "dupatta shown in the reference image, with the same colours, geometric prints and borders. " +
                "Do not change the outfit design." +
                " She is at a Navratri garba night, holding dandiya sticks, with warm string lights, marigold "
                "decorations and a softly blurred crowd behind her. Full body, vertical frame, joyful candid moment. "
                "No text, no watermark."),
    ),
}


def run(name: str, width=832, height=1104, steps=8):
    t = TESTS[name]
    files = [prep(p, f"{name}-{i}") for i, p in enumerate(t["inputs"])]
    client = Client(SPACE, token=env("HF_TOKEN"), verbose=False)
    t0 = time.time()
    result, seed = client.predict(
        images=[{"image": handle_file(str(f))} for f in files],
        prompt=t["prompt"], seed=7, randomize_seed=False, true_guidance_scale=1.0,
        num_inference_steps=steps, height=height, width=width, rewrite_prompt=False, num_images_per_prompt=1, api_name="/infer")
    item = result[0]
    src = item["image"] if isinstance(item.get("image"), str) else item["image"]["path"]
    dst = OUT / f"{name}.png"
    shutil.copy(src, dst)
    print(f"OK {name} -> {dst} ({time.time() - t0:.0f}s, seed {seed})", flush=True)


if __name__ == "__main__":
    for n in sys.argv[1:] or TESTS:
        try:
            run(n)
        except Exception as e:
            print(f"FAIL {n}: {str(e)[:400]}", flush=True)
