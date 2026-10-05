import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from lecture2doc import pipeline, ui


def test_a_video_is_paired_with_the_pdf_of_the_same_name():
    pdfs = ["Intro to GIS.pdf", "week_02-maps.pdf", "other.pdf"]
    assert ui.pair_pdf("intro_to_gis", pdfs) == "Intro to GIS.pdf"
    assert ui.pair_pdf("Week 02 Maps", pdfs) == "week_02-maps.pdf"
    assert ui.pair_pdf("week_02-maps (recording)", pdfs) == "week_02-maps.pdf"  # close enough
    assert ui.pair_pdf("completely different", pdfs) is None
    assert ui.pair_pdf("x", []) is None


def test_files_are_listed_in_natural_order_ignoring_case(tmp_path):
    for name in ("L-GDI-10.mp4", "L-Gdi-2.mp4", "l-gdi-1.mp4", "L-GDI-2-b.mp4", "x.txt"):
        (tmp_path / name).write_bytes(b"x")
    assert ui.list_files(str(tmp_path), {".mp4"}) == ["l-gdi-1.mp4", "L-GDI-2-b.mp4", "L-Gdi-2.mp4", "L-GDI-10.mp4"]


def test_a_video_named_by_its_number_finds_the_pdf_that_continues_its_name():
    pdfs = ["L-GDI-5-Methods-1-Architectures.pdf", "L-GDI-5-Methods-3-Encoding.pdf"]
    assert ui.pair_pdf("L-Gdi-5-Methods-1", pdfs) == "L-GDI-5-Methods-1-Architectures.pdf"
    assert ui.pair_pdf("L-GDI-5-Methods-3-Encoding_subset", pdfs) == "L-GDI-5-Methods-3-Encoding.pdf"


@pytest.fixture
def app(tmp_path):
    pdfs, videos = tmp_path / "pdf", tmp_path / "vid"
    pdfs.mkdir(), videos.mkdir()
    for name in ("one.pdf", "two.pdf", "notes.txt"):
        (pdfs / name).write_bytes(b"x")
    for name in ("one.mp4", "Two.MKV", "readme.md"):
        (videos / name).write_bytes(b"x")
    a = ui.App(tmp_path / "settings.json")
    a.set_folders({"pdf_dir": str(pdfs), "video_dir": str(videos), "out_dir": str(tmp_path / "out")})
    return a


    assert list(rows) == ["one.mp4", "Two.MKV"]  # natural order, ignoring case
    rows = {r["video"]: r for r in app.lectures()}
    assert sorted(rows) == ["one.mp4", "Two.MKV"]
    assert rows["one.mp4"]["pdf"] == "one.pdf" and rows["Two.MKV"]["pdf"] == "two.pdf"
    app.set_pair("one.mp4", "two.pdf")
    assert {r["video"]: r["pdf"] for r in app.lectures()}["one.mp4"] == "two.pdf"


def test_settings_survive_a_restart(app, tmp_path):
    app.set_options({"cleanup": False, "language": " de "})
    again = ui.App(tmp_path / "settings.json")
    assert again.settings["options"]["cleanup"] is False and again.settings["options"]["language"] == "de"
    assert again.settings["pdf_dir"] == app.settings["pdf_dir"]


def wait_for(app, video, status, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if app.jobs[video]["status"] == status:
            return
        time.sleep(0.02)
    raise AssertionError(app.jobs[video])


def test_a_queued_lecture_runs_in_the_background_and_reports_progress(app, monkeypatch):
    seen = {}

    def fake_run(job, reporter):
        seen["job"] = job
        reporter.plan(["slides", "writing"])
        reporter.stage("slides", 0.5, "half")
        reporter.log("hello")
        return job.output

    monkeypatch.setattr(pipeline, "run", fake_run)
    app.enqueue(["one.mp4"])
    wait_for(app, "one.mp4", "done")
    job = app.jobs["one.mp4"]
    assert job["log"] == ["hello"] and job["overall"] == 1.0
    assert seen["job"].pdf.name == "one.pdf" and seen["job"].output.name == "one.docx"
    assert seen["job"].workdir.parts[-2:] == (".lecture2doc", "one")
    assert seen["job"].options.cleanup is True and seen["job"].options.language is None


def test_a_failure_is_shown_not_raised_and_a_cancel_stops_the_run(app, monkeypatch):
    def boom(job, reporter):
        raise pipeline.PipelineError("the PDF is broken")

    monkeypatch.setattr(pipeline, "run", boom)
    app.enqueue(["one.mp4"])
    wait_for(app, "one.mp4", "error")
    assert app.jobs["one.mp4"]["error"] == "the PDF is broken"

    def slow(job, reporter):
        while True:
            reporter.stage("slides", 0.1)
            time.sleep(0.01)

    monkeypatch.setattr(pipeline, "run", slow)
    app.enqueue(["one.mp4"])
    wait_for(app, "one.mp4", "running")
    app.cancel("one.mp4")
    wait_for(app, "one.mp4", "cancelled")


def test_a_video_without_slides_is_not_started(app):
    app.set_pair("one.mp4", "")
    (app.settings.get("pairs"))["one.mp4"] = ""
    app.set_folders({"pdf_dir": str(app.settings["out_dir"])})  # a folder without PDFs
    app.enqueue(["one.mp4"])
    assert app.jobs["one.mp4"]["status"] == "error" and "No slide PDF" in app.jobs["one.mp4"]["error"]


def test_the_glossary_is_shared_by_the_folder_and_built_only_once(app, tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(pipeline, "make_glossary", lambda pdf, on_progress=None: built.append(pdf.name) or "fresh")
    assert app.glossary("one.mp4") == "fresh" and built == ["one.pdf"]
    app.save_glossary("one.mp4", "alpha\n\n beta \n")
    assert app.glossary("one.mp4") == "alpha\nbeta\n" and built == ["one.pdf"]  # read from the file now
    assert (tmp_path / "pdf" / ".lecture2doc" / "glossary.txt").is_file()
    assert app.glossary("Two.MKV") == "alpha\nbeta\n"  # another lecture, another PDF: the same glossary
    assert all(r["has_glossary"] for r in app.lectures())
    assert app.glossary("one.mp4", generate=True) == "fresh" and built == ["one.pdf", "one.pdf"]
    app.reset("one.mp4")  # starting over keeps the shared glossary
    assert all(r["has_glossary"] for r in app.lectures())


@pytest.fixture
def server(app):
    srv = ui.make_server(app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def call(server, path, body=None, host=None, origin=None):
    port = server.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Host", host or f"127.0.0.1:{port}")
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_the_page_and_the_state_are_served(server):
    status, page = call(server, "/")
    assert status == 200 and b"lecture2doc" in page
    status, data = call(server, "/api/state")
    state = json.loads(data)
    assert status == 200 and [l["video"] for l in state["lectures"]] == ["one.mp4", "Two.MKV"]


def test_requests_from_other_websites_are_refused(server):
    assert call(server, "/api/state", host="evil.example")[0] == 403
    assert call(server, "/api/folders", {"out_dir": "x"}, origin="http://evil.example")[0] == 403


def test_unknown_videos_and_routes_are_rejected(server):
    assert call(server, "/api/glossary?video=nope.mp4")[0] == 400
    assert call(server, "/api/run", {"videos": ["../../etc/passwd"]})[0] == 400
    assert call(server, "/api/nothing", {})[0] == 404


def test_the_glossary_progress_is_served(server):
    status, data = call(server, "/api/glossary/progress")
    assert status == 200 and json.loads(data) == {"decks_done": 0, "decks_total": 0}
