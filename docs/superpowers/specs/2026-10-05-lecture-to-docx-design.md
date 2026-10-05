# Lecture video to per-slide transcript (Word) — Design

## Goal
Students want to skim a lecture or search it for terms without watching the recording. Given a lecture MP4 (slides + lecturer audio) and the slide deck (PPTX + PDF), produce a Word document with one section per slide: slide number, title (if any), and the transcript of what was said while that slide was shown.

## Inputs / Output
- Inputs: `lecture.mp4`, `slides.pdf`, `slides.pptx`
- Output: `lecture.docx` (+ `segments.json` debug/cache file next to it)
- CLI: `python lecture2doc.py lecture.mp4 slides.pdf slides.pptx -o lecture.docx`
  - `--language CODE` (default: auto-detect)
  - `--no-cleanup` skip LLM cleanup
  - `--keep-raw` also include the raw transcript in the docx
  - `--no-thumbnails` omit slide images
  - `--from-segments segments.json` reuse a (possibly hand-corrected) slide-to-time mapping, skipping frame matching

## Configuration (`.env`, same names as `scads_transcribe`)
- `SCADS_API_KEY`, `SCADS_BASE_URL` (`https://llm.scads.ai/v1`), `SCADS_STT_MODEL` (`alias-stt`)
- `SCADS_LLM_MODEL` — per-span cleanup (default `alias-huge-no-thinking`)
- `SCADS_GLOSSARY_MODEL` — glossary building (default: value of `SCADS_LLM_MODEL`)
- The key is only read from `.env`; never logged or copied. `.env` is git-ignored; `.env.example` is committed.

## Pipeline & modules
1. **`slides.py`** — render PDF pages to images (PyMuPDF); extract per-slide title and text from PPTX (python-pptx). Title = title placeholder, else first text line, else none. PDF text is the fallback when PPTX text is missing.
2. **`segment.py`** — ffmpeg extracts 1 frame/s. Each frame is matched against rendered slides (perceptual hash + structural similarity, tolerant of compression, scaling, webcam overlays); best match wins above a confidence threshold. Temporal smoothing removes 1-frame glitches; consecutive frames on the same slide merge into one span, so animation builds stay in one span. Stretches below threshold become "no slide match" spans (speech attached to the surrounding section). Output: ordered list of `{slide, start, end, confidence}`.
3. **`transcribe.py`** — ffmpeg extracts 16 kHz mono audio, split into 5-minute chunks; each chunk sent to `alias-stt` with word timestamps (client and retry/backoff adapted from `scads_transcribe`; falls back to segment-level timing if word timestamps are rejected). Words are assigned to the span containing their start time.
4. **`cleanup.py`** — optional LLM stage:
   - Build a glossary (terms, names, acronyms, topics) and a deck outline (number + title) from all slide text with `SCADS_GLOSSARY_MODEL`; if the deck is too large for one call, build per section and merge.
   - For each span, send glossary, outline, current + neighbouring slide text, and the raw transcript to `SCADS_LLM_MODEL` at temperature 0. Instruction: fix misrecognised terminology, punctuation, obvious fillers; do not add, remove, summarise or reorder content.
   - Guardrails: if cleaned length differs from raw by more than ~25%, or the call fails after retries, use the raw text and print a warning. Raw text always preserved in `segments.json`. Strip any `<think>` blocks from output.
   - Spans run concurrently with a small limit; results cached on disk keyed by a hash of (model, prompt inputs) so reruns do not repeat calls.
5. **`docx_out.py`** — python-docx. Per slide: heading `Slide N – Title` (number only if no title), optional thumbnail, transcript paragraphs. If a slide is revisited, later spans are appended as further paragraphs under the same heading, each prefixed with its time (`[mm:ss]`). Slides with no speech are included with `(no speech)` so numbering stays contiguous. Real Word heading styles are used so the navigation pane and Ctrl+F work.

## Error handling
- Missing tools/inputs (ffmpeg, files, API key) fail fast with a clear message.
- API calls use retry with backoff; STT failures after retries abort (partial results are not silently dropped); cleanup failures degrade to raw text per span.
- A summary prints matched/unmatched seconds per slide so poor matching is visible.

## Testing
- Unit tests (pytest) with synthetic frames and slide images: matching, smoothing/merging, revisit handling, word-to-span assignment, cleanup guardrails (mocked LLM), docx structure.
- One manual end-to-end run on a short real clip.

## Out of scope (v1)
Diarization, GUI, summaries, PDF/Markdown output, auto-correction of slide matching beyond the `segments.json` workflow.

## Open items
- Confidence thresholds and smoothing window to be tuned on a real lecture.
