import argparse
import logging
from pathlib import Path

OWN_FILE_NAME = Path(__file__).stem
# ログ出力先はスクリプト位置基準で解決し、実行 cwd に依存しないようにする
LOG_DIR = Path(__file__).resolve().parent.parent / "logfiles"

logger = logging.getLogger(__name__)  # ファイルの名前を渡す
logger.setLevel(logging.DEBUG)


def main(**kwargs: object) -> None:
    """_summary_"""
    logger.debug(f"{kwargs=}")


if __name__ == '__main__':
    # Main 実行された場合のみ、ログファイルを出力する
    # 本番稼働では、ログファイルの蓄積により容量を圧迫されないように、ファイルにログ出力させないこと
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)  # ログ出力先を確実に用意する
        formatter_func = '%(asctime)s - %(module)s.%(funcName)s [%(levelname)s]\t%(message)s'  # フォーマットを定義
        logging.basicConfig(
            filename=str(LOG_DIR / f"{OWN_FILE_NAME}.logger.log"),
            level=logging.DEBUG,
            format=formatter_func,
            encoding='utf-8',
        )  # ログファイルを出力する設定

        # コマンドライン引数を受け取りたい場合
        parser = argparse.ArgumentParser()
        parser.add_argument('--arg1', type=str, required=False)
        args = parser.parse_args()

        main(**vars(args))
    except Exception:
        logger.exception("Unhandled exception in main")
