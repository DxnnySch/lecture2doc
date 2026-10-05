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
    app.save_glossary("term")  # the cleanup needs the glossary
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
    app.save_glossary("term")  # the cleanup needs the glossary
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


def test_a_video_without_slides_is_not_started(app, tmp_path):
    empty = tmp_path / "empty"  # a slides folder without PDFs
    empty.mkdir()
    app.set_folders({"pdf_dir": str(empty)})
    app.save_glossary("term")  # the cleanup needs the glossary
    app.enqueue(["one.mp4"])
    assert app.jobs["one.mp4"]["status"] == "error" and "No slide PDF" in app.jobs["one.mp4"]["error"]


def test_the_glossary_is_one_file_in_the_slides_folder(app, tmp_path):
    assert app.glossary_state()["exists"] is False
    with pytest.raises(pipeline.PipelineError, match="not built"):
        app.glossary_text()
    app.save_glossary("alpha\n\n beta \n")
    assert app.glossary_text() == "alpha\nbeta\n"
    assert (tmp_path / "pdf" / ".lecture2doc" / "glossary.txt").is_file()
    assert app.glossary_state()["exists"] is True and app.glossary_state()["terms"] == 2
    app.enqueue(["one.mp4"], rebuild=True)  # not blocked; the glossary exists anyway
    app.reset("one.mp4")  # starting over keeps the glossary
    assert app.glossary_state()["exists"] is True


def test_the_glossary_is_built_in_the_background_with_progress(app, tmp_path, monkeypatch):
    def fake(folder, on_progress=None, models=None, say=None):
        on_progress(1, 4, 2, 5)
        pipeline.save_glossary(folder, "term")
        return "term"

    monkeypatch.setattr(pipeline, "make_glossary", fake)
    app.build_glossary()
    end = time.time() + 5
    while app.glossary_state()["building"] and time.time() < end:
        time.sleep(0.02)
    state = app.glossary_state()
    assert state["exists"] and state["error"] is None
    assert state["progress"] == {"decks_done": 1, "decks_total": 4, "parts_done": 2, "parts_total": 5}


def test_a_failed_glossary_build_is_reported(app, monkeypatch):
    def boom(folder, on_progress=None, models=None, say=None):
        raise pipeline.PipelineError("the API key is wrong")

    monkeypatch.setattr(pipeline, "make_glossary", boom)
    app.build_glossary()
    end = time.time() + 5
    while app.glossary_state()["building"] and time.time() < end:
        time.sleep(0.02)
    assert app.glossary_state()["error"] == "the API key is wrong" and not app.glossary_state()["exists"]


def test_processing_with_cleanup_needs_the_glossary_first(app):
    with pytest.raises(pipeline.PipelineError, match="Build the glossary first"):
        app.enqueue(["one.mp4"])
    assert "one.mp4" not in app.jobs
    app.set_options({"cleanup": False})  # no cleanup, no glossary needed
    app.enqueue(["one.mp4"])
    assert "one.mp4" in app.jobs


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
    assert call(server, "/api/glossary")[0] == 400  # not built yet
    assert call(server, "/api/run", {"videos": ["../../etc/passwd"]})[0] == 400
    assert call(server, "/api/nothing", {})[0] == 404


def test_the_state_tells_whether_the_glossary_exists(server):
    state = json.loads(call(server, "/api/state")[1])
    assert state["glossary"]["exists"] is False and state["glossary"]["building"] is False


def test_models_are_chosen_per_task_and_passed_on(app, monkeypatch):
    assert app.settings["models"] == {"glossary": [], "cleanup": [], "transcribe": []}
    app.set_models("cleanup", ["a", "b", "a"])
    app.set_models("transcribe", ["w"])
    with pytest.raises(pipeline.PipelineError, match="unknown task"):
        app.set_models("nothing", ["a"])
    with pytest.raises(pipeline.PipelineError, match="list of names"):
        app.set_models("cleanup", "a")
    row = app._row("one.mp4")
    options = app._job(row).options
    assert options.models == {"glossary": [], "cleanup": ["a", "b"], "transcribe": ["w"]}
    seen = {}

    def fake(folder, on_progress=None, models=None, say=None):
        seen["models"] = models
        say("Testing…")
        pipeline.save_glossary(folder, "t")
        return "t"

    monkeypatch.setattr(pipeline, "make_glossary", fake)
    app.set_models("glossary", ["g1", "g2"])
    app.build_glossary()
    end = time.time() + 5
    while app.glossary_state()["building"] and time.time() < end:
        time.sleep(0.02)
    assert seen["models"] == ["g1", "g2"] and app.glossary_state()["phase"] == "Testing…"
    again = ui.App(app.settings_file)
    assert again.settings["models"]["cleanup"] == ["a", "b"]  # remembered


def test_the_list_of_models_comes_from_the_api(app, tmp_path, monkeypatch):
    listing = [type("M", (), {"id": i, "mode": m, "max_input_tokens": 1000})
               for i, m in (("zai-org/GLM-5.3", "chat"), ("alias-stt", "audio_transcription"), ("flux", "image_generation"))]

    class Client:
        models = type("L", (), {"list": staticmethod(lambda: type("R", (), {"data": listing})())})()

    import lecture2doc.api as api
    from lecture2doc.config import load_config

    env = tmp_path / ".env"  # not the developer's own .env
    env.write_text("SCADS_API_KEY=k")
    monkeypatch.setattr(ui, "load_config", lambda: load_config(env))

    monkeypatch.setattr(api, "make_client", lambda cfg: Client())
    models = app.api_models()
    assert [m["id"] for m in models] == ["zai-org/GLM-5.3", "alias-stt"]  # only what the tasks can use
    assert models[0]["recommended"] is True and models[1]["recommended"] is False
    defaults = app.default_models()
    assert defaults["transcribe"] == ["alias-stt"] and "zai-org/GLM-5.3" in defaults["cleanup"]
