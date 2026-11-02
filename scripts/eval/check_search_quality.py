#!/usr/bin/env python
"""社区检索质量评测（门禁第 11 关）。

**这个脚本回答一个问题**：社区搜索还能不能把视频找出来。

没有它的话，检索能力只有「跑得通」这一层证据：接口返 200、搜标题能搜到。
但真正会退化的不是「接口报错」，而是**「接口照常返 200，只是搜不出东西了」**
——索引少一列、正文没进索引、触发器漏写一列，三者的症状都是这个。
这类退化不会被任何「功能正常」的测试发现：搜索还是能搜，只是搜不全。

所以这里做两轨判定（ADR 0030）：

- **结构轨**：索引的列集合、三个同步触发器写的列、分词器——
  问「该在的东西在不在」。这部分不依赖语料，改坏了立刻红。
- **质量轨**：固定语料上的 top-1 准确率、正文词召回、2 字词召回、零误召回——
  问「效果有没有掉」。阈值写在 `corpus.json` 里，与语料同源。

**语料是合成的**（`corpus.json`）：真实字幕是平台内容，著作权归发布方
（ADR 0002），公开仓库不收录真实正文。合成语料不代表真实分布，
它保证的是**可复算**：同一份语料、同一份阈值、同一份命令，任何人跑都得到
同一个数字。本地无网络、无 key、秒级跑完，所以它能进门禁。

跑法：

    backend/venv/Scripts/python.exe scripts/eval/check_search_quality.py

任何一条不达标就 exit 1。
"""
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(REPO, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

CORPUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus.json")

#: 语料里用来算「这一条文档含不含这个词」的字段。
#: 顺序与索引列集合一致，但这里用的是**语料侧的字段名**（语料里叫 title / summary_md）。
_FIELDS = ("title", "tags", "summary_md", "subtitle_text", "video_url")


class Report:
    """逐条 ``<PASS|FAIL>`` 记录器。失败一条就是门禁失败。"""

    def __init__(self):
        self.failures = 0

    def check(self, ok, label, detail=""):
        if not ok:
            self.failures += 1
        tag = "<PASS>" if ok else "<FAIL>"
        suffix = f" —— {detail}" if detail else ""
        print(f"{tag} {label}{suffix}")
        return ok


def load_corpus():
    if not os.path.exists(CORPUS_PATH):
        raise SystemExit(f"找不到评测语料：{CORPUS_PATH}")
    with open(CORPUS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def seed(documents):
    """把语料里的文档按**生产路径**灌进库里。

    刻意走 reserve → complete 这条真实协议，而不是直接 INSERT：
    直接 INSERT 会绕过「状态机」与「唯一索引」这两道真实的约束，
    评测出来的就不是线上那条路。
    """
    import database

    for doc in documents:
        outcome, _ = database.reserve_video(
            doc["video_url"], None, video_title=doc.get("title", "")
        )
        if outcome != "reserved":
            raise SystemExit(
                f"语料文档 {doc['id']} 占位失败（{outcome}）：语料本身有问题"
            )
        updated = database.complete_video(
            doc["video_url"],
            summary_md=doc.get("summary_md", ""),
            mindmap_md="",
            tags=list(doc.get("tags") or []),
            subtitle_text=doc.get("subtitle_text", ""),
        )
        if updated != 1:
            raise SystemExit(f"语料文档 {doc['id']} 写回失败：占位行不见了")


def expected_docs(query, documents):
    """一条查询应该命中哪些文档。

    语料里 top1 与 short2 直接给了 ``expect``；hit 类没给，就按
    「哪个文档的字段里含这个词」算出来——那正是这条查询的语义。
    """
    if query.get("expect"):
        return {query["expect"]}
    hits = set()
    for doc in documents:
        for field in _FIELDS:
            value = doc.get(field)
            if isinstance(value, list):
                value = " ".join(value)
            if value and query["q"] in str(value):
                hits.add(doc["id"])
                break
    return hits


def run(corpus):
    """评测在临时库里跑；跑完把临时库删掉。

    门禁每次都跑这个脚本，临时库留在 ``%TEMP%`` 里只会一天天堆下去。
    """
    tmpdir = tempfile.mkdtemp(prefix="viddigest-eval-")
    try:
        return _run(corpus, tmpdir)
    finally:
        import database

        # 连接池里可能还攥着那个库文件；Windows 上文件被开着就删不掉。
        database.forget_all_connections()
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run(corpus, tmpdir):
    import database

    report = Report()
    documents = corpus["documents"]
    queries = corpus["queries"]
    thresholds = corpus["thresholds"]

    database.DB_PATH = os.path.join(tmpdir, "eval.db")
    database.forget_all_connections()
    database.init_db()
    seed(documents)

    url_to_doc = {doc["video_url"]: doc["id"] for doc in documents}

    # ── 结构轨：该不该在的东西在不在 ──────────────────────────
    with database.get_db() as conn:
        index_cols = tuple(
            r[1] for r in conn.execute("PRAGMA table_info(videos_fts)")
        )
        triggers = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master"
                " WHERE type = 'trigger' AND name LIKE 'videos_fts_%'"
            )
        }
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'videos_fts'"
        ).fetchone()
        index_sql = row[0] if row else ""

    report.check(
        index_cols == database._VIDEO_SEARCH_COLUMNS,
        "结构：索引列集合",
        "、".join(index_cols),
    )
    report.check(
        set(triggers) == {"videos_fts_ai", "videos_fts_au", "videos_fts_ad"},
        "结构：三个同步触发器都在",
        "、".join(sorted(triggers)),
    )
    missing = {
        name: [c for c in database._VIDEO_SEARCH_COLUMNS if c not in sql]
        for name, sql in triggers.items()
    }
    missing = {k: v for k, v in missing.items() if v}
    report.check(
        not missing, "结构：触发器写全 5 列", "漏写：" + str(missing) if missing else ""
    )
    report.check(
        "using fts5" in index_sql.lower() and "trigram" in index_sql.lower(),
        "结构：索引是 trigram 分词的 FTS5 表",
    )

    # ── 质量轨：效果有没有掉 ────────────────────────────────
    top1_hit = top1_total = 0
    hit_hit = hit_total = 0
    short_hit = short_total = 0
    none_missed = none_total = 0
    failures = []

    for query in queries:
        q = query["q"]
        kind = query["kind"]
        result = database.search_community_videos(q=q, page=1, page_size=50)
        items = result["items"]
        got = [url_to_doc.get(item["video_url"]) for item in items]

        if kind == "top1":
            top1_total += 1
            if got and got[0] == query.get("expect"):
                top1_hit += 1
            else:
                failures.append(
                    f"top1 失手：{q!r} 期望 {query.get('expect')}，实际 {got[:3]}"
                )
        elif kind == "hit":
            hit_total += 1
            expected = expected_docs(query, documents)
            if expected & set(got):
                hit_hit += 1
            else:
                failures.append(f"正文词召不回：{q!r}（期望 {sorted(expected)}）")
        elif kind == "short2":
            short_total += 1
            expected = expected_docs(query, documents)
            if expected & set(got):
                short_hit += 1
            else:
                failures.append(f"2 字词召不回：{q!r}（期望 {sorted(expected)}）")
        elif kind == "none":
            none_total += 1
            if items:
                none_missed += 1
                failures.append(f"无谓召回：{q!r} 命中了 {got[:3]}")
        else:
            failures.append(f"未知的查询类别：{kind!r}")

    def ratio(hit, total):
        return (hit / total) if total else 0.0

    report.check(
        ratio(top1_hit, top1_total) >= thresholds["top1_accuracy"],
        "质量：top-1 准确率",
        f"{top1_hit}/{top1_total} = {ratio(top1_hit, top1_total):.1%}"
        f"（阈值 ≥ {thresholds['top1_accuracy']:.0%}）",
    )
    report.check(
        ratio(hit_hit, hit_total) >= thresholds["hit_recall"],
        "质量：正文词召回",
        f"{hit_hit}/{hit_total} = {ratio(hit_hit, hit_total):.1%}"
        f"（阈值 = {thresholds['hit_recall']:.0%}）",
    )
    report.check(
        ratio(short_hit, short_total) >= thresholds["short2_recall"],
        "质量：2 字词召回",
        f"{short_hit}/{short_total} = {ratio(short_hit, short_total):.1%}"
        f"（阈值 = {thresholds['short2_recall']:.0%}）",
    )
    report.check(
        none_missed <= thresholds["none_miss"],
        "质量：零误召回",
        f"误召回 {none_missed}/{none_total}（阈值 ≤ {thresholds['none_miss']}）",
    )

    if failures:
        print("")
        print("失手明细：")
        for line in failures[:20]:
            print(f"  - {line}")
        if len(failures) > 20:
            print(f"  - …… 还有 {len(failures) - 20} 条")

    return report


def main():
    corpus = load_corpus()
    report = run(corpus)
    if report.failures:
        print("")
        print(f"eval: FAILED（{report.failures} 条不达标）")
        return 1
    print("")
    print("eval: OK（结构与质量全部达标）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
