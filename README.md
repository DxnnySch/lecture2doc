# lecture2doc

Turns a lecture video plus its slides (PDF export and PPTX) into a Word document with one section per slide: the slide title, a thumbnail, and what the lecturer said while that slide was on screen. Speech-to-text and transcript cleanup run through the ScaDS.AI OpenAI-compatible API.

How it works: video frames are sampled and matched against the PDF pages to find which slide is shown when (4:3 slides shown pillarboxed in a 16:9 recording are handled); the audio is transcribed in chunks with word timestamps; each word is assigned to the slide visible at that moment; an LLM then fixes recognition errors using a glossary built from the slide text (with guardrails that fall back to the raw text if the output deviates too much).

## Setup

1. Python 3.11+ and `ffmpeg` on `PATH`.
2. Create the environment and install dependencies:
   ```bash
   python -m venv .venv
   .venv/Scripts/pip install -r requirements.txt     # Linux/macOS: .venv/bin/pip
   ```
   `openai>=3` is required.
3. Copy `.env.example` to `.env` and fill in your key (`.env` is git-ignored; never commit it). Variables:
   - `SCADS_API_KEY` (required) - your ScaDS.AI API key
   - `SCADS_BASE_URL` - default `https://llm.scads.ai/v1`
   - `SCADS_STT_MODEL` - speech-to-text model alias, default `alias-stt`
   - `SCADS_LLM_MODEL` - model for transcript cleanup, default `alias-huge-no-thinking`
   - `SCADS_GLOSSARY_MODEL` - optional separate model for the glossary step (e.g. a reasoning model); defaults to `SCADS_LLM_MODEL`

## Usage

```bash
.venv/Scripts/python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx -o lecture.docx
```

The PDF supplies the page images used for matching; the PPTX supplies slide titles. Hidden PPTX slides are skipped (they are not in the PDF). If the PPTX is unreadable or its slide count does not match the PDF, titles are taken from the PDF (the largest text in the upper part of each page, so footers are ignored) and a warning is printed.

In the document, each slide gets a heading (`Slide N – Title`), a subtitle line with the time range(s) it was on screen in the video (e.g. `12:30 – 15:02`, several ranges if the lecturer returned to it), its thumbnail and the transcript. Every slide starts on a new page.

Intermediate files go to the work directory (default `output/<video stem>/`): `words.json` (STT cache, so reruns are free; progress is also cached per chunk in `words.json.chunks.json`, so a failed run resumes where it stopped), `llm_cache.json` (cleanup cache), and `segments.json`.

### Options

| Option | Meaning |
| --- | --- |
| `-o, --output` | output `.docx` (default `<workdir>/<video stem>.docx`) |
| `--workdir` | cache/debug directory |
| `--language` | language code for STT (default: auto-detect) |
| `--no-cleanup` | skip the LLM cleanup, use the raw transcript |
| `--keep-raw` | also put the raw transcript in the document |
| `--no-thumbnails` | omit slide thumbnails |
| `--no-timestamps` | omit the video time range under each slide heading |
| `--no-page-breaks` | do not start every slide on a new page (more compact) |
| `--rebuild` | rebuild the `.docx` from an existing `segments.json` only: no video, ffmpeg, API key or API calls (see below) |
| `--from-segments FILE` | reuse the slide/time spans from a (hand-corrected) `segments.json` |
| `--interval` | seconds between sampled frames (default 1.0) |
| `--threshold` | minimum match score for a frame to count (default 0.6) |
| `--smooth` | majority-vote window in frames (default 5) |

A summary after matching lists seconds matched per slide, the unmatched percentage, and slides never shown. Many unmatched seconds or flicker between neighbouring slides: tune `--threshold` (0.5-0.8) and `--smooth` (3-9); this reruns quickly because STT is cached.

## Correct-and-regenerate workflow

`segments.json` holds every span (`slide`, `start`, `end`, `confidence`, `raw`, `clean`). If a slide assignment is wrong, edit the `slide`/`start`/`end` values and regenerate with the corrected spans:

```bash
.venv/Scripts/python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx --from-segments output/<stem>/segments.json
```

Words are re-assigned to the edited spans using the cached `words.json`.

## Rebuild the document offline

Layout options (`--no-timestamps`, `--no-page-breaks`, `--no-thumbnails`, `--keep-raw`) and the slide titles only affect the Word file, so you never need to transcribe again to change them. `--rebuild` regenerates the `.docx` from the saved `segments.json` (which holds the cleaned and raw text and the times) plus the PDF/PPTX, in seconds, without video, ffmpeg, API key or network:

```bash
.venv/Scripts/python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx --rebuild
```

It reads `<workdir>/segments.json` (or the file given with `--from-segments`), overwrites the `.docx` and never touches `segments.json` or the caches. The video path is only used to name the output. You can hand-edit `clean` text or slide/time spans in `segments.json` first.

## Known limits

- The tool assumes a single speaker: there is no diarization and no speaker labels. No summaries either; the output is the transcript only.
- Slide matching relies on the video showing the slides; talking-head-only stretches are attached to the neighbouring slide.
- LLM cleanup can occasionally misjudge a term; use `--keep-raw` to compare, or `--no-cleanup`.
- The API can be slow; a full lecture's cleanup may take a while.
