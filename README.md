# lecture2doc

Turns a lecture video plus its slides (PDF export and PPTX) into a Word document with one section per slide: the slide title, a thumbnail, and what the lecturer said while that slide was on screen. Speech-to-text and transcript cleanup run through the ScaDS.AI OpenAI-compatible API.

How it works: video frames are sampled and matched against the PDF pages to find which slide is shown when (4:3 slides shown pillarboxed in a 16:9 recording are handled, and the page number in the corner of the video identifies half-built animated slides); the audio is cut at every slide change and the audio of each stretch on one slide is transcribed on its own, so every word belongs to exactly one slide; an LLM then fixes recognition errors using a glossary built from the slide text (with guardrails that fall back to the raw text if the output deviates too much).

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
   - `SCADS_REQUESTS_PER_MINUTE` - cap for LLM requests per minute, default `25` (the gateway allowed 30 per key and model when this was written); lower it if your key has a smaller quota

## Usage

```bash
.venv/Scripts/python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx -o lecture.docx
```

The PDF supplies the page images used for matching; the PPTX supplies slide titles. Hidden PPTX slides are skipped (they are not in the PDF). If the PPTX is unreadable or its slide count does not match the PDF, titles are taken from the PDF (the largest text in the upper part of each page, so footers are ignored) and a warning is printed.

The document has one page per stretch of the lecture on one slide, in lecture order. Each page has a heading (`Slide N – Title`), a subtitle line with the time range it was on screen in the video (e.g. `12:30 – 15:02`), the slide's thumbnail and the transcript of that stretch. If the lecturer goes 24 → 25 → 24 → 23 → 26, that is five pages in exactly that order, even though slide 24 appears twice. Slides that were never shown get no page. With `--group-by-slide` the document instead has one section per slide of the deck, in slide order, with all visits of a slide together (time-stamped paragraphs).

Intermediate files go to the work directory (default `output/<video stem>/`): `clips.json` (speech-to-text cache, one entry per audio clip and saved as soon as the clip is done, so reruns are free and a failed run resumes where it stopped), `llm_cache.json` (cleanup cache), and `segments.json`.

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
| `--group-by-slide` | one section per slide of the deck, in slide order, instead of one page per stretch of the lecture |
| `--no-page-breaks` | do not start every page on a new page (more compact) |
| `--rebuild` | rebuild the `.docx` from an existing `segments.json` only: no video, ffmpeg, API key or API calls (see below) |
| `--from-segments FILE` | reuse the slide/time spans from a (hand-corrected) `segments.json` |
| `--interval` | seconds between sampled frames (default 1.0) |
| `--threshold` | minimum match score for a frame to count (default 0.6) |
| `--smooth` | majority-vote window in frames (default 5) |
| `--no-slide-numbers` | do not use the page number in the corner of the video to identify slides (see below) |

A summary after matching lists seconds matched per slide, the unmatched percentage, and slides never shown. Many unmatched seconds or flicker between neighbouring slides: tune `--threshold` (0.5-0.8) and `--smooth` (3-9); this reruns quickly because the transcription is cached.

## Correct-and-regenerate workflow

`segments.json` holds every span (`slide`, `start`, `end`, `confidence`, `audio_start`, `audio_end`, `raw`, `clean`). `start`/`end` are when the slide was on screen; `audio_start`/`audio_end` are where the audio was cut for its transcript. If a slide assignment is wrong, edit the `slide`/`start`/`end` values and regenerate with the corrected spans:

```bash
.venv/Scripts/python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx --from-segments output/<stem>/segments.json
```

The audio is cut again at the edited times; only the clips whose cut times changed are transcribed again (and cleaned again by the LLM), everything else comes from the caches.

## How slides are recognised

Every video frame is compared with the PDF pages. Slides that are built up by animation are the weak spot of that: a half-built slide hardly resembles its finished PDF page and may look more like another, emptier page. So, when the PDF prints page numbers (usually bottom right), the tool also compares the number area of each frame with the number area of every PDF page (no OCR; the PDF supplies how the numbers look). Where the corner clearly shows a page's number, that page wins over the whole-slide match; pages that print the same number are told apart by the picture. If the PDF has no usable page numbers, or the corner is not visible, matching falls back to whole slides. The matching summary says how many frames the number could be read in and how many frames it changed. `--no-slide-numbers` switches this off. It costs one more pass over the video.

## How the transcript is split between slides

The lectures are recorded slide by slide, so at every slide change the audio has a silent gap between two recordings (in the sample lecture a median of 2.5 s, against 0.2 s for ordinary pauses). After matching, the tool finds these gaps with ffmpeg's silence detector and cuts the audio in the middle of the longest gap (at least 0.5 s) within 3 seconds of every slide change. Each stretch on one slide is transcribed separately, so a sentence can never be split between two slides and nothing depends on word timestamps (the speech-to-text service only estimates those, evenly spread over each phrase, so they can put words on the wrong side of a slide change).

If a slide change has no silent gap nearby, as in a lecture recorded in one go, that change is cut exactly at the detected slide change and the summary says so: a word spoken right at such a change may then land on the neighbouring slide. A clip with less than half a second of speech is not sent to the speech-to-text service at all (silence makes the model invent text), and it gets an empty transcript.

The speech-to-text requests share the `SCADS_REQUESTS_PER_MINUTE` quota with the LLM requests, and each finished clip is cached at once.

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
- The API can be slow; a full lecture's cleanup may take a while. The LLM requests are throttled to `SCADS_REQUESTS_PER_MINUTE`, and a rate-limit error (HTTP 429) is retried after the server's `Retry-After` time or, without one, after 15/30/60 seconds. Results are cached, so an interrupted or partly failed run only repeats the missing calls.
