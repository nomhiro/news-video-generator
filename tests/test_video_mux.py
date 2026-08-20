"""音声多重化ステップ（`mux_audio`）の検証。

`VideoComposer._run_ffmpeg` の第2段（無音の映像に音声を混ぜるだけ）を
モジュール関数として切り出したもの。Remotion レンダラ（別タスク）も同じ関数を
呼ぶため、コピーではなく共有にする必要がある。
"""

import pytest

from src.generators.video_composer import VideoCompositionError, mux_audio


def test_mux_audio_builds_a_copy_command(monkeypatch, tmp_path) -> None:
    """映像は再エンコードしないこと。

    第2段が -c:v copy でなければ、1段で合成していた頃の
    「マクサーが映像パケットを溜め込んで OOM」が再発する。
    """
    recorded: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr("src.generators.video_composer.subprocess.run", fake_run)
    mux_audio(
        tmp_path / "silent.mp4",
        tmp_path / "voice.mp3",
        tmp_path / "out.mp4",
        timeout_sec=900,
    )

    cmd = recorded[0]
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-shortest" in cmd


def test_mux_audio_reports_the_exit_code(monkeypatch, tmp_path) -> None:
    """終了コードを必ず残すこと。負の値はシグナルで殺されたことを意味する。"""
    import subprocess

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(-9, cmd, output="", stderr="killed")

    monkeypatch.setattr("src.generators.video_composer.subprocess.run", fake_run)
    with pytest.raises(VideoCompositionError, match="-9"):
        mux_audio(
            tmp_path / "silent.mp4",
            tmp_path / "voice.mp3",
            tmp_path / "out.mp4",
            timeout_sec=900,
        )
