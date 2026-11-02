"""门禁第 11 关不该往临时目录里堆东西。

评测脚本原来用 `mkdtemp` 建临时库、跑完不删：门禁每跑一次，本机临时目录里
就多一个 `viddigest-eval-*` 空目录，一天能堆几十个（实测 2026-11-02 清掉了
50 个）。这不是「不干净」的问题——它让临时目录变成没人敢删的黑洞。

所以这里盯**跑完没有残留**：脚本改回 `mkdtemp` 就转红。

顺带把 `database.DB_PATH` 复原：评测脚本把它指向临时库里，跑完库被删掉，
不复原的话后面用例会拿到一个指向已删文件的路径。
"""
import glob
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVAL_DIR = os.path.join(REPO, "scripts", "eval")
if EVAL_DIR not in sys.path:
    sys.path.insert(0, EVAL_DIR)

import database  # noqa: E402

import check_search_quality as ev  # noqa: E402


def _eval_temp_dirs():
    """临时目录里现存的评测临时库。"""
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "viddigest-eval-*")))


def test_eval_run_leaves_no_temp_dir(db):
    before = _eval_temp_dirs()
    original_db_path = database.DB_PATH
    try:
        report = ev.run(ev.load_corpus())
    finally:
        database.DB_PATH = original_db_path
        database.forget_all_connections()

    # 前提自检：评测自己都不达标的话，「没有残留」这条断言就没有意义。
    assert report.failures == 0, "评测不达标，这条用例的前提没了"
    assert _eval_temp_dirs() - before == set(), "评测跑完留下了临时库目录"
