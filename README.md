# lecture2doc

Turn recorded lectures into Word documents you can skim and search.

Point it at a folder of **lecture videos** and a folder of the matching **slide PDFs**. For every lecture you get a `.docx` with one page per stretch of the lecture: the slide title, a thumbnail of the slide, the time it was on screen, and a cleaned-up transcript of what the lecturer said while it was shown.

```
lecture.mp4 + lecture.pdf   ->   lecture.docx
```

It has a simple browser interface (recommended) and a command line for scripting.

## What you need

| Requirement | Notes |
| --- | --- |
| Python 3.11 or newer | check with `python --version` |
| ffmpeg | must be on your `PATH`, check with `ffmpeg -version` |
| A ScaDS.AI API key | used for speech-to-text and transcript cleanup; [get one here](https://selfservice.tu-dresden.de/services/scads-llm-api/api-access/) |
| Your lectures | the videos (`.mp4`, `.mkv`, `.mov`, `.avi`, `.webm`, `.m4v`) and the slides of each exported as PDF |

Installing ffmpeg: Windows `winget install ffmpeg`, macOS `brew install ffmpeg`, Debian/Ubuntu `sudo apt install ffmpeg`.

## Setup

**1. Get the code and create a virtual environment**

```bash
git clone https://github.com/DxnnySch/lecture2doc.git
cd lecture2doc
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

**4. Start it**

```bash
python -m lecture2doc
```

Your browser opens the lecture2doc page (if not, open the address printed in the terminal, usually `http://127.0.0.1:8765/`). It only listens on your own computer. Stop it with `Ctrl+C` in the terminal.

If you did not activate the environment, call its Python directly instead: `.venv/Scripts/python -m lecture2doc` (Windows) or `.venv/bin/python -m lecture2doc` (macOS/Linux).

## Using the interface

The page has four numbered sections, top to bottom:

1. **Folders.** Choose the folder with your slide PDFs, the folder with your videos, and where the Word files should go (Browse… opens a folder dialog, or paste a path). The choice is remembered for next time.
2. **Glossary.** Press **Build glossary** once. It reads all PDFs in the slides folder and lists the technical terms, names and abbreviations of your course; the AI uses it to fix speech-recognition errors, which makes the transcripts much better. A counter shows how many PDFs are done. Processing is blocked until the glossary exists. Afterwards you can **Edit glossary** (see below). This section is hidden when the AI cleanup is switched off in section 4.
3. **Lectures.** Every video in the folder is listed (in natural order) with the PDF it was matched to by file name (for example `week3.mp4` and `week3.pdf`). If the match is wrong or missing, pick the right PDF from the drop-down. Tick the lectures you want and press **Process selected**. They run one after the other. Each lecture shows a progress bar with the current step (finding the slides in the video, transcribing, cleaning up the text, writing the document), how far it is, and how long it has been running; the bar at the bottom shows how many lectures of the batch are finished. *Show details* lists remarks such as how many seconds of video matched each slide. **Stop** ends the run at the next step; finished work is kept. When a lecture is done, **Open Word file** opens the document, and **Open output folder** opens the folder with all of them.
4. **Document options** (collapsed): AI cleanup on or off, thumbnails, time stamps, page breaks, grouping by slide, raw transcript, spoken language.

### Fixing mistakes: the glossary and re-running

There is **one glossary for the whole slides folder**, shared by all lectures: terms and abbreviations from one lecture are often used in another, also where they are not on the slide. Technical terms are exactly what speech recognition gets wrong, so a good glossary gives a better transcript.

- **Edit glossary** opens the list: **edit** any term, **remove** wrong ones with ✕, **add** missing ones (type and press Enter, for example an abbreviation the lecturer says but the slides do not show), and filter the list. **Save glossary** stores it in the slides folder (`.lecture2doc/glossary.txt`). *Rebuild from slides* starts over from the PDFs and replaces your edits (for example after you add new PDFs to the folder).
- Press **Process selected** again. Nothing that is already done is repeated: the slide matching and the speech-to-text are kept, and only the AI cleanup runs again with your glossary. A rerun after a glossary edit therefore takes only as long as the cleanup step instead of a full run.
- Changed only a document option (thumbnails, time stamps, ...)? Use **Rebuild file**: the document is made again from the saved transcript in seconds, without video or internet.
- **Start over** deletes everything saved for a lecture (transcript and caches) so the next run begins from scratch. The glossary is kept.

An interrupted or failed run (closed browser, lost connection, rate limit) also continues where it stopped when you press **Process selected** again.

### Where the files go

For a video called `week3.mp4` with the slides `week3.pdf`, and the output folder `out`:

| File | Meaning |
| --- | --- |
| `out/week3.docx` | the Word document |
| `<PDF folder>/.lecture2doc/glossary.txt` | the glossary shared by all lectures of that folder, one term per line (you can also edit it by hand) |
| `out/.lecture2doc/week3/segments.json` | which slide was shown when, with the raw and cleaned text |
| `out/.lecture2doc/week3/matching.json`, `clips.json`, `llm_cache.json` | caches of the slide matching, the speech-to-text and the AI cleanup, so reruns are cheap |

## How it works

1. Video frames are sampled and compared with the PDF pages to find which slide is on screen when. Pillarboxed 4:3 slides in a 16:9 recording are handled, and the page number in the corner of the video identifies half-built animated slides.
2. The audio is cut at every slide change, and each stretch on one slide is transcribed on its own, so every word belongs to exactly one slide.
3. An LLM (the fastest of the most powerful ones, chosen automatically) fixes recognition errors. The glossary is built once from the text of all the slide PDFs in the folder (and can be edited by you), and each stretch of transcript is corrected with that glossary, the list of all slide titles, and the text of the current, previous and next slide. Guardrails fall back to the raw text if the output deviates too much.
4. The Word document is written with real heading styles, so the navigation pane and Ctrl+F work.

Slide titles are taken from the PDF: the largest text in the upper part of each page, so footers and page numbers are ignored.

The document has one page per stretch of the lecture on one slide, in lecture order. Each page has a heading (`Slide N – Title`), a subtitle line with the time range it was on screen in the video (e.g. `12:30 – 15:02`), the slide's thumbnail and the transcript of that stretch. If the lecturer goes 24 → 25 → 24 → 23 → 26, that is five pages in exactly that order, even though slide 24 appears twice. Slides that were never shown get no page. With *Group by slide* (`--group-by-slide`) the document instead has one section per slide of the deck, in slide order, with all visits of a slide together (time-stamped paragraphs).

## Configuration

Set these in `.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCADS_API_KEY` | (required) | your ScaDS.AI API key |
| `SCADS_BASE_URL` | `https://llm.scads.ai/v1` | API endpoint |
| `SCADS_STT_MODEL` | `alias-stt` | speech-to-text model |
| `SCADS_LLM_MODEL` | `auto` | model for the transcript cleanup. `auto` = the fastest of the powerful models for every request (see below); set a model name to always use that one |
| `SCADS_GLOSSARY_MODEL` | same as `SCADS_LLM_MODEL` | model for the glossary step; same rule |
| `SCADS_LLM_MODELS` | the four models below | the models `auto` chooses from, comma separated |
| `SCADS_REQUESTS_PER_MINUTE` | `25` | request cap per model; the gateway allowed 30 per key and model when this was written, lower it if your quota is smaller |

### Automatic choice of the AI model

The ScaDS.AI gateway offers several strong models and their speed differs a lot (in a test between 0.8 s and 11 s for the same cleanup request). Unless you set `SCADS_LLM_MODEL`, lecture2doc handles this itself:

1. The API does not say how strong a model is, so the candidates are a short list of the most powerful chat models: `alias-huge-no-thinking`, `zai-org/GLM-5.3`, `MiniMaxAI/MiniMax-M3` and `openai/gpt-oss-120b` (change it with `SCADS_LLM_MODELS`). Models the API does not offer right now are dropped.
2. At the start of a glossary build or a cleanup, all candidates get the same small test request in parallel (about 10 seconds). A model that fails it or answers wrongly is not used.
3. Every following request goes to the model expected to answer first, based on its measured speed (kept up to date from the real answers) and on how many requests it is already working on, so busy models share the load. A model that returns an error or hits its rate limit is skipped for a minute and the next one answers.

The log under *Show details* lists the models by speed and how many requests each one answered. The speech-to-text model is not part of this (`SCADS_STT_MODEL`).

The API client is the standard OpenAI one, so another OpenAI-compatible service may work by changing the base URL and model names. This is untested.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| The page says to add your API key | create `.env` as described in step 3 of the setup, then reload the page |
| `ffmpeg` not found | install it and make sure `ffmpeg -version` works in the same terminal |
| A video has no PDF | the names are too different; choose the PDF in the drop-down of that lecture |
| The AI always uses one model although you expect the automatic choice | remove or comment out `SCADS_LLM_MODEL` (and `SCADS_GLOSSARY_MODEL`) in `.env`: a model named there is always used |
| *Failed: ... open in Word* | the `.docx` is open in Word; close it and process the lecture again (nothing is transcribed twice) |
| Wrong slide on some pages, or many unmatched seconds | tune `--threshold` and `--smooth` or correct `segments.json` on the command line (see below) |
| HTTP 429 (rate limit) | retried automatically; if it keeps happening, lower `SCADS_REQUESTS_PER_MINUTE` |
| *Browse…* does nothing | your Python has no Tk dialog; paste the folder path into the field instead |
| Port 8765 is in use | lecture2doc takes the next free port and prints its address |

## Command line

The same pipeline without the interface, for scripts and fine tuning:

```bash
python -m lecture2doc VIDEO.mp4 SLIDES.pdf -o lecture.docx
```

Without arguments, `python -m lecture2doc` starts the interface. Intermediate files go to the work directory (default `output/<video stem>/`) and are the same as listed above. The glossary is saved in the PDF's folder (`.lecture2doc/glossary.txt`) and built from all PDFs in that folder, so keep one course per folder: edit the file between runs to change the glossary.

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
| `--rebuild` | rebuild the `.docx` from an existing `segments.json` only: no video, ffmpeg, API key or API calls |
| `--from-segments FILE` | reuse the slide/time spans from a (hand-corrected) `segments.json` |
| `--interval` | seconds between sampled frames (default 1.0) |
| `--threshold` | minimum match score for a frame to count (default 0.6) |
| `--smooth` | majority-vote window in frames (default 5) |
| `--no-slide-numbers` | do not use the page number in the corner of the video to identify slides |

A summary after matching lists seconds matched per slide, the unmatched percentage, and slides never shown. Many unmatched seconds or flicker between neighbouring slides: tune `--threshold` (0.5-0.8) and `--smooth` (3-9); this reruns quickly because the transcription is cached.

### Correcting the slide assignment

`segments.json` holds every span (`slide`, `start`, `end`, `confidence`, `audio_start`, `audio_end`, `raw`, `clean`). `start`/`end` are when the slide was on screen; `audio_start`/`audio_end` are where the audio was cut for its transcript. If a slide assignment is wrong, edit the `slide`/`start`/`end` values and regenerate with the corrected spans:

```bash
python -m lecture2doc VIDEO.mp4 SLIDES.pdf --from-segments output/<stem>/segments.json
```

The audio is cut again at the edited times; only the clips whose cut times changed are transcribed again (and cleaned again by the LLM), everything else comes from the caches.

### Rebuilding the document offline

Layout options and the slide titles only affect the Word file, so you never need to transcribe again to change them. `--rebuild` regenerates the `.docx` from the saved `segments.json` plus the PDF, in seconds, without video, ffmpeg, API key or network:

```bash
python -m lecture2doc VIDEO.mp4 SLIDES.pdf --rebuild
```

It reads `<workdir>/segments.json` (or the file given with `--from-segments`), overwrites the `.docx` and never touches `segments.json` or the caches. The video path is only used to name the output. You can hand-edit `clean` text or slide/time spans in `segments.json` first.

## How slides are recognised

Every video frame is compared with the PDF pages. Slides that are built up by animation are the weak spot of that: a half-built slide hardly resembles its finished PDF page and may look more like another, emptier page. So, when the PDF prints page numbers (usually bottom right), the tool also compares the number area of each frame with the number area of every PDF page (no OCR; the PDF supplies how the numbers look). Where the corner clearly shows a page's number, that page wins over the whole-slide match; pages that print the same number are told apart by the picture. If the PDF has no usable page numbers, or the corner is not visible, matching falls back to whole slides. `--no-slide-numbers` switches this off. It costs one more pass over the video.

## How the transcript is split between slides

The lectures are recorded slide by slide, so at every slide change the audio has a silent gap between two recordings (in the sample lecture a median of 2.5 s, against 0.2 s for ordinary pauses). After matching, the tool finds these gaps with ffmpeg's silence detector and cuts the audio in the middle of the longest gap (at least 0.5 s) within 3 seconds of every slide change. The clips on either side of a gap keep only a quarter of a second of that silence (likewise silence at the very start and end of the recording), because speech-to-text models invent text such as "Thank you." for long silent stretches. Each stretch on one slide is transcribed separately, so a sentence can never be split between two slides and nothing depends on word timestamps (the speech-to-text service only estimates those, evenly spread over each phrase, so they can put words on the wrong side of a slide change).

If a slide change has no silent gap nearby, as in a lecture recorded in one go, that change is cut exactly at the detected slide change and the details say so: a word spoken right at such a change may then land on the neighbouring slide. A clip with less than half a second of speech is not sent to the speech-to-text service at all (silence makes the model invent text), and it gets an empty transcript.

The speech-to-text requests share the `SCADS_REQUESTS_PER_MINUTE` quota with the LLM requests, and each finished clip is cached at once.

## Known limits

- The tool assumes a single speaker: there is no diarization and no speaker labels. No summaries either; the output is the transcript only.
- Slide matching relies on the video showing the slides; talking-head-only stretches are attached to the neighbouring slide.
- LLM cleanup can occasionally misjudge a term; use *Also include the raw transcript* (`--keep-raw`) to compare, or turn the cleanup off.
- The API can be slow; a full lecture's cleanup may take a while. The LLM requests are throttled to `SCADS_REQUESTS_PER_MINUTE`, and a rate-limit error (HTTP 429) is retried after the server's `Retry-After` time or, without one, after 15/30/60 seconds. Results are cached, so an interrupted or partly failed run only repeats the missing calls.
- The slide matching step has no fine-grained progress: for a long lecture the bar moves slowly for a few minutes while the video is read.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests need ffmpeg on your `PATH` but no API key (API calls are mocked).

## License

[MIT](LICENSE)
