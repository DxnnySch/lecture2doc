from pathlib import Path

import numpy as np
import pymupdf
from pptx import Presentation


def make_pdf(path: Path, n: int, titles: list[str] | None = None, width: int = 960, height: int = 540,
             numbers: bool = False) -> Path:
    """numbers=True prints the page number bottom right (like a real slide footer) and keeps the rectangles clear of it."""
    doc = pymupdf.open()
    for i in range(n):
        page = doc.new_page(width=width, height=height)
        rng = np.random.RandomState(i)
        for _ in range(12):
            x, y = int(rng.randint(0, width - 160)), int(rng.randint(60, height - (190 if numbers else 120)))
            w, h = int(rng.randint(40, 160)), int(rng.randint(20, 100))
            page.draw_rect(pymupdf.Rect(x, y, x + w, y + h), color=None, fill=tuple(float(c) for c in rng.rand(3)))
        page.insert_text((40, 40), titles[i] if titles else f"Title {i + 1}", fontsize=24)
        if numbers:
            page.insert_text((width - 79, height - 32), str(i + 1), fontsize=14)
    doc.save(path)
    return path


def make_pptx(path: Path, slides: list[tuple[str | None, str, bool]]) -> Path:
    prs = Presentation()
    for title, body, hidden in slides:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        if title is None:
            el = s.shapes.title._element
            el.getparent().remove(el)
        else:
            s.shapes.title.text = title
        s.placeholders[1].text = body
        if hidden:
            s._element.set("show", "0")
    prs.save(path)
    return path


import subprocess

from PIL import Image


def make_video(images, order, path: Path, fps: int = 1, pillarbox_4_3: bool = False, size=(320, 180)) -> Path:
    """One frame per entry of `order`: an index into images, an image itself, or None for a black frame."""
    vw, vh = size
    frames = path.parent / (path.stem + "_frames")
    frames.mkdir(exist_ok=True)
    for i, idx in enumerate(order):
        source = idx if isinstance(idx, Image.Image) else (None if idx is None else images[idx])
        if source is None:
            img = Image.new("RGB", (vw, vh))
        elif pillarbox_4_3:
            pw = round(vh * 4 / 3)
            img = Image.new("RGB", (vw, vh))
            img.paste(source.resize((pw, vh)), ((vw - pw) // 2, 0))
        else:
            img = source.resize((vw, vh))
        img.save(frames / f"f{i:03d}.png")
    seconds = len(order) / fps
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i", str(frames / "f%03d.png"),
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


def animation_state(image):
    """What a half-built slide looks like: everything but the footer (with the page number) is still missing."""
    w, h = image.size
    state = Image.new("RGB", (w, h), "white")
    state.paste(image.crop((0, int(h * 0.87), w, h)), (0, int(h * 0.87)))
    return state
