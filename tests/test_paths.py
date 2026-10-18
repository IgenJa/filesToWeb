from pathlib import Path

from app.display import format_bytes, savings
from app.paths import safe_stem, unique_output_path


def test_safe_stem_removes_characters_windows_rejects():
    assert safe_stem("bad<name>?.jpg") == "bad_name__"
    assert safe_stem("CON.jpg") == "file-CON"
    assert safe_stem("   ") == "file"
    assert safe_stem("-secret.png") == "file-secret"


def test_unique_output_path_never_replaces_the_original(tmp_path: Path):
    source = tmp_path / "photo.webp"
    source.write_bytes(b"original")
    chosen = unique_output_path(tmp_path, "photo", ".webp", source)
    assert chosen != source
    assert chosen.name == "photo-2.webp"
    chosen.write_bytes(b"new")
    second = unique_output_path(tmp_path, "photo", ".webp", source)
    assert second.name == "photo-3.webp"
    assert source.read_bytes() == b"original"


def test_savings_and_byte_labels():
    saved, percent, larger = savings(1000, 250)
    assert saved == 750
    assert percent == 75
    assert larger is False
    saved, percent, larger = savings(1000, 1200)
    assert larger is True
    assert saved < 0
    assert format_bytes(1536).endswith("KB")
