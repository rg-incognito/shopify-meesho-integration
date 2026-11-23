"""Persona test: create a fictional brand model once, then reuse her as a reference with any garment.
Uses the same Qwen-Image-Edit-2511 demo as qwen_test.py. Outputs: images/_persona/."""
import shutil
import sys
import time
from pathlib import Path

from gradio_client import Client, handle_file
from PIL import Image

from pipeline.config import env
from qwen_test import SPACE, prep

ROOT = Path(__file__).parent
OUT = ROOT / "images" / "_persona"
OUT.mkdir(parents=True, exist_ok=True)

PERSONA = ("a fictional young Indian woman named Noor, about 24 years old, warm wheatish skin, long dark brown wavy "
           "hair with a middle parting, soft oval face, defined dark eyebrows, brown eyes, a tiny gold nose stud, "
           "natural minimal makeup, gentle friendly expression. She is an original character, not a celebrity "
           "and not any real person")

GARMENT_LOCK = ("Keep the outfit IDENTICAL to the garment reference: same colours, prints, patchwork panels, "
                "borders, mirror work and the printed dupatta. Do not invent new patterns.")
FACE_LOCK = ("The woman must be the SAME person as in the first reference photo: same face, face shape, eyebrows, "
             "eyes, nose stud, skin tone and hair.")

JOBS = {
    # 1) create the persona from a blank canvas
    "noor-portrait": dict(
        canvas=True,
        prompt=(f"Photorealistic head-and-shoulders portrait of {PERSONA}. She wears a plain off-white cotton kurta. "
                "Soft natural window light, plain warm beige background, sharp focus on the face, looking at the "
                "camera with a slight smile. Professional photography, 85mm lens. No text, no watermark."),
        size=(832, 1024)),
    # 2) reuse her with the garment, two different scenes
    "noor-haveli": dict(
        refs=["images/_persona/noor-portrait.png", "images/ctvgqs/orig/01.jpg"],
        prompt=("Photorealistic full-body fashion photograph. The woman from the first reference image wears the "
                "complete outfit from the second reference image: black sleeveless blouse with coloured mirror dots, "
                "flared black, maroon and orange patchwork lehenga skirt with block prints, and the printed black "
                "and maroon dupatta over one shoulder. " + FACE_LOCK + " " + GARMENT_LOCK +
                " She stands in the sunlit courtyard of a Rajasthani haveli with white arches and potted plants, "
                "looking slightly away with a soft smile. Natural daylight, 50mm lens, candid Pinterest style. "
                "No text, no watermark."),
        size=(832, 1104)),
    "noor-garba-night": dict(
        refs=["images/_persona/noor-portrait.png", "images/ctvgqs/orig/01.jpg"],
        prompt=("Photorealistic full-body photograph at night. The woman from the first reference image wears the "
                "complete outfit from the second reference image: black sleeveless blouse with coloured mirror dots, "
                "flared black, maroon and orange patchwork lehenga skirt and the printed black and maroon dupatta. "
                + FACE_LOCK + " " + GARMENT_LOCK +
                " She is at a Navratri garba night holding two dandiya sticks, smiling at the camera, warm string "
                "lights and marigold garlands behind her, softly blurred dancers in the background. "
                "No text, no watermark."),
        size=(832, 1104)),
}


def run(name: str, steps=8):
    job = JOBS[name]
    if job.get("canvas"):
        canvas = OUT / "canvas.jpg"
        Image.new("RGB", job["size"], (235, 226, 212)).save(canvas, quality=95)
        inputs = [canvas]
    else:
        inputs = [prep(ROOT / r, f"{name}-{i}") if "orig" in r else ROOT / r for i, r in enumerate(job["refs"])]
    client = Client(SPACE, token=env("HF_TOKEN"), verbose=False)
    t0 = time.time()
    result, seed = client.predict(
        images=[{"image": handle_file(str(f))} for f in inputs], prompt=job["prompt"], seed=11,
        randomize_seed=False, true_guidance_scale=1.0, num_inference_steps=steps,
        height=job["size"][1], width=job["size"][0], rewrite_prompt=False, num_images_per_prompt=1,
        api_name="/infer")
    item = result[0]
    src = item["image"] if isinstance(item.get("image"), str) else item["image"]["path"]
    dst = OUT / f"{name}.png"
    shutil.copy(src, dst)
    print(f"OK {name} -> {dst} ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    for n in sys.argv[1:] or JOBS:
        try:
            run(n)
        except Exception as e:
            print(f"FAIL {n}: {repr(e)[:400]}", flush=True)
