"""自動投稿の有効/無効。"""

from pathlib import Path

from src.social.switch import PostingSwitch


def test_ファイルが無ければ既定値を返す(tmp_path: Path) -> None:
    switch = PostingSwitch(tmp_path / "x_posting.json", default_enabled=False)

    assert switch.is_enabled() is False


def test_画面から有効にできる(tmp_path: Path) -> None:
    path = tmp_path / "x_posting.json"
    switch = PostingSwitch(path, default_enabled=False)

    switch.set_enabled(True)

    assert switch.is_enabled() is True
    # 別のインスタンス（= 別プロセス）からも見える
    assert PostingSwitch(path, default_enabled=False).is_enabled() is True


def test_一度切り替えたら_既定値より_ファイルが優先される(tmp_path: Path) -> None:
    """既定値は「ファイルが無いときの初期値」でしかない。

    デプロイのたびに既定値へ戻ると、画面で有効にした翌日に
    黙って投稿が止まる。
    """
    path = tmp_path / "x_posting.json"
    PostingSwitch(path, default_enabled=False).set_enabled(True)

    assert PostingSwitch(path, default_enabled=False).is_enabled() is True


def test_壊れたファイルは既定値として扱う(tmp_path: Path) -> None:
    """壊れた JSON で画面が 500 になると、止めることも直すこともできない。"""
    path = tmp_path / "x_posting.json"
    path.write_text("{broken", encoding="utf-8")

    assert PostingSwitch(path, default_enabled=False).is_enabled() is False
