from pathlib import Path

import numpy as np
import pymupdf


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


import subprocess

from PIL import Image


def make_video(images, order, path: Path, fps: int = 1, pillarbox_4_3: bool = False, size=(320, 180),
               audio: Path | None = None) -> Path:
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
         *(["-i", str(audio)] if audio else ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]),
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


def make_wav(path: Path, pieces, rate: int = 16000) -> Path:
    """Audio from pieces like [("tone", 2.0), ("silence", 1.5)]: a loud 440 Hz tone or digital silence."""
    import wave

    parts = []
    for kind, seconds in pieces:
        n = int(seconds * rate)
        if kind == "tone":
            parts.append((0.5 * 32767 * np.sin(2 * np.pi * 440 * np.arange(n) / rate)).astype(np.int16))
        else:
            parts.append(np.zeros(n, np.int16))
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(np.concatenate(parts).tobytes())
    return path
