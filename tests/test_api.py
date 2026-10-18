import time
from pathlib import Path

import pytest
from PIL import Image

from app.config import load_user_config
from app.models import Settings


def test_health_and_defaults(api):
    health = api.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert "ffmpeg" in body
    assert "avif" in body
    assert body["version"]
    defaults = api.get("/api/defaults").json()
    assert defaults["settings"]["image_format"] == "webp"
    assert defaults["settings"]["image_quality"] == 82
    assert defaults["settings"]["max_width"] == 2560


def test_server_stays_on_loopback():
    from app.__main__ import HOST

    assert HOST == "127.0.0.1"


def test_invalid_config_falls_back(tmp_path: Path):
    broken = tmp_path / "config.json"
    broken.write_text("{not json", encoding="utf-8")
    settings, output = load_user_config(broken)
    assert settings == Settings()
    assert output is None

    custom = tmp_path / "custom.json"
    custom.write_text('{"image_quality": 70, "output_dir": "finished", "_comment": "note"}', encoding="utf-8")
    settings, output = load_user_config(custom)
    assert settings.image_quality == 70
    assert settings.image_format == "webp"
    assert output == "finished"


def test_batch_reports_each_file_and_keeps_going(api, tmp_path: Path):
    good = tmp_path / "photo.png"
    Image.new("RGB", (80, 40), (20, 120, 80)).save(good)
    bad = tmp_path / "notes.txt"
    bad.write_text("hello", encoding="utf-8")
    original = good.read_bytes()
    added = api.post("/api/paths", json={"paths": [str(bad), str(good)]})
    assert added.status_code == 200
    files = added.json()["files"]
    assert {item["name"]: item["status"] for item in files} == {"notes.txt": "error", "photo.png": "ready"}

    output = tmp_path / "web"
    started = api.post(
        "/api/jobs",
        json={
            "file_ids": [item["id"] for item in files],
            "output_dir": str(output),
            "settings": {"skip_suitable": False, "image_quality": 80},
        },
    )
    assert started.status_code == 200
    finished = _wait(api, started.json()["id"])
    assert finished["status"] == "completed"
    by_name = {item["name"]: item for item in finished["files"]}
    assert by_name["notes.txt"]["status"] == "error"
    assert by_name["photo.png"]["status"] == "completed"
    assert by_name["photo.png"]["output_size"] > 0
    assert by_name["photo.png"]["width"] == 80
    assert (output / by_name["photo.png"]["output_name"]).is_file()
    assert good.read_bytes() == original
    assert finished["summary"]["failed"] == 1
    assert finished["summary"]["completed"] == 1

    opened = api.post(f"/api/jobs/{finished['id']}/open")
    assert opened.status_code == 200


def test_upload_quality_guard_and_discard(api, tmp_path: Path):
    source = tmp_path / "dot.png"
    Image.new("RGB", (16, 16), "blue").save(source)
    with source.open("rb") as handle:
        uploaded = api.post("/api/upload", files=[("files", ("dot.png", handle, "image/png"))])
    assert uploaded.status_code == 200
    file_id = uploaded.json()["files"][0]["id"]
    rejected = api.post("/api/jobs", json={"file_ids": [file_id], "settings": {"image_quality": 0}})
    assert rejected.status_code == 422
    assert isinstance(rejected.json()["detail"], str)

    started = api.post(
        "/api/jobs",
        json={"file_ids": [file_id], "output_dir": str(tmp_path / "out"), "settings": {"skip_suitable": False}},
    )
    finished = _wait(api, started.json()["id"])
    task = finished["files"][0]
    assert task["status"] == "completed"
    output = Path(finished["output_dir"]) / task["output_name"]
    assert output.is_file()

    from app.jobs import get_manager

    manager = get_manager()
    job = manager.jobs[finished["id"]]
    stored = job.tasks[0]
    outside = tmp_path / "dot.png"
    stored.decision = "pending"
    stored.output_path = str(outside)
    with pytest.raises(RuntimeError, match="outside the output folder"):
        manager.decide(finished["id"], file_id, "discard")
    assert outside.is_file()

    stored.decision = "pending"
    stored.larger = True
    stored.output_path = str(output)
    stored.status = "completed"
    discarded = api.post(f"/api/jobs/{finished['id']}/files/{file_id}/discard")
    assert discarded.status_code == 200
    assert discarded.json()["files"][0]["status"] == "discarded"
    assert not output.exists()


def test_folder_scan_skips_unrelated_files(manager, tmp_path: Path):
    folder = tmp_path / "album"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(nested / "one.png")
    (folder / "readme.txt").write_text("nope", encoding="utf-8")
    (folder / ".hidden.png").write_bytes(b"hidden")
    added = manager.add_paths([str(folder)])
    names = [item["name"] for item in added["files"]]
    assert names == ["one.png"]


def _wait(api, job_id: str, timeout: float = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = api.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        data = response.json()
        if data["status"] in {"completed", "cancelled", "error"}:
            return data
        time.sleep(0.05)
    raise AssertionError("The batch did not finish.")
