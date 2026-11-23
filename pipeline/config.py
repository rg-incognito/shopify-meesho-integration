import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
IMAGES_DIR = ROOT / "images"
LOGS_DIR = ROOT / "logs"

load_dotenv(ROOT / ".env")

with open(ROOT / "config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise RuntimeError(f"{name} is not set. Add it to {ROOT / '.env'} (see .env.example).")
    return value


def setup_logging(name: str) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOGS_DIR / f"{name}.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
