# lecture2doc

Turn a recorded lecture into a Word document you can skim and search.

You give it the **lecture video** and the **slides** (as PDF and PowerPoint). You get a `.docx` with one page per stretch of the lecture: the slide title, a thumbnail of the slide, the time it was on screen, and a cleaned-up transcript of what the lecturer said while it was shown.

```
lecture.mp4 + slides.pdf + slides.pptx   ->   lecture.docx
```

## What you need

| Requirement | Notes |
| --- | --- |
| Python 3.11 or newer | check with `python --version` |
| ffmpeg | must be on your `PATH`, check with `ffmpeg -version` (see below) |
| A ScaDS.AI API key | used for speech-to-text and transcript cleanup |
| Your lecture files | the video (`.mp4`), the slides exported as PDF, and the original `.pptx` |

Installing ffmpeg: Windows `winget install ffmpeg`, macOS `brew install ffmpeg`, Debian/Ubuntu `sudo apt install ffmpeg`.

## Quick start

**1. Get the code and create a virtual environment**

```bash
git clone <this repository's URL>
cd lecture_video-to-text
python -m venv .venv
```

**2. Activate it and install the dependencies**

| Windows (PowerShell) | macOS / Linux |
| --- | --- |
| `.venv\Scripts\Activate.ps1` | `source .venv/bin/activate` |

```bash
pip install -r requirements.txt
```

**3. Add your API key**

Copy `.env.example` to `.env` (Windows: `copy .env.example .env`) and replace `sk-your-scads-ai-key` with your key. `.env` is git-ignored, so the key is never committed.

**4. Run it**

```bash
python lecture2doc.py lecture.mp4 slides.pdf slides.pptx -o lecture.docx
```

That is all. A long lecture takes a while (transcription and cleanup call the API); everything is cached, so if a run is interrupted, running the same command again continues where it stopped.

If you did not activate the environment, call its Python directly instead: `.venv/Scripts/python lecture2doc.py ...` (Windows) or `.venv/bin/python lecture2doc.py ...` (macOS/Linux).

## How it works

1. Video frames are sampled and compared with the PDF pages to find which slide is on screen when. Pillarboxed 4:3 slides in a 16:9 recording are handled, and the page number in the corner of the video identifies half-built animated slides.
2. The audio is cut at every slide change, and each stretch on one slide is transcribed on its own, so every word belongs to exactly one slide.
3. An LLM fixes recognition errors using a glossary built from the slide text. Guardrails fall back to the raw text if the output deviates too much.
4. The Word document is written with real heading styles, so the navigation pane and Ctrl+F work.

## Configuration

Set these in `.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCADS_API_KEY` | (required) | your ScaDS.AI API key |
| `SCADS_BASE_URL` | `https://llm.scads.ai/v1` | API endpoint |
| `SCADS_STT_MODEL` | `alias-stt` | speech-to-text model |
| `SCADS_LLM_MODEL` | `alias-huge-no-thinking` | model for transcript cleanup |
| `SCADS_GLOSSARY_MODEL` | same as `SCADS_LLM_MODEL` | optional separate model for the glossary step (e.g. a reasoning model) |
| `SCADS_REQUESTS_PER_MINUTE` | `25` | request cap; the gateway allowed 30 per key and model when this was written, lower it if your quota is smaller |

The API client is the standard OpenAI one, so another OpenAI-compatible service may work by changing the base URL and model names. This is untested.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests need ffmpeg on your `PATH` but no API key (API calls are mocked).

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `ffmpeg` not found | install it and make sure `ffmpeg -version` works in the same terminal |
| Missing or invalid API key | check that `.env` is in the folder you run the command from and contains `SCADS_API_KEY=...` |
| The `.docx` cannot be saved | it is open in Word; close it and run again with `--rebuild`, or pick another file with `-o` |
| Wrong slide on some pages, or many unmatched seconds | tune `--threshold` (0.5-0.8) and `--smooth` (3-9), or correct `segments.json` (see below); reruns are fast because transcription is cached |
| HTTP 429 (rate limit) | retried automatically; if it keeps happening, lower `SCADS_REQUESTS_PER_MINUTE` |

## Usage in detail

```bash
python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx -o lecture.docx
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
python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx --from-segments output/<stem>/segments.json
```

The audio is cut again at the edited times; only the clips whose cut times changed are transcribed again (and cleaned again by the LLM), everything else comes from the caches.

## How slides are recognised

Every video frame is compared with the PDF pages. Slides that are built up by animation are the weak spot of that: a half-built slide hardly resembles its finished PDF page and may look more like another, emptier page. So, when the PDF prints page numbers (usually bottom right), the tool also compares the number area of each frame with the number area of every PDF page (no OCR; the PDF supplies how the numbers look). Where the corner clearly shows a page's number, that page wins over the whole-slide match; pages that print the same number are told apart by the picture. If the PDF has no usable page numbers, or the corner is not visible, matching falls back to whole slides. The matching summary says how many frames the number could be read in and how many frames it changed. `--no-slide-numbers` switches this off. It costs one more pass over the video.

## How the transcript is split between slides

The lectures are recorded slide by slide, so at every slide change the audio has a silent gap between two recordings (in the sample lecture a median of 2.5 s, against 0.2 s for ordinary pauses). After matching, the tool finds these gaps with ffmpeg's silence detector and cuts the audio in the middle of the longest gap (at least 0.5 s) within 3 seconds of every slide change. The clips on either side of a gap keep only a quarter of a second of that silence (likewise silence at the very start and end of the recording), because speech-to-text models invent text such as "Thank you." for long silent stretches. Each stretch on one slide is transcribed separately, so a sentence can never be split between two slides and nothing depends on word timestamps (the speech-to-text service only estimates those, evenly spread over each phrase, so they can put words on the wrong side of a slide change).

If a slide change has no silent gap nearby, as in a lecture recorded in one go, that change is cut exactly at the detected slide change and the summary says so: a word spoken right at such a change may then land on the neighbouring slide. A clip with less than half a second of speech is not sent to the speech-to-text service at all (silence makes the model invent text), and it gets an empty transcript.

The speech-to-text requests share the `SCADS_REQUESTS_PER_MINUTE` quota with the LLM requests, and each finished clip is cached at once.

## Rebuild the document offline

If the `.docx` is open in Word it cannot be overwritten: the tool then says so, and everything else (`segments.json`, the caches) is already saved. Close the file and run the same command with `--rebuild`, or choose another file with `-o`.

Layout options (`--no-timestamps`, `--no-page-breaks`, `--no-thumbnails`, `--keep-raw`) and the slide titles only affect the Word file, so you never need to transcribe again to change them. `--rebuild` regenerates the `.docx` from the saved `segments.json` (which holds the cleaned and raw text and the times) plus the PDF/PPTX, in seconds, without video, ffmpeg, API key or network:

```bash
python lecture2doc.py VIDEO.mp4 SLIDES.pdf SLIDES.pptx --rebuild
```

It reads `<workdir>/segments.json` (or the file given with `--from-segments`), overwrites the `.docx` and never touches `segments.json` or the caches. The video path is only used to name the output. You can hand-edit `clean` text or slide/time spans in `segments.json` first.

## Known limits

- The tool assumes a single speaker: there is no diarization and no speaker labels. No summaries either; the output is the transcript only.
- Slide matching relies on the video showing the slides; talking-head-only stretches are attached to the neighbouring slide.
- LLM cleanup can occasionally misjudge a term; use `--keep-raw` to compare, or `--no-cleanup`.
- The API can be slow; a full lecture's cleanup may take a while. The LLM requests are throttled to `SCADS_REQUESTS_PER_MINUTE`, and a rate-limit error (HTTP 429) is retried after the server's `Retry-After` time or, without one, after 15/30/60 seconds. Results are cached, so an interrupted or partly failed run only repeats the missing calls.

## License

[MIT](LICENSE)
