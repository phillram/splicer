"""Remembering each video's parts."""

from __future__ import annotations

from splicer import projects


def test_save_load_forget(tmp_path):
    video = tmp_path / "v.mkv"
    video.write_bytes(b"x" * 10)
    folder = tmp_path / "projects"
    assert projects.load(video, folder) is None
    projects.save(video, {"parts": [["A", 0, 1, True]], "tracks": {"0": True}}, folder)
    assert projects.load(video, folder)["parts"] == [["A", 0, 1, True]]
    video.write_bytes(b"y" * 11)  # a different file now: not the same video
    assert projects.load(video, folder) is None
    video.write_bytes(b"x" * 10)
    projects.forget(video, folder)
    assert projects.load(video, folder) is None
