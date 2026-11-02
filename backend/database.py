import os
import sys
import json
import hashlib
import math
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

# 视频链接的查询侧归一（工单 #25）。这个模块不 import 本项目的任何东西，
# 所以顶层引入不会绕成环——与 model_catalog 那个刻意延迟的 import 不同。
from url_canonical import canonical_video_url

# 思维导图的结构规则（工单 #106 / ADR 0027）。同样不 import 本项目的任何
# 东西（纯文本处理），所以顶层引入不会绕成环——存量导图重建要用它，
# 而导图规则的唯一真值必须只有一份。
from mindmap import (
    clean_mindmap_markdown,
    exceeds_limits,
    is_degenerate,
    rebuild_mindmap_from_summary,
)

# 总结模板的默认 id（工单 #105 / ADR 0026）。同样不 import 本项目的任何
# 东西（只读一个 JSON 清单），所以顶层引入不会绕成环——而列的默认值
# 必须跟着清单走，另写一份 'full' 就是两份真值。
from prompt_template import default_template_id

# 额度种类的唯一注册表（ADR 0033）。同样不 import 本项目任何东西——列名与
# 中文名只有一份，本模块只决定怎么用它们。
import quota_kinds

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "app.db")

# 线程本地连接缓存（uvicorn 线程池复用线程，连接随之复用）
_thread_local = threading.local()

# 全部已建立连接（含线程池工作线程那份），供测试夹具彻底清理。
# threading.local() 的属性只有持有它的线程能删，主测试线程拿不到工作线程那份，
# 因此必须在这里留一份清单。
#
# 强引用是必须的（Connection 不支持弱引用，实测 weakref.WeakSet 直接抛
# TypeError: cannot create weak reference to 'sqlite3.Connection' object），
# 代价是「线程死了但清单还留着它的连接」会造成 fd 滞留。因此每次登记前
# 先剔除已死线程的条目——threading.enumerate() 只列活线程。
_open_conns = []
#: 每份连接由哪个线程创建（与 _open_conns 同序）。
#: 测试要证明「工作线程那份确实被登记了」，光数数量不够——
#: 同一份连接被登记两次也能让计数达标。
_open_conns_threads = []
_open_conns_lock = threading.Lock()

# 连接代际号。清理连接时递增，所有线程下次取连接时发现代际变了就重建。
#
# 为什么不能靠「试一次 execute 看连接是否已关闭」：sqlite3 禁止跨线程使用
# 连接，主线程对一份**仍打开**的连接执行 execute 会抛
#   ProgrammingError: SQLite objects created in a thread can only be used in
#   that same thread
# 而对一份**已关闭**的连接抛的是
#   ProgrammingError: Cannot operate on a closed database
# 两者异常类型相同，只有消息文本能区分——也就是说，在主线程用
# `pytest.raises(sqlite3.ProgrammingError)` 断言「连接已关闭」恒真，
# 测不出任何东西（这正是本工单早期版本里一条假通过的断言）。
# 代际号不依赖连接对象的内部状态，跨线程可靠。
_conn_generation = 0


def get_connection_generation() -> int:
    return _conn_generation

#: 每用户保留的解析历史条数。
#:
#: 曾经是 30，配合历史页一次全量渲染尚可。搜索 + 标签 + 收藏 + 分页
#: 落地之后，30 条是一个用户**翻不到底也搜不到**的窗口：想找三个月前
#: 存的那条链接，唯一的办法是它还没被挤出去。
#:
#: 1000 是保留条数，不是单页条数 —— 列表按 HISTORY_PAGE_SIZE_DEFAULT
#: 分页取，否则一千个卡片一次进 DOM。
MAX_PARSE_HISTORY_PER_USER = 1000

#: 历史列表单页条数。与社区列表同值：两个列表的翻页手感不该不一样。
HISTORY_PAGE_SIZE_DEFAULT = 20

# 两个额度上限都从环境变量读取，改配置不必发版。
# 各自独立取值：解析产出内容、追问消耗对话，用量节奏本就不同。
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        # 配错时退回默认值，而不是让整个应用起不来——
        # 一个环境变量写错不该让整个站点不可用。
        return default


DAILY_PARSE_LIMIT = _env_int("VIDDIGEST_DAILY_PARSE_LIMIT", 3)
DAILY_CHAT_LIMIT = _env_int("VIDDIGEST_DAILY_CHAT_LIMIT", 10)


def get_db_path():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return DB_PATH


def _prune_dead_threads_locked() -> None:
    """剔除已死线程的连接条目（调用方须已持锁）。

    线程死掉后，它的 threading.local 一并销毁，连接本可被 GC 回收；
    但 _open_conns 强引用着它，不剔除就会一直占着 OS 文件句柄。
    实测：200 个短命线程，不剔除时注册表滞留 201 条、句柄净增 401。

    sqlite3.Connection 不支持弱引用（weakref.WeakSet 抛 TypeError），
    只能靠「线程是否还活着」判断，threading.enumerate() 正好只列活线程。

    快路径比的是**身份**不是数量：曾用 `len(alive) == len(登记数)` 直接跳过，
    但死线程数与新活线程数相等时那两条判据会同时成立，死条目被漏掉
    （实测残留 2 条，每条 WAL 连接占 2~3 个句柄）。
    """
    alive = {t.ident for t in threading.enumerate()}
    if _open_conns_threads and alive.issuperset(_open_conns_threads):
        return  # 每条登记都属于活线程，无需逐一比对

    keep = [i for i, owner in enumerate(_open_conns_threads) if owner in alive]
    if len(keep) == len(_open_conns_threads):
        return

    _open_conns[:] = [_open_conns[i] for i in keep]
    _open_conns_threads[:] = [_open_conns_threads[i] for i in keep]


def open_connections():
    """当前进程内所有未关闭的数据库连接（跨线程）。

    仅供测试夹具使用：它需要清掉线程池工作线程持有的连接，
    而那些连接存在各自的 threading.local() 里，外部无法直接触达。
    """
    with _open_conns_lock:
        _prune_dead_threads_locked()
        return list(_open_conns)


def open_connection_threads():
    """与 open_connections() 同序的「创建线程 ident」列表。

    单独给出是因为「工作线程那份被登记了」这件事，光数数量证明不了：
    同一份连接被登记两次也能让计数达标。

    这里也调剔除——否则「与 open_connections() 同序」不总成立：
    先读 threads 再读 conns 时，中间若有线程死亡，两者长度就会不等。
    """
    with _open_conns_lock:
        _prune_dead_threads_locked()
        return list(_open_conns_threads)


def forget_all_connections():
    """关闭并清空全部已建立连接，同时递增代际号。

    sqlite3 的连接归创建它的线程所有，**主线程 close 不了工作线程那份**：
    会抛 ProgrammingError: objects created in a thread can only be used in
    that same thread。threading 又没有「按线程 ident 投递任务」的 API，
    因此工作线程的连接只能等它自己在下次 get_db() 时因代际号变化而丢弃，
    届时引用计数归零、连接被 GC 回收（sqlite3.Connection.__del__ 会 close）。

    所以这里对工作线程的连接不做 close，只清登记；对主线程自己的那份
    直接 close——那条是真能关掉的。跨线程 close 抛错被静默吞掉，
    等于一个都没关，假装关了才是更糟的错误。

    递增代际号解决的是「别再用旧连接」，与「立刻关掉旧连接」是两件事：
    前者是正确性保证（由测试守住），后者只是资源回收的尽力而为。
    """
    global _conn_generation
    current = threading.get_ident()
    with _open_conns_lock:
        _conn_generation += 1
        _prune_dead_threads_locked()
        owned_by_current = [
            conn for conn, owner in zip(_open_conns, _open_conns_threads)
            if owner == current
        ]
        _open_conns.clear()
        _open_conns_threads.clear()

    for conn in owned_by_current:
        try:
            conn.close()
        except sqlite3.Error:
            pass


@contextmanager
def get_db():
    """线程本地连接复用：WAL/外键等 PRAGMA 仅在新连接时设置一次，
    避免每次操作重建连接的开销（实测单次查询 155ms → 亚毫秒级）。"""
    with _open_conns_lock:
        generation = _conn_generation

    conn = getattr(_thread_local, "conn", None)
    if conn is not None and getattr(_thread_local, "generation", None) != generation:
        # 连接属于上一代（已被清理或换过库），本线程还拿着它。
        # 必须重建，否则工作线程会写进上一个测试的库。
        conn = None
        _thread_local.conn = None

    if conn is None:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        # 归一函数注册成 SQL 可调用的形式，让下面的 INSERT 触发器能用它
        # （工单 #25）。注册在**每条**连接上：线程本地缓存会复用连接，
        # 而 create_function 是连接级的，少一处就有一个线程拿到未注册的连接。
        conn.create_function(
            "canonical_url_of", 1, canonical_video_url, deterministic=True
        )
        # 邀请码状态也注册成 SQL 可调用：状态筛选（WHERE invite_status(...)
        # = ?）与列表投影（每行的 status）必须是**同一份算法**。
        # 分成两份的话，筛出来的行和它显示的状态会对不上——而那种对不上
        # 不报错，只是「筛了却看不到」「看到却筛不出来」。
        conn.create_function(
            "invite_status", 4, invite_code_status, deterministic=True
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _thread_local.conn = conn
        _thread_local.generation = generation
        with _open_conns_lock:
            _prune_dead_threads_locked()
            _open_conns.append(conn)
            _open_conns_threads.append(threading.get_ident())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    """初始化数据库表结构"""
    with get_db() as conn:
        # is_favorite 的补列必须排在 executescript **之前**。
        #
        # 脚本里有 `CREATE INDEX ... ON parse_history(is_favorite)`，而
        # `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作：老库里那一列
        # 不会凭空出现，索引一建就 no such column，整段脚本直接 abort，
        # 排在后面的迁移永远轮不到 —— 表现为「代码本地好好的，一升级就打不开」。
        #
        # 全新库里 parse_history 还没建出来，那时也不能 ALTER，所以函数内部
        # 自己判断表在不在。
        _migrate_history_favorite_column(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_vip INTEGER DEFAULT 0,
                vip_expire_at TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                -- 单人额度上限覆盖（工单 #12）。**可空、默认 NULL**。
                -- NULL 的语义是「回落全局上限」，**不是 0**：0 是「一条都不能用」，
                -- 两者在库里必须分得开，否则给某个账号「不限量」会变成「完全封死」。
                parse_limit_override INTEGER,
                chat_limit_override INTEGER,
                daily_summary_count INTEGER DEFAULT 0,
                last_summary_date TEXT,
                daily_parse_count INTEGER DEFAULT 0,
                last_parse_date TEXT,
                daily_chat_count INTEGER DEFAULT 0,
                last_chat_date TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT DEFAULT 'cny',
                status TEXT DEFAULT 'pending',
                plan_type TEXT DEFAULT 'monthly',
                stripe_session_id TEXT UNIQUE,
                stripe_payment_intent_id TEXT,
                paid_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);

            CREATE TABLE IF NOT EXISTS parse_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                video_url TEXT NOT NULL,
                -- 同 videos：比较用，不回显。带它是因为下面那句
                -- `LEFT JOIN videos v ON v.canonical_url = h.canonical_url`
                -- 按原文 JOIN 的话，两个人粘了不同形态就谁也 join 不上谁。
                canonical_url TEXT DEFAULT '',
                video_title TEXT DEFAULT '',
                video_data TEXT DEFAULT '',
                summary_md TEXT DEFAULT '',
                mindmap_md TEXT DEFAULT '',
                subtitle_data TEXT DEFAULT '',
                chat_history TEXT DEFAULT '[]',
                -- 收藏（个人标记）。为什么是这一列而不是一张表：
                -- 「收藏哪条解析」是 parse_history 行的属性，跟着行走
                -- 才对，单独建表要多一次 join，还多一个能写歪的入口。
                -- 它只影响**这条记录自己**的裁剪与删除，不进社区。
                is_favorite INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_history_user ON parse_history(user_id, updated_at);
            -- (user_id, video_url) 的唯一索引**不在这里建**，由
            -- _migrate_parse_history_unique_url 拥有：建 UNIQUE 之前必须先把
            -- 存量重复行合成掉，而那一步需要表已经存在（全新库这一刻还没建）。
            -- 留在这段脚本里会与 executescript 的「对已存在的表是空操作」
            -- 特性撞车——老库里那一条普通索引会让后面的 CREATE UNIQUE 静默失效。
            -- 「仅收藏」是历史页的一个档位，这条索引让它不必扫全表。
            CREATE INDEX IF NOT EXISTS idx_history_fav ON parse_history(user_id, is_favorite);

            -- 社区视频表（ADR 0001）：全站共享，一个链接只有一行。
            --
            -- 与 parse_history 的分工：那张表是「谁解析过什么」的个人记录，
            -- 每用户滚动保留 MAX_PARSE_HISTORY_PER_USER 条；这张表是社区内容本身，
            -- 任何人都能读，且**刻意不做条数裁剪**——裁剪会直接摧毁社区的价值
            -- （A 解析的视频被裁掉，B 就再也复用不到了）。
            --
            -- video_url 的唯一性靠下面那条全局唯一索引，而不是表内 UNIQUE 约束：
            -- 它是「同一链接全站只解析一次」这条承诺的落地点，必须有自己的名字，
            -- 迁移与排查时能直接指认（parse_history 那条 idx_history_user_url 含
            -- user_id，无法复用）。
            --
            -- status = 'pending' 的行是**占位**：占位者才是首次解析者，由他去调模型。
            -- 已 ready 的行谁都不能改写（complete_video 只更新自己占的那一行）。
            --
            -- parsed_by 不建外键：解析者注销后社区内容必须留下来。
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_url TEXT NOT NULL,
                -- 归一值（工单 #25）。video_url 保持「用户粘进来的原文」不变，
                -- 它要回显；这一列只用于**比较**，让同一个视频的多种形态
                -- （无 www / 移动域名 / 跟踪参数 / 分享文案）收敛成同一个 key。
                -- 写入后从不被 UPDATE：全库没有一条 UPDATE 会改 video_url。
                canonical_url TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                summary_md TEXT DEFAULT '',
                mindmap_md TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                subtitle_text TEXT DEFAULT '',
                -- 这次总结用的提示词模板 id（工单 #105 / ADR 0026）。
                --
                -- 解析完成时才写：占位行在完成之前还不知道用哪份模板，
                -- 那时它是 NULL，而不是一个假装确定的默认值。
                -- 老库由 _migrate_videos_template_column 补列并回填成默认模板。
                template TEXT,
                parsed_by INTEGER,
                -- 覆盖闸门（工单 #20）。NULL = 此刻没人正在覆盖。
                -- 为什么落在 videos 上而不是单开一张锁表：抢锁要判的条件是
                -- 「这一行归我 + 没人占着」，前者是 videos 上的列、后者也是
                -- videos 上的列，拆成两张表就多一次跨表竞态。
                regenerating_by INTEGER,
                regenerating_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_url ON videos(video_url);

            -- 追问会话（工单 #8）：明细表。一次「会话」= (user_id, video_url) 的聚合。
            --
            -- **刻意不做条数裁剪**：parse_history 的 30 条滚动规则是它自己那张表的
            -- 规矩，与本表无关。追问记录被裁掉是静默的数据丢失——用户问过的东西
            -- 不该因为他最近解析得有点多就消失。工单点名的缺陷正是这个。
            --
            -- role 拆成两行（一问一行、一答一行），而不是 {question, answer} 一行：
            -- 「最近 3 轮」天然是消息序列，按 id 取最近 2 倍条数即可，不用反解配对；
            -- 而两列结构会让断流时那半条「有问无答」的记录**看起来完整**，
            -- 于是被当成有效历史塞进模型上下文。
            --
            -- 不建 user_id 外键：与 videos.parsed_by 同一理由——
            -- 用户注销后会话记录应随该用户一起消失，而不是变成孤儿行。
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                -- 同上：追问记录也要能跨形态对上号。
                canonical_url TEXT DEFAULT '',
                user_id INTEGER NOT NULL,
                video_url TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_chat_user_url ON chat_messages(user_id, video_url, id);

            -- 邀请码（工单 #99 / #103）。注册必须带码，码由管理员发放。
            --
            -- **只存哈希**：库里存的是 sha256(明文)，不是明文——库被读走
            -- 不等于码被用掉。代价是明文取不回来，所以**明文只在创建响应
            -- 里给那一次**，之后谁都拿不回完整码。
            --
            -- 表结构（每一列的含义）与升级路径见本文件末尾的
            -- _INVITE_CODES_DDL：那是**唯一**一份 DDL，executescript 与
            -- 老库升级路径引用同一个常量——两处各写一遍的话，漂移的后果
            -- 是老库缺列而新库不缺（症状：老用户升级后注册报 no such
            -- column，而新装的人一切正常）。
            """
            + _INVITE_CODES_DDL
            + """
        """)

        # 邀请码（工单 #99）：建表 + 补列。
        #
        # 排在最前面不是因为它更要紧，而是因为它是**唯一一张新表**：
        # 老库里这张表压根不存在，executescript 里的
        # `CREATE TABLE IF NOT EXISTS` 正好把缺的表建出来；而这一步
        # 还要负责「表在但列不全」的补列（SQLite 不支持
        # ADD COLUMN IF NOT EXISTS，只能先查列再 ALTER）。
        _migrate_invite_codes_table(conn)

        _migrate_quota_columns(conn)
        _migrate_quota_override_columns(conn)
        _migrate_video_card_columns(conn)
        _migrate_regenerate_columns(conn)
        # template 列（工单 #105）：补列 + 把老行回填成默认模板。
        # 排在 _migrate_regenerate_columns 之后即可——它只碰 videos 一张表，
        # 与其它迁移没有先后依赖。
        _migrate_videos_template_column(conn)
        _migrate_admin_column(conn)
        # canonical_url（工单 #25）：补列 + 回填 + 建普通索引。
        # 排在 executescript 之后是因为表得先存在（全新库那一刻还没建）；
        # 排在 _migrate_parse_history_unique_url 之前是因为那条也要扫
        # parse_history，而回填会 UPDATE 那张表。
        _migrate_canonical_url_columns(conn)
        _create_canonical_url_triggers(conn)
        # canonical 上的唯一索引（工单 #25）：**去重必须排在建索引之前**，
        # 与 _migrate_parse_history_unique_url 同一套理由。
        # 也排在 _create_video_search_index 之前：那一步会对 videos 做一次
        # #7/工单 #19 建立的 FTS 'rebuild'，而这里可能刚删掉了几行。
        _migrate_videos_canonical_unique(conn)
        # 去重必须排在 _create_video_search_index 之前：它自己也会扫
        # parse_history，而重复行会让社区搜索的候选集出现同一条内容两次。
        # 排在 executescript 之后是因为表得先存在（全新库那一刻还没建）。
        _migrate_parse_history_unique_url(conn)
        # 存量导图重建（工单 #106 / ADR 0027）：退化（层级 <2）的导图按新
        # 规则重建，依据是同一行的总结正文。排在去重之后是因为去重会
        # 删行，先删完再扫少扫几行；排在 _create_video_search_index 之后
        # 是因为那条 UPDATE 会触发 videos_fts_au 触发器——索引表得先在。
        _create_video_search_index(conn)
        _migrate_mindmap_levels(conn)
        # 模型清单（工单 #13 / ADR 0011）是**另一张表**，DDL 与播种都在
        # model_catalog 里——那张表不属于 users 域，塞进来只会让两条不同的
        # 迁移线索混在同一个文件里。
        #
        # 延迟 import：model_catalog 顶层要 import database（取 get_db），
        # 模块级双向 import 会成环。与 auth.get_current_user 里的
        # `from database import get_user_by_id` 同一套路。
        from model_catalog import init_model_catalog
        init_model_catalog(conn)


def _migrate_invite_codes_table(conn) -> None:
    """确保 invite_codes 表存在、且是**只存哈希**的新结构（工单 #99 / #103）。

    ## 三条路径

    1. **表不存在** → 按新结构建出来。
    2. **表在，还是老结构**（``code`` 存明文）→ **重建表**：建新表 → 把
       明文逐行换算成哈希搬过去 → 删旧表 → 改名。SQLite 改不了列类型
       （``code TEXT NOT NULL UNIQUE`` → ``code_hash TEXT NOT NULL UNIQUE``），
       删列更是要重建表，所以这是唯一走得通的路。
    3. **表在，已经是新结构** → 只补缺的列（SQLite 没有 ADD COLUMN IF
       NOT EXISTS，只能先查列再 ALTER）。

    ## 建表语句只有一份

    DDL 在 ``_INVITE_CODES_DDL`` 里，executescript 与本函数引用**同一个
    常量**：两处各写一遍的话，漂移的后果是老库缺列而新库不缺——症状是
    「老用户升级后注册报 no such column」，而新装的人一切正常。
    ``tests/test_invite_codes.py`` 里那条「老库升级后的列集必须与全新库
    完全一致」的断言守着这件事。

    重复执行幂等：表在且已是新结构时，这一步什么都不改。
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='invite_codes'"
    ).fetchone()
    if row is None:
        conn.execute(_INVITE_CODES_DDL)
        return

    existing = {r["name"] for r in conn.execute("PRAGMA table_info(invite_codes)")}
    if "code_hash" not in existing:
        # 老结构：``code`` 存明文。必须重建表（见上面第 2 条路径）。
        _rebuild_invite_codes_as_hashed(conn)
        return

    # 已是新结构，但列不全（例如上一版发布时少写了两列）：逐列补。
    # SQLite 没有 ADD COLUMN IF NOT EXISTS，先查列再 ALTER。
    for column, ddl in (
        ("code_hint", "ALTER TABLE invite_codes ADD COLUMN code_hint TEXT NOT NULL DEFAULT ''"),
        ("note", "ALTER TABLE invite_codes ADD COLUMN note TEXT"),
        ("revoked", "ALTER TABLE invite_codes ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0"),
        ("used_count", "ALTER TABLE invite_codes ADD COLUMN used_count INTEGER NOT NULL DEFAULT 0"),
        ("max_uses", "ALTER TABLE invite_codes ADD COLUMN max_uses INTEGER NOT NULL DEFAULT 1"),
        ("expires_at", "ALTER TABLE invite_codes ADD COLUMN expires_at TEXT"),
        ("created_at", "ALTER TABLE invite_codes ADD COLUMN created_at TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in existing:
            conn.execute(ddl)


def _legacy_invite_value(row, columns, name, default=None):
    """老表里取一列的值：列不存在或值为空时用默认值。"""
    if name not in columns:
        return default
    value = row[name]
    return default if value is None else value


def _rebuild_invite_codes_as_hashed(conn) -> int:
    """老库升级：``code`` 存明文 → **重建表**成只存哈希（工单 #103）。

    搬数据时逐行算哈希：明文只在**这一刻**存在于内存里，落库的是哈希。

    提示列（``code_hint``）**从明文算**——从哈希算不出来，所以这一列
    必须在升级这一刻定下来。

    **整段在一个事务里**：DROP TABLE 在 autocommit 下会**立刻提交**，
    中途失败就是「表没了、数据也没了」；显式 BEGIN 让它要么全成、
    要么全不成。

    **升级后要把旧页上的明文清掉**（VACUUM）：DROP 掉的表只是被标记为
    空闲页，**内容还在文件里**——不 VACUUM 的话，「库被读走等于码被用掉」
    这条承诺对老库是假的：攻击者翻文件空闲页照样翻出明文。VACUUM 把文件
    重写一遍，空闲页消失。VACUUM 失败（文件被锁）不阻断升级——行级的
    保证（表里没有明文）已经成立。

    返回搬过去的行数。
    """
    old_columns = {r["name"] for r in conn.execute("PRAGMA table_info(invite_codes)")}
    rows = conn.execute("SELECT * FROM invite_codes").fetchall()

    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE invite_codes")
        conn.execute(_INVITE_CODES_DDL)
        for row in rows:
            plaintext = (_legacy_invite_value(row, old_columns, "code", "") or "").strip()
            conn.execute(
                "INSERT INTO invite_codes (id, code_hash, code_hint, created_by, created_at,"
                " expires_at, max_uses, used_count, revoked, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    hash_invite_code(plaintext),
                    mask_invite_code(plaintext) if plaintext else "",
                    _legacy_invite_value(row, old_columns, "created_by"),
                    _legacy_invite_value(row, old_columns, "created_at") or _iso_utc(),
                    _legacy_invite_value(row, old_columns, "expires_at"),
                    _legacy_invite_value(row, old_columns, "max_uses", 1),
                    _legacy_invite_value(row, old_columns, "used_count", 0),
                    _legacy_invite_value(row, old_columns, "revoked", 0),
                    _legacy_invite_value(row, old_columns, "note"),
                ),
            )
        # AUTOINCREMENT 的序列要跟上：显式搬 id 之后不更新序列，下一枚码会
        # 拿到一个**用过的** id（老码的 id 被接口引用过，不能复用）。
        max_id = conn.execute("SELECT max(id) FROM invite_codes").fetchone()[0]
        if max_id:
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'invite_codes'")
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('invite_codes', ?)",
                (max_id,),
            )
        if own_transaction:
            conn.execute("COMMIT")
    except Exception:
        if own_transaction:
            conn.execute("ROLLBACK")
        raise

    # 事务外做：VACUUM 不能在事务里跑。失败不阻断升级。
    try:
        conn.execute("VACUUM")
    except sqlite3.Error:
        pass
    return len(rows)


def _migrate_quota_columns(conn) -> None:
    """给已存在的 users 表补上额度拆分需要的新列（expand 阶段）。

    为什么必须单独一步：`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，
    老库里不会有新列，代码一读就报 no such column。

    逐列判断再 ALTER：SQLite 没有 `ADD COLUMN IF NOT EXISTS`，
    重复执行会抛 duplicate column name，所以先查列是否存在。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    for column, ddl in (
        ("daily_parse_count", "ALTER TABLE users ADD COLUMN daily_parse_count INTEGER DEFAULT 0"),
        ("daily_chat_count", "ALTER TABLE users ADD COLUMN daily_chat_count INTEGER DEFAULT 0"),
        ("last_parse_date", "ALTER TABLE users ADD COLUMN last_parse_date TEXT"),
        ("last_chat_date", "ALTER TABLE users ADD COLUMN last_chat_date TEXT"),
    ):
        if column not in existing:
            conn.execute(ddl)


def _migrate_quota_override_columns(conn) -> None:
    """给已存在的 users 表补上单人额度覆盖列（工单 #12，expand 阶段）。

    为什么必须单独一步：`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，
    老库里不会有新列，quota_limit 一读就报 no such column。**建表语句与迁移
    两处都必须写**，只改前者的话老库起不来、而新库看起来一切正常——
    这类错误只在升级现场暴露。

    逐列判断再 ALTER：SQLite 没有 `ADD COLUMN IF NOT EXISTS`，重复执行会抛
    duplicate column name，所以先查列是否存在。这也让重复跑 init_db 幂等。

    列**不写 NOT NULL、不写 DEFAULT**：0 与 NULL 在额度语义里是两件事
    （一条都不能用 vs 回落全局），建表语句里同样保持可空默认 NULL。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    for column, ddl in (
        ("parse_limit_override", "ALTER TABLE users ADD COLUMN parse_limit_override INTEGER"),
        ("chat_limit_override", "ALTER TABLE users ADD COLUMN chat_limit_override INTEGER"),
    ):
        if column not in existing:
            conn.execute(ddl)


def _migrate_admin_column(conn) -> None:
    """给已存在的 users 表补上 is_admin 列（工单 #11，expand 阶段）。

    与额度列同一套路：建表语句里已经有这一列，但 `CREATE TABLE IF NOT EXISTS`
    对老库是空操作，老库的 users 表不会凭空多出列，而 auth.require_admin
    一读就报 no such column。**两处都必须写**：只改建表语句，老库起不来。

    NOT NULL + DEFAULT 0：没有「未设置」这个状态，一个账号要么是管理员要么不是。
    老行由 DEFAULT 0 兜底，不会因为加列而被判成管理员。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "is_admin" not in existing:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")


# ── 首个管理员播种（工单 #11）──────────────────────────────
#
# 播种只做一件事：把 VIDDIGEST_ADMIN_EMAILS 里列出的账号置为 is_admin=1。
# 它**不是**「同步」——配置里删掉一个邮箱不会撤销该账号的管理员身份
# （撤销是人的操作，不是配置的副作用）。这样 env 少写一个字符不会在
# 下次启动时静默削掉一个管理员。


def _parse_admin_emails(raw: str | None) -> list[str]:
    """把逗号分隔的邮箱串解析成去空、去首尾空白的列表。

    strip 与跳过空串是必须的：运维手写 "a@x.com, b@x.com" 时逗号后面那个
    空格是常态，邮箱本身还可能带着引号残留。不 strip 就会拿一个永远匹配不上的
    字符串去查库，然后**安静地什么也不播种**——那是最难查的一种失败。

    保持原样、不做大小写折叠：注册时 email 原样入库（见 create_user，
    没有 lower），折叠会让「配了大写、注册用小写」这类偏差变成静默命中，
    反过来更难解释。按原样匹配，配错就是不播种。
    """
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def seed_admin_emails_from_env() -> int:
    """按 VIDDIGEST_ADMIN_EMAILS 播种管理员，返回受影响的用户行数。

    **未配置是合法配置**：返回 0，不报错、不播种。不是每个部署都有管理员。

    刻意在**调用时**读 env（而不是模块级冻结成常量），与 quota_limit 同一理由：
    冻结的副本会让「改了配置行为随之改变」这条无法验证。启动播种自然满足这一点，
    但本函数也可能被测试直接调用。
    """
    emails = _parse_admin_emails(os.getenv("VIDDIGEST_ADMIN_EMAILS"))
    if not emails:
        return 0
    placeholders = ",".join("?" * len(emails))
    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE users SET is_admin = 1 WHERE email IN ({placeholders})",
            emails,
        )
        return cursor.rowcount


# ── 社区浏览与搜索（工单 #7）─────────────────────────────────
#
# 可见性是这个工单的全部难点：社区列表对**任何人**开放，而字幕、总结、
# 思维导图只对已登录的人开放。两条规则一旦写反就是真实的隐私事故，
# 所以下面的实现有一条硬约定——对外响应**按字段白名单投影**，不靠逐个剔除。


def _migrate_regenerate_columns(conn) -> None:
    """给 videos 补上覆盖闸门的两列（工单 #20）。

    与 _migrate_video_card_columns 同一套路：`CREATE TABLE IF NOT EXISTS`
    对已存在的表是空操作，老库里不会凭空多出列，不补列则代码一 SELECT 就报
    `no such column`。

    刻意**不碰 status**：ADR 0007 要求覆盖期间旧内容继续对所有人可见，
    把行改回 pending 会让复用者突然看到空白。所以这个闸门是**并行的**第二套
    占用语义，与 pending 状态机互不影响。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
    for column, ddl in (
        ("regenerating_by", "ALTER TABLE videos ADD COLUMN regenerating_by INTEGER"),
        ("regenerating_at", "ALTER TABLE videos ADD COLUMN regenerating_at TEXT"),
    ):
        if column not in existing:
            conn.execute(ddl)


def _migrate_video_card_columns(conn) -> None:
    """给 videos 补上社区卡片要用的两列（expand 阶段，与额度列同一套路）。

    为什么必须单独一步：`CREATE TABLE IF NOT EXISTS videos` 对已存在的表
    是空操作，#6 建的老库里不会凭空多出列，而列表要显示的标题与封面恰恰
    是 #6 建表时没有的。不补列，代码一 SELECT 就报 no such column。

    只**新增**两列可空带默认值的列。videos 已有的每一列、唯一索引、
    pending/ready 状态机都不动：那是 #6 定下的承诺，不该被后一张工单
    顺带改掉——两张表职责分离的边界仍然是 #6 划的那条。

    标题与封面由谁写入：解析时服务端拿不到它们（字幕流里没有平台标题，
    也没有缩略图地址），因此由前端解析成功后回填，见 publish_video_card。
    在此之前这两列为空串——社区列表会显示占位文本，但**不会**显示错误的值。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
    for column, ddl in (
        ("video_title", "ALTER TABLE videos ADD COLUMN video_title TEXT DEFAULT ''"),
        ("cover_url", "ALTER TABLE videos ADD COLUMN cover_url TEXT DEFAULT ''"),
    ):
        if column not in existing:
            conn.execute(ddl)


def _migrate_videos_template_column(conn) -> None:
    """给 videos 补上 template 列（工单 #105 / ADR 0026）。

    与 _migrate_video_card_columns 同一套路：`CREATE TABLE IF NOT EXISTS`
    对已存在的表是空操作，老库里不会凭空多出列，不补列则代码一 SELECT 就报
    `no such column`。

    老行回填成**默认模板**：那批总结是在「还没有模板这回事」的时候解析出来的，
    它们用的就是默认那份。留 NULL 会让「这一份是哪种模板」变成两义——
    NULL 既可能是「默认」，也可能是「还没解析完」，两者混在一起没法读。
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
    if "template" not in existing:
        conn.execute("ALTER TABLE videos ADD COLUMN template TEXT")
    conn.execute(
        "UPDATE videos SET template = ? WHERE template IS NULL OR template = ''",
        (default_template_id(),),
    )


#: 合并时逐列取「最新的非空值」。空 = '' 或 None 两种形态都算空，
#: 理由与 _dedupe_parse_history 一致：两条重复行往往是互补的，
#: 只留最新那条会把另一半悄悄抹掉。
_VIDEO_CONTENT_COLUMNS = (
    "video_title", "cover_url", "summary_md", "mindmap_md",
    "subtitle_text", "tags",
)


def _row_order_key(row: dict) -> tuple:
    """「更新」的可比键。与 _dedupe_parse_history 用同一套：时间优先，id 兜底。

    id 兜底是必须的：created_at / updated_at 的默认值是
    ``datetime('now')``，同一秒内建出来的两行字符串完全相同，
    没有兜底时 max() 会随便挑一个，「最新」就成了不确定的。
    """
    return (row.get("updated_at") or "", row.get("created_at") or "", row["id"])


def _dedupe_videos_by_canonical(conn) -> tuple[int, int]:
    """把 videos 上 canonical_url 相同的行合成一行（工单 #25）。

    为什么必须去重：``reserve_video`` 的并发安全靠的是 INSERT 撞唯一索引
    （IntegrityError 那一支）。没有 canonical 上的唯一索引，两个用户同时粘
    两种形态就会插出两行——而那正是本工单要消灭的重复。

    合并规则（每一条都有代价，不是随手定的）：
    - **幸存者 = 更新**（时间优先，id 兜底）。
    - 每个内容列取组内**最新的非空值**：两条重复行往往互补。
    - ``status`` 取 ready 优先：只有一条是 pending 而另一条已经 ready 时，
      合成 pending 会让社区内容重新变成「还在解析」，而那份内容明明存在。
    - ``created_at`` 取**最早**：它是「社区里第一次出现这个视频」的时间。
    - ``parsed_by`` 取幸存者的（= 最新的）。这是**产品语义**不是工程细节：
      它决定谁能重新解析这一条（can_regenerate 与 complete_video 的
      ``WHERE parsed_by IS ?`` 都落在它上面）。
    - ``video_url`` 取幸存者的：原文要留着**回显**（工单 #25 第 2 问）。

    **正在被覆盖的一组整组跳过**：regenerating_by 非空说明有人正在改写它，
    合并不去抢别人的锁、也不把它归零——归零会让那次覆盖静默失败。

    返回 (被合并掉的行数, 跳过的组数)。跳过的那几组留给
    _migrate_videos_canonical_unique 决定不建唯一索引。
    """
    groups: dict[str, list[dict]] = {}
    for row in conn.execute(
        """SELECT id, video_url, canonical_url, status, parsed_by,
                  regenerating_by, created_at, updated_at,
                  video_title, cover_url, summary_md, mindmap_md,
                  subtitle_text, tags
           FROM videos WHERE COALESCE(canonical_url, '') <> '' ORDER BY id"""
    ):
        groups.setdefault(row["canonical_url"], []).append(dict(row))

    removed = skipped = 0
    for canonical, rows in groups.items():
        if len(rows) < 2:
            continue
        if any(r["regenerating_by"] is not None for r in rows):
            skipped += 1
            continue

        ordered = sorted(rows, key=_row_order_key)
        winner = dict(ordered[-1])

        merged = {col: "" for col in _VIDEO_CONTENT_COLUMNS}
        for row in ordered:  # 正序覆盖 → 最终留下的是最新的非空值
            for col in _VIDEO_CONTENT_COLUMNS:
                value = row.get(col)
                if value not in (None, ""):
                    merged[col] = value

        # status：pending 不许盖掉 ready。
        if winner.get("status") != VIDEO_STATUS_READY:
            winner["status"] = VIDEO_STATUS_READY if any(
                r.get("status") == VIDEO_STATUS_READY for r in ordered
            ) else winner["status"]

        conn.execute(
            """UPDATE videos SET status = ?, parsed_by = ?, created_at = ?,
                      video_title = ?, cover_url = ?, summary_md = ?,
                      mindmap_md = ?, subtitle_text = ?, tags = ?
               WHERE id = ?""",
            (
                winner["status"], winner["parsed_by"], ordered[0].get("created_at") or "",
                merged["video_title"], merged["cover_url"], merged["summary_md"],
                merged["mindmap_md"], merged["subtitle_text"], merged["tags"],
                winner["id"],
            ),
        )
        losers = [r["id"] for r in ordered[:-1]]
        conn.execute(
            f"DELETE FROM videos WHERE id IN ({','.join('?' * len(losers))})", losers
        )
        removed += len(losers)
    return removed, skipped


def _migrate_videos_canonical_unique(conn) -> int:
    """去重之后把 canonical 上的索引升级成 UNIQUE（工单 #25）。

    顺序不能反：先合成重复行，再建唯一索引。老库里已经有重复行时
    ``CREATE UNIQUE INDEX`` 直接失败，init_db 整段 abort——
    症状是「本地好好的，一升级就打不开」。这与
    _migrate_parse_history_unique_url 是同一套理由。

    **有组被跳过就不建唯一索引**（本轮拍板的决定）：去重不彻底时建唯一索引
    要么失败、要么靠运气。宁可让这条索引暂时保持普通索引——那是**可观察**的
    （``PRAGMA index_list(videos)`` 里 unique=0），下一次启动会再试一次。
    绝不为了建一条索引去强抢别人正在进行的覆盖。

    索引带 ``WHERE canonical_url <> ''``：canonical 为空的行不受约束。
    canonical 为空只可能是 video_url 本身为空，而那种行本来就没有
    「同一链接」的语义，让它参与唯一约束只会让建索引失败。

    返回被合并掉的行数。
    """
    existing = conn.execute("PRAGMA index_list(videos)").fetchall()
    if any(r["name"] == "idx_videos_canonical" and r["unique"] for r in existing):
        # 已经 UNIQUE —— 有唯一索引就不可能有重复行，连表都不用扫。
        return 0

    removed, skipped = _dedupe_videos_by_canonical(conn)
    if skipped:
        print(
            "[database] videos 去重跳过了 "
            f"{skipped} 组（这些组里有一行正在被覆盖，regenerating_by 非空）。"
            "本次**不**建立 canonical 唯一索引，下一次启动会再试。"
            f" 已合并 {removed} 行。",
            file=sys.stderr,
        )
        return removed

    # 老库里那条同名**普通**索引必须先删：CREATE UNIQUE INDEX IF NOT EXISTS
    # 遇到同名索引是空操作，不删就会静默留下一条普通索引，
    # 而代码与注释都以为唯一性已经成立。
    conn.execute("DROP INDEX IF EXISTS idx_videos_canonical")
    conn.execute(
        "CREATE UNIQUE INDEX idx_videos_canonical ON videos(canonical_url) "
        "WHERE canonical_url <> ''"
    )
    return removed


def _create_canonical_url_triggers(conn) -> None:
    """给三张表挂 AFTER INSERT 触发器，把空的 canonical_url 就地填上。

    为什么需要这一层兜底：第 4 片之后**所有读出口**都按 canonical 比较，
    而这一列不空这件事此前只靠「三个写入路径都记得填」来保证——那是约定，
    不是保证。任何一条直接 INSERT 的路径（测试造老库、造边界态，
    将来还可能有导入脚本）都会留下一批空值行，于是那些行在任何 canonical
    查询里都匹配不上：症状是「换个形态打开同一个视频，说没解析过」，
    而**不报错**。

    落在数据库层而不是调用方，是因为这与本仓既有的 FTS 触发器同一套路：
    那条也正是「每条 UPDATE videos 的路径都会自动重建索引，
    不必记得回头调一次重新索引」。漏掉一次就是一条静默搜不到的视频。

    ⚠️ AFTER 触发器里的 UPDATE 会让唯一索引在那次 UPDATE 上**再检查一次**：
    第二次插入先以 canonical='' 通过部分索引（`WHERE canonical_url <> ''`
    本就不约束空值），随后 UPDATE 成真值时唯一约束生效、整条 INSERT 原子回滚。
    所以 `reserve_video` 依赖 ``except sqlite3.IntegrityError`` 的并发安全
    不受影响——由 test_reserving_a_second_form_does_not_create_a_second_row 守着。

    只在**为空且 video_url 非空**时才动：video_url 本身为空的那种行没有
    「同一链接」的语义，让它参与唯一约束只会让建索引失败。
    """
    for table in _CANONICAL_URL_TABLES:
        conn.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {table}_fill_canonical
                AFTER INSERT ON {table}
                WHEN NEW.canonical_url = '' AND COALESCE(NEW.video_url, '') <> ''
            BEGIN
                UPDATE {table} SET canonical_url = canonical_url_of(NEW.video_url)
                WHERE id = NEW.id;
            END"""
        )


#: 需要 canonical_url 的三张表。顺序无关，每张都独立判存在性。
_CANONICAL_URL_TABLES = ("videos", "parse_history", "chat_messages")


def _migrate_canonical_url_columns(conn) -> int:
    """给三张表补 canonical_url 列，并把存量行回填成它的归一值（工单 #25）。

    为什么必须单独一步：`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，
    #6 建的老库里不会凭空多出列，而下面那条 CREATE INDEX 一建就 no such column，
    整段脚本直接 abort —— 症状是「本地好好的，一升级就打不开」。

    回填而不是「只对新行生效」：库里已经躺着的那些行才是要救的那批，
    而新行由三个 INSERT 路径自己填（见 reserve_video / upsert_parse_history /
    append_chat_turn）。只对新行生效的话，升级后的老数据会永远落空——

    而且是**静默**落空：列存在、有索引、查得到行，只是值全是空的。

    幂等：只在值为空时才回填，所以重复跑 init_db 不会覆盖任何已经算好的值，
    也不会与将来某个 UPDATE 抢同一列（目前全库没有 UPDATE 会碰它）。

    返回被回填的行数（0 表示库里本来就干净，或已经是新结构）。
    """
    filled = 0
    for table in _CANONICAL_URL_TABLES:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "canonical_url" not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN canonical_url TEXT DEFAULT ''")
        rows = conn.execute(
            f"SELECT id, video_url FROM {table} WHERE COALESCE(canonical_url, '') = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                f"UPDATE {table} SET canonical_url = ? WHERE id = ?",
                (canonical_video_url(row["video_url"]), row["id"]),
            )
        filled += len(rows)

    # 索引**不**放在这三张表上：chat_messages 与 parse_history 的读出口在第 4 片
    # 才切过来，现在建索引只是给一个还不会被查的列建索引。
    # videos 那条必须在这里建——它是社区去重的主查询路径（`WHERE canonical_url = ?`），
    # 而 videos 刻意不做条数裁剪，全表扫会随社区增长线性变慢。
    #
    # 这里只建**普通**索引：唯一化必须排在去重之后（第 3 片），
    # 否则老库里已有的重复行会让 CREATE UNIQUE 直接失败。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_canonical ON videos(canonical_url)"
    )
    return filled


#: 合成历史重复行时，「空」的判定覆盖的 JSON 空壳。
#: chat_history 的默认值是 '[]'，它与 '' 在「用户还没问过任何一句」这件事上同义。
_PARSE_HISTORY_BLANK = (None, "", "[]")


def _dedupe_parse_history(conn) -> int:
    """把 parse_history 上已有的 (user_id, video_url) 重复行合成一行。

    必须在建 UNIQUE 索引**之前**跑：老库里已经有重复行时，
    ``CREATE UNIQUE INDEX`` 会直接失败，init_db 整段 abort——
    表现是「本地好好的，一升级就打不开」。

    合成规则（每一条都是为了不丢用户已经攒下的东西）：

    - **幸存者是最新那条**（按 updated_at / created_at，再按 id）；
    - 每一列取组内**最新的非空值**。两条重复行往往是互补的：
      一条只有视频源信息（App.vue 的 persistParseRecord），
      另一条只有 AI 产出（VideoSummary 的 persistHistory）。
      只留最新那条会把另一半悄悄抹掉。
    - ``is_favorite`` 取组内**或**。收藏是这个产品里唯一一处
      「用户明说了别删它」的地方，合成时丢掉它等于替用户做了决定。
    - ``created_at`` 取**最早**那条：它是「我第一次解析这个视频」的时间，
      取最新会把老记录伪装成新记录，进而在滚动裁剪里多占一格。

    返回被删掉的行数（0 表示库里本来就干净）。
    """
    groups: dict[tuple, list] = {}
    for row in conn.execute(
        """SELECT id, user_id, video_url, video_title, video_data, summary_md,
                  mindmap_md, subtitle_data, chat_history, is_favorite,
                  created_at, updated_at
             FROM parse_history ORDER BY user_id, video_url, id"""
    ).fetchall():
        groups.setdefault((row["user_id"], row["video_url"]), []).append(row)

    def newest_non_empty(group, column):
        for row in reversed(group):
            if row[column] not in _PARSE_HISTORY_BLANK:
                return row[column]
        return ""

    removed = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        survivor = group[-1]
        conn.execute(
            """UPDATE parse_history
               SET video_title=?, video_data=?, summary_md=?, mindmap_md=?,
                   subtitle_data=?, chat_history=?, is_favorite=?,
                   created_at=?, updated_at=?
               WHERE id=?""",
            (
                newest_non_empty(group, "video_title"),
                newest_non_empty(group, "video_data"),
                newest_non_empty(group, "summary_md"),
                newest_non_empty(group, "mindmap_md"),
                newest_non_empty(group, "subtitle_data"),
                newest_non_empty(group, "chat_history") or "[]",
                1 if any(r["is_favorite"] for r in group) else 0,
                group[0]["created_at"] or survivor["created_at"],
                survivor["updated_at"] or survivor["created_at"],
                survivor["id"],
            ),
        )
        conn.execute(
            f"DELETE FROM parse_history WHERE id IN "
            f"({','.join('?' * (len(group) - 1))})",
            [r["id"] for r in group[:-1]],
        )
        removed += len(group) - 1
    return removed


def _migrate_parse_history_unique_url(conn) -> int:
    """把 (user_id, video_url) 收敛成一键，并把那条索引升级成 UNIQUE。

    为什么要 UNIQUE：``upsert_parse_history`` 原来是「先 SELECT 再 INSERT」的
    读-改-写，两个并发请求各自查到「还没有这行」就各插一行——
    docstring 承诺的「按 (user_id, video_url) 去重」在并发下并不成立
    （实测 10 线程同时写同一对 → 2 行）。数据库层没有任何兜底：
    ``idx_history_user_url`` 当时是**普通索引**，不是唯一索引。

    触发条件现实存在：``App.vue`` 的 persistParseRecord 与
    ``VideoSummary.vue`` 的 persistHistory 是两个互不相干的组件，
    都会对同一 URL 发 save，用户连点或重试即可能并发。

    顺序不能反：先合成重复行，再建唯一索引。返回被删掉的重复行数。
    """
    existing = conn.execute("PRAGMA index_list(parse_history)").fetchall()
    if any(r["name"] == "idx_history_user_url" and r["unique"] for r in existing):
        # 已经是 UNIQUE —— 有唯一索引就不可能有重复行，连表都不用扫。
        return 0
    removed = _dedupe_parse_history(conn)
    # 老库里那条同名普通索引必须先删：CREATE UNIQUE INDEX IF NOT EXISTS
    # 遇到同名索引是**空操作**，不删就会静默地留下一条普通索引，
    # 而代码与注释都以为唯一性已经成立。
    conn.execute("DROP INDEX IF EXISTS idx_history_user_url")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_history_user_url "
        "ON parse_history(user_id, video_url)"
    )
    return removed


def _migrate_mindmap_levels(conn) -> int:
    """把存量导图收拾到当前结构规则之内（工单 #106 / #111 / ADR 0027、0029）。

    返回被改写的行数。

    ## 为什么需要这一步

    导图退化（只剩一根）发生在**写库那一刻**：模型给的导图在清洗后层级
    不足 2，落库的就是一条光杆。而清洗只作用于**新**的总结——存量行里的
    导图永远不会被再洗一次。所以「只对新总结生效」等于没改：库里躺着的
    存量导图还是一根光杆。

    ## 两类要收拾的存量行

    * **退化的行**（只有一级标题，没有分支）：按同一行的总结正文
      （``summary_md``）重建——正文里已经有现成的结构（``##`` / ``###``
      标题），它是重建导图唯一可靠的来源，不用猜，也不用重新调模型。
    * **层级够但超上限的行**（同一父节点下超过 7 个子节点，或全图超过
      30 个节点）：按当前规则截回上限之内（``clean_mindmap_markdown``）。
      这一类**不拿总结正文替换**——重建会丢掉原图独有的细节，截断只丢
      超出的那部分。

    ## 例外：没有正文的记录保持原样

    没有总结正文就没有重建依据。这类行**原样保留**：宁可留一根光杆，
    也不拿别处的文字编一棵导图出来。

    ## 幂等

    收拾过的导图层级 ≥2 且在上限之内，下一次启动就不再命中任何一条
    条件；即使命中，结果与原值相同也不会写库。重复执行不产生额外改动。
    """
    rebuilt = 0
    for table in ("videos", "parse_history"):
        rows = conn.execute(
            f"SELECT id, summary_md, mindmap_md FROM {table} "
            f"WHERE COALESCE(mindmap_md, '') != ''"
        ).fetchall()
        for row in rows:
            md = row["mindmap_md"]
            if is_degenerate(md):
                summary = (row["summary_md"] or "").strip()
                if not summary:
                    # 无正文：没有重建依据，保持原样
                    continue
                new_md = rebuild_mindmap_from_summary(summary)
            elif exceeds_limits(md):
                # 层级够但超上限：按当前规则截回上限之内（不换内容，只丢超出的）
                new_md = clean_mindmap_markdown(md)
            else:
                # 层级够、也在上限之内：一个字符都不动
                continue
            if not new_md or new_md == md:
                continue
            conn.execute(
                f"UPDATE {table} SET mindmap_md = ? WHERE id = ?",
                (new_md, row["id"]),
            )
            rebuilt += 1
    return rebuilt


def _migrate_history_favorite_column(conn) -> None:
    """给 parse_history 补上 is_favorite 列。

    理由同 _migrate_video_card_columns：``CREATE TABLE IF NOT EXISTS``
    对已存在的表是空操作，老库里不会凭空多出这一列，而收藏的读写都要
    SELECT 它 —— 不补列，代码一跑就 no such column。

    默认 0（未收藏），所以老记录升级过来不会被当成收藏。

    **表还不存在时什么都不做**：``init_db`` 把本函数排在 executescript 之前
    调用，而全新库的 parse_history 那一刻还不存在，ALTER 会直接抛
    ``no such table``。那种情况下随后 CREATE TABLE 自带这一列。
    """
    existing = {
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='parse_history'"
        )
    }
    if not existing:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(parse_history)")}
    if "is_favorite" not in columns:
        conn.execute(
            "ALTER TABLE parse_history ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0"
        )


#: 全文检索索引表名。
#:
#: 它是 videos 的**派生索引**，不是第二份数据：content='videos' 让索引的行
#: 直接指向内容表的那一行，存两份文本就意味着两份可能不一致的数据——
#: 那正是工单 #7 要消灭的「同一链接两份总结」的同源问题。
_VIDEO_SEARCH_INDEX = "videos_fts"

#: FTS5 分词器。
#:
#: 中文只能用 trigram：默认的 unicode61 按空白与标点切词，一整句没有空格
#: 的中文会被当成**一个**词，于是搜「异步」永远匹配不上「深入理解异步编程」
#: ——实测召回 0。trigram 按 3 字符滑窗切，中文子串因此可召回。
_VIDEO_SEARCH_TOKENIZER = "trigram"

#: trigram 的最小可匹配长度：不足 3 个字符的词在索引里没有对应 trigram，
#: 查了必然是空（实测 2 字中文词在 MATCH 路径召回 0，3 字起正常）。
#: 按标签精确筛选不受此限——它走 json_each，不经 FTS。
#:
#: 注意：这是 **MATCH 路径**的地板，不是搜索能力的总下限。
#: 总下限是 MIN_SEARCH_TERM_CHARS——2 字词另有子串兜底。
MIN_FTS_TERM_CHARS = 3

#: 关键词搜索的总下限：2 个字符。
#:
#: 2 字词走子串兜底（见 _search_substring），所以它搜得到；
#: 1 字仍然没有意义——单个汉字在社区正文里到处都是，命中一片等于没筛。
#: 与其给用户一片噪音，不如直说「太短」。
MIN_SEARCH_TERM_CHARS = 2

#: 索引覆盖的列集合（ADR 0030）。
#:
#: 为什么是这 5 列：原先只索引标题、标签、链接三列，于是**只出现在正文里
#: 的词一条都搜不到**——同一份 96 条查询语料上的实测：正文词召回
#: 0/51（0.0%）；加上总结列是 18/51（35.3%）；**总结与字幕一起**才是
#: 51/51（100%）。字幕必须进索引：总结是模型压缩过的，字幕才是视频
#: 真正讲了什么。
#:
#: 代价是索引体积 36KB → 68KB（约 1.9 倍），换来正文可检索。
#: mindmap_md 刻意不索引：导图文本与总结大量重复，进索引只是同一段话
#: 被索引两遍，体积再涨一分、召回不涨一分。
#:
#: 顺序即 CREATE VIRTUAL TABLE 的列顺序。老库的列集合与此不同时，
#: _create_video_search_index 会重建整张索引表（那里有说明）。
_VIDEO_SEARCH_COLUMNS = ("video_title", "tags", "video_url", "summary_md", "subtitle_text")

#: 触发器 SQL 里要落的列。三个触发器共用同一份列清单——
#: 写死三处的话，加列时漏掉一处就是某条写路径上的索引静默失效。
_SEARCH_INDEX_COLS = ", ".join(_VIDEO_SEARCH_COLUMNS)
_SEARCH_INDEX_NEW_VALS = ", ".join("new." + c for c in _VIDEO_SEARCH_COLUMNS)
_SEARCH_INDEX_OLD_VALS = ", ".join("old." + c for c in _VIDEO_SEARCH_COLUMNS)

_VIDEO_SEARCH_TRIGGER_SETUP = f"""
    CREATE TRIGGER IF NOT EXISTS videos_fts_ai AFTER INSERT ON videos BEGIN
        INSERT INTO {_VIDEO_SEARCH_INDEX}(rowid, {_SEARCH_INDEX_COLS})
        VALUES (new.id, {_SEARCH_INDEX_NEW_VALS});
    END;

    CREATE TRIGGER IF NOT EXISTS videos_fts_ad AFTER DELETE ON videos BEGIN
        INSERT INTO {_VIDEO_SEARCH_INDEX}({_VIDEO_SEARCH_INDEX}, rowid,
            {_SEARCH_INDEX_COLS})
        VALUES ('delete', old.id, {_SEARCH_INDEX_OLD_VALS});
    END;

    CREATE TRIGGER IF NOT EXISTS videos_fts_au AFTER UPDATE ON videos BEGIN
        INSERT INTO {_VIDEO_SEARCH_INDEX}({_VIDEO_SEARCH_INDEX}, rowid,
            {_SEARCH_INDEX_COLS})
        VALUES ('delete', old.id, {_SEARCH_INDEX_OLD_VALS});
        INSERT INTO {_VIDEO_SEARCH_INDEX}(rowid, {_SEARCH_INDEX_COLS})
        VALUES (new.id, {_SEARCH_INDEX_NEW_VALS});
    END;
"""


def _video_search_index_columns(conn) -> tuple:
    """索引表现在有哪些列。空元组 = 表还不存在。

    FTS5 的虚表也吃 PRAGMA table_info（实测能列出列名），
    所以老库的列集合是**问得出来的**——这是「要不要重建」的唯一判据，
    不靠「有没有报错」猜。
    """
    rows = conn.execute(f"PRAGMA table_info({_VIDEO_SEARCH_INDEX})").fetchall()
    return tuple(r["name"] for r in rows)


def _create_video_search_index(conn) -> None:
    """建（或补齐）社区全文检索索引：外部内容 FTS5 表 + 三个同步触发器。

    触发器是这张表与 videos 之间唯一的同步机制，写在库里而不是应用代码里：
    任何一条 UPDATE videos 的路径——包括 complete_video——都会自动重建该行的
    索引，不必记得回头调一次「重新索引」。漏掉一次就是一条静默搜不到的视频。

    'rebuild' 幂等：老库里在触发器建立之前就有的行会被全量灌进索引，
    重复执行只是重建，不会重复插入。

    **老库的列集合不同 → 重建整张索引表**（ADR 0030）。
    FTS5 的外部内容表不支持 ALTER ADD COLUMN，加列只能 DROP 重建；
    实测 DROP TABLE **不会**带走建在它上面的三个触发器，留着就会在重建时
    报 `trigger videos_fts_ai already exists`。所以顺序是：
    先删触发器 → 删表 → 建表 → 建触发器 → rebuild。
    重建后存量行立刻可搜——rebuild 会把内容表的全量灌进索引。
    """
    existing = _video_search_index_columns(conn)
    if existing and existing != _VIDEO_SEARCH_COLUMNS:
        conn.executescript(
            "DROP TRIGGER IF EXISTS videos_fts_ai;"
            "DROP TRIGGER IF EXISTS videos_fts_au;"
            "DROP TRIGGER IF EXISTS videos_fts_ad;"
        )
        conn.execute(f"DROP TABLE IF EXISTS {_VIDEO_SEARCH_INDEX}")
    conn.executescript(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {_VIDEO_SEARCH_INDEX} USING fts5(
            {_SEARCH_INDEX_COLS},
            content='videos', content_rowid='id',
            tokenize='{_VIDEO_SEARCH_TOKENIZER}'
        );
    """)
    conn.executescript(_VIDEO_SEARCH_TRIGGER_SETUP)
    conn.execute(
        f"INSERT INTO {_VIDEO_SEARCH_INDEX}({_VIDEO_SEARCH_INDEX}) VALUES ('rebuild')"
    )


# ── 可见性：对外投影的字段白名单 ────────────────────────────
#
# 白名单，不是黑名单。差别在于**新增列会不会自动泄漏**：
# 逐个剔除的写法里，往 videos 加一列的人不必改任何东西，那一列就已经
# 出现在未登录访客的响应里了；白名单下它根本不会被构造出来。
#
# 键集合由测试逐个断言（set(...) == {...}），不是「断言某个键不在」。

#: 未登录访客在社区**列表**里能看到的全部键。id / video_url 是标识而非内容：
#: 卡片要能被点开、详情要能被寻址，没有它们列表页无法导航；
#: 它们不承载任何字幕 / 总结 / 思维导图文本。
COMMUNITY_CARD_FIELDS = ("id", "video_url", "cover_url", "video_title", "tags")

#: 已登录用户看**详情**时能拿到的键。列表无论登录与否都只用上面的白名单——
#: 列表是公开入口，把内容塞进去等于让未登录访客多拿一份。
COMMUNITY_DETAIL_FIELDS = COMMUNITY_CARD_FIELDS + (
    "summary_md", "mindmap_md", "subtitle_text", "created_at", "updated_at",
)

COMMUNITY_PAGE_SIZE_DEFAULT = 20
COMMUNITY_PAGE_SIZE_MAX = 100


def _decode_tags_text(raw) -> list:
    """把 tags 列的 JSON 文本还原成字符串数组。

    收在一处是因为这段曾经在这个文件里长出过四份逐字重复的副本，各写各的。
    现已全部收口：下面三个出口都调这一个函数，规则改动只改这一处。
    类型不对就当没有，不让脏数据变成下游的 TypeError。

    ⚠️ **过滤非字符串元素是刻意口径，不是遗漏**（工单 #35）。它解的 tags 列
    存的就是字符串数组，下游拿到的每一项都要当字符串用，混进数字会一路走到
    界面上才炸。同一个文件里的 `_legacy_chat_history` **不**过滤，
    因为那一列存的是对象数组 —— 详见那里的说明（含实测对照）。
    两者的差异已由 `tests/test_json_array_decoders.py` 钉住。
    """
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        parsed = []
    if not isinstance(parsed, list):
        return []
    return [t for t in parsed if isinstance(t, str)]


def _project_video(row, fields: tuple) -> dict:
    """按字段白名单投影一行，tags 由 JSON 文本还原成数组。

    清洗规则不在这里另写一份，交给 _decode_tags_text —— 与 get_video_by_url
    共用同一份实现（工单 #32 收口），规则改动只改那一处。
    """
    item = {name: row[name] for name in fields}
    item["tags"] = _decode_tags_text(item.get("tags"))
    return item


#: 页码的绝对上界。
#:
#: 它存在的原因不是「怕有人翻太多页」，而是**防整数溢出**：SQLite 的
#: INTEGER 是 64 位有符号，Python 整数却是任意精度。page 没有上界时
#: `?page=9223372036854775807` 会让 OFFSET 变成一个绑不进去的整数，
#: sqlite3 抛 "Python int too large to convert to SQLite INTEGER"，
#: 路由层没捕获 → 500。而社区列表是**公开入口，不需要登录**。
#:
#: 10^9 页 × 每页上限 100 = 10^11，离 2^63 还剩八个数量级的余量，
#: 真实数据量永远到不了这个页数，所以钳到它只是让「翻过头」表现得
#: 与「翻过头没东西」一致，而不是打成 500。
_MAX_SAFE_PAGE = 1_000_000_000


def _clamp_page(page: int, page_size: int) -> tuple[int, int]:
    """页码与页长收敛到安全范围。**两个方向都要收。**

    - page < 1 归一到 1；page 另有上界 _MAX_SAFE_PAGE（否则 OFFSET 溢出）。
    - page_size < 1 归一到 1，上限封顶：它直接进 LIMIT，一个巨大的
      page_size 会让数据库先扫过整张表再丢掉。
    """
    try:
        page = int(page)
        page_size = int(page_size)
    except (TypeError, ValueError):
        page, page_size = 1, COMMUNITY_PAGE_SIZE_DEFAULT
    return (max(1, min(page, _MAX_SAFE_PAGE)),
            max(1, min(page_size, COMMUNITY_PAGE_SIZE_MAX)))


def _paginate(from_clause: str, where: str, params: tuple, page: int,
              page_size: int, order_by: str) -> dict:
    """跑一次 count 与一次取页，返回统一的分页信封。

    from / where / order 都由调用方给全，**不套子查询**：FTS5 的 bm25() 是
    辅助函数，必须与 FTS 表在**同一条 SELECT** 里可见，一旦裹进
    `SELECT * FROM ( ... JOIN videos_fts f ... )` 就会报
    no such column: bm25 —— 索引还在，只是排不了序。
    """
    page, page_size = _clamp_page(page, page_size)
    with get_db() as conn:
        total = conn.execute(
            f"SELECT count(*) FROM {from_clause} WHERE {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT v.* FROM {from_clause} WHERE {where} ORDER BY {order_by}"
            " LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "items": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
    }


#: 社区内容的可见条件。status='ready' 是硬条件：pending 行是**占位**，
#: 里面没有总结也没有字幕，把它当内容展示就是给用户看一个空壳。
#: （brief 与票面都点名过这条。）
_COMMUNITY_VISIBLE = "v.status = 'ready'"


def _split_tags(tag) -> list:
    """把 ``"编程,架构设计"`` / ``["编程", "架构设计"]`` 统一成去空后的列表。"""
    if tag is None:
        return []
    raw = tag if isinstance(tag, (list, tuple)) else str(tag).split(",")
    out = []
    for t in raw:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return out


def _tag_clause(tag, alias: str = "v") -> tuple[str, tuple]:
    """按一组标签精确筛选（**并集**：命中任一即可）。

    为什么并集不是交集：交集在两个标签很少共同出现时直接返回空，
    而「选了筛选反而一条都没有」在界面上和「筛选坏了」完全一样。
    并集永远给得出东西，选错方向也就不会长得像 bug。

    刻意走 json_each 而不是 FTS：trigram 匹配不到 2 个字符的词
    （实测「编程」召回 0），而标签里大量是 2 字词。标签是**枚举值**，
    精确匹配既更快也更准。

    json_valid 不是保险是必需：videos.tags 是存 JSON 的 TEXT 列，
    坏一行 json_each 就抛，而调用方是列表接口 —— 一行坏数据 = 整个
    页面 500。COALESCE 同理：历史页那边是 LEFT JOIN，v.tags 可能是 NULL。
    """
    names = _split_tags(tag)
    if not names:
        return "", ()
    marks = ", ".join("?" * len(names))
    return (
        f" AND EXISTS (SELECT 1 FROM json_each("
        f"  CASE WHEN json_valid(COALESCE({alias}.tags, '[]'))"
        f"       THEN {alias}.tags ELSE '[]' END"
        f") WHERE value IN ({marks}))",
        tuple(names),
    )


def list_community_videos(page: int = 1, page_size: int = COMMUNITY_PAGE_SIZE_DEFAULT,
                         tag: str = "") -> dict:
    """社区列表（对未登录访客公开）。返回白名单投影后的行。

    翻页与按标签筛选都在这里，且**不要求登录**——它们是列表本身的用法，
    不是特权。
    """
    clause, params = _tag_clause(tag)
    where = f"{_COMMUNITY_VISIBLE}{clause}"
    order = "v.created_at DESC, v.id DESC"
    result = _paginate("videos v", where, params, page, page_size, order)
    result["items"] = [_project_video(r, COMMUNITY_CARD_FIELDS) for r in result["items"]]
    return result


def list_community_tags() -> list:
    """社区里出现过的全部标签及各自条数（只数 ready 行）。

    给社区页的标签筛选当**选项来源**。刻意不由前端从当前页汇总：
    那样一来翻页或一筛选，标签就会增减，用户读起来是「筛选不生效」。

    json_valid 守卫是必需的而不是保险：videos.tags 坏一行，json_each
    就抛，而这一行正是整个标签行的数据源 —— 抛了就是整行标签都没了。
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT je.value AS tag, count(*) AS n
               FROM videos v
               JOIN json_each(
                   CASE WHEN json_valid(COALESCE(v.tags, '[]'))
                        THEN v.tags ELSE '[]' END) je
               WHERE v.status = 'ready'
               GROUP BY je.value
               ORDER BY n DESC, je.value""",
        ).fetchall()
    return [{"tag": r["tag"], "count": r["n"]} for r in rows]


def get_community_video(video_id: int) -> dict | None:
    """社区视频详情（调用方必须已鉴权）。仅 ready 行。"""
    with get_db() as conn:
        row = conn.execute(
            f"SELECT v.* FROM videos v WHERE v.id = ? AND {_COMMUNITY_VISIBLE}",
            (video_id,),
        ).fetchone()
    return _project_video(row, COMMUNITY_DETAIL_FIELDS) if row else None


def publish_video_card(video_url: str, video_title: str = "", cover_url: str = "") -> int:
    """回填社区卡片的标题与封面，返回更新的行数。

    三条约束，各有代价，都不是随手加的：

    1. 必须是 ready。占位（pending）行还没有社区内容，此刻写标题等于
       给一个迟早可能被 release_video 删掉的行做卡片，列表会闪出一条
       点进去什么都没有的记录。
    2. 只写这两列。标题封面是**平台元数据**，与谁解析、何时解析无关；
       顺带把 tags 或 summary 一起改掉就是绕过 #6「ready 行谁都不能改写」
       的承诺，从后门开了一条写路径。
    3. **只填空，不覆盖**。标题与封面是用户判断内容和搜索的入口：任何登录
       用户都能改写它，就等于给了一条「把别人的视频改成别的样子」的路子，
       还会静默改动 FTS 索引——与「社区内容不会被别人改写」直接冲突。
       所以已填的字段谁都动不了，先到先得。
       代价：第一个填的人如果填错了，这个字段就错下去了；修它需要人工改库。

    返回值是**诚实的**：SQLite 的 changes() 统计的是「被 UPDATE 语句触及的行」，
    `SET col='same'` 也会算 1。所以 WHERE 里除了「至少一列为空」，还要求
    「传进来的那个值确实非空」——否则第二个只想补封面的调用者会拿到
    updated=1，而实际上一个字段都没写进去。
    """
    if not video_title and not cover_url:
        return 0
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE videos
                  SET video_title = CASE WHEN COALESCE(video_title, '') = '' THEN ? ELSE video_title END,
                      cover_url    = CASE WHEN COALESCE(cover_url, '')    = '' THEN ? ELSE cover_url    END,
                      updated_at   = ?
                WHERE canonical_url = ? AND status = 'ready'
                  AND ((COALESCE(video_title, '') = '' AND ? <> '')
                    OR (COALESCE(cover_url, '') = '' AND ? <> ''))""",
            (video_title, cover_url,
             datetime.now(timezone.utc).isoformat(), canonical_video_url(video_url),
             video_title, cover_url),
        )
        return cursor.rowcount


#: 搜索词里要剥掉的字符：全部 C0 控制字符（含 NUL）与 DEL。
#:
#: NUL 是**实测致命**的：它进 FTS5 字符串字面量会让 trigram 分词器判定
#: 字符串未闭合，直接抛 `OperationalError: unterminated string` → 500。
#: 而 `?q=%00` 浏览器与 curl 都发得出去，不是不可达路径。
#:
#: 其余 C0 控制字符实测（LF / TAB）返回 200，但 SQLite 各版本对它们的
#: 处理并无文档化承诺，一并剥掉是防御性做法：搜索词里本来就不该有它们。
_SEARCH_STRIP_CHARS = {c: None for c in range(0x20)}


def _fts_phrase(q: str) -> str:
    """把用户输入包成 FTS5 字符串字面量。

    两件事，缺一不可，顺序也不能换：

    1. **剥控制字符**（NUL 在内）——见 _SEARCH_STRIP_CHARS。少了这步，
       `?q=%00` 就是一个 500。
    2. **双写单引号**包成字符串字面量。FTS5 查询串有自己的语法，直接把
       用户输入当查询丢进去会抛 OperationalError——实测 '"unterminated'、
       'a OR'、"foo'bar" 三种都抛。后果是「搜一下就把搜索接口打成 500」，
       而且 500 看起来与输入有关，用户只会以为自己输错了。
       FTS5 字符串字面量的转义是**双写单引号**，不是反斜杠。
    """
    return '"' + q.translate(_SEARCH_STRIP_CHARS).replace('"', '""') + '"'


def _escape_like(term: str) -> str:
    """把 LIKE 的元字符转义掉。

    LIKE 认三个元字符：`%`（任意长）、`_`（任意单字符）、`\\`（转义符本身）。
    不转义的话，用户搜「100%」会命中一切含「100」的行，搜「C++」里的下划线
    同理——实测「带 ESCAPE 且逐字符转义」与真值表完全一致，
    「不带 ESCAPE」时 `%`/`_` 变成通配符，误召回一片。
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_substring(term: str, tag_clause: str, tag_params: tuple,
                      page: int, page_size: int) -> dict:
    """2 字词的子串兜底：在**原始 videos 表**上做 LIKE 子串匹配。

    为什么不在 FTS5 表上做：FTS5 外部内容表上的 LIKE 会**穿过 content 表读值**
    （实测：没同步进索引的行也会被 LIKE 命中），可见性口径与 MATCH 不一致。
    原始表上匹配，口径才与「社区里能看到的行」一致。

    为什么只给 2 字用：≥3 字一律走 MATCH——FTS5 的短语语义与 bm25 排序都在
    那里。把 3 字以上也交给 LIKE，整个检索就退化成朴素扫描。

    搜的列与索引的列**一致**（标题、标签、链接、总结、字幕），
    否则「索引里搜得到、兜底里搜不到」会成为一条随口径漂移的规则。

    顺序用时间倒序而非相关度：LIKE 没有相关度可言，硬排一个会骗人。
    """
    pattern = "%" + _escape_like(term) + "%"
    like_clause = " OR ".join(
        "COALESCE(v." + col + ", '') LIKE ? ESCAPE '\\'" for col in _VIDEO_SEARCH_COLUMNS
    )
    where = f"({like_clause}) AND {_COMMUNITY_VISIBLE}{tag_clause}"
    params = [pattern] * len(_VIDEO_SEARCH_COLUMNS) + list(tag_params)
    return _paginate("videos v", where, params, page, page_size,
                     "v.created_at DESC, v.id DESC")


def search_community_videos(q: str = "", page: int = 1,
                           page_size: int = COMMUNITY_PAGE_SIZE_DEFAULT,
                           tag: str = "") -> dict:
    """社区搜索：视频名称关键词 / 视频链接精确定位 / 标签，三种入口。

    - q 以 http(s):// 开头 → 按 video_url **精确**匹配。链接定位要的是「就是
      这一条」，而 FTS 只能在原文里找子串，搜到的是「包含这段链接的东西」，
      两者不是一回事。URL 规范化由前端负责（它已有 canonicalUrl）。
    - 其余 q 按**长度**分三条路（ADR 0030）：

      1. ≥3 字 → FTS5 MATCH。索引覆盖标题、标签、链接、总结、字幕五列。
      2. 恰好 2 字 → trigram 够不着，走子串兜底（_search_substring）。
      3. 1 字 → 判「太短」，不查。单字在社区正文里到处都是，命中一片等于没筛。
    - tag → json_each 精确筛选，可与 q 取交集。

    调用方**必须已鉴权**：搜索是登录用户的能力，未登录返回 401（路由层
    的 Depends 负责），这里只管检索。
    """
    q = (q or "").strip()
    tag_clause, tag_params = _tag_clause(tag)

    if q.startswith(("http://", "https://")):
        where = f"{_COMMUNITY_VISIBLE} AND v.canonical_url = ?{tag_clause}"
        order = "v.created_at DESC, v.id DESC"
        result = _paginate("videos v", where, (canonical_video_url(q), *tag_params),
                           page, page_size, order)
    elif q:
        term = q.translate(_SEARCH_STRIP_CHARS)
        if len(term) < MIN_SEARCH_TERM_CHARS:
            # 1 字：命中一片等于没筛。直说太短，不给噪音。
            result = _paginate(
                "videos v", f"1 = 0 AND {_COMMUNITY_VISIBLE}{tag_clause}",
                tag_params, page, page_size, "v.created_at DESC, v.id DESC",
            )
        elif len(term) < MIN_FTS_TERM_CHARS:
            # 2 字：trigram 够不着，走子串兜底（ADR 0030）。
            result = _search_substring(term, tag_clause, tag_params, page, page_size)
        else:
            phrase = _fts_phrase(q)
            where = f"{_VIDEO_SEARCH_INDEX} MATCH ? AND {_COMMUNITY_VISIBLE}{tag_clause}"
            # 相关度优先，其次按时间倒序——同分时让新内容在前，顺序稳定可测。
            #
            # bm25() 的参数必须写 FTS 表**名**，不能写 FROM 子句里的别名：
            # 写成 bm25(f) 会报 no such column: f（SQLite 认不出这是个辅助函数，
            # 就当成列引用去找）。MATCH 左侧同理，所以 FROM 里给了别名 f、
            # 条件里仍写表名——这个不一致是实测逼出来的，不是笔误。
            result = _paginate(
                f"{_VIDEO_SEARCH_INDEX} f JOIN videos v ON v.id = f.rowid",
                where, (phrase, *tag_params), page, page_size,
                f"bm25({_VIDEO_SEARCH_INDEX}), v.created_at DESC, v.id DESC",
            )
    else:
        where = f"{_COMMUNITY_VISIBLE}{tag_clause}"
        result = _paginate("videos v", where, tag_params, page, page_size,
                           "v.created_at DESC, v.id DESC")

    result["items"] = [_project_video(r, COMMUNITY_CARD_FIELDS) for r in result["items"]]
    result["mode"] = "url" if q.startswith(("http://", "https://")) else ("text" if q else "browse")
    # 短查询的真相说给前端听：text 模式下不足 MIN_SEARCH_TERM_CHARS 个字符
    # **必然**召回 0（1 个字在社区正文里到处都是，命中一片等于没筛），
    # 接口照常 200。不把这个边界讲出来的话，用户搜「机」看到的就是「没反应」
    # ——与「社区里真没有」在界面上完全一样。URL 精确匹配与浏览模式不受
    # 这个限制（前者走等值、后者不过滤），所以只在 text 模式置位。
    #
    # min_chars 报的是**总下限**（2），不是 trigram 的 3：2 字词另有兜底，
    # 它搜得到。报 3 会让前端把「机器」这种能搜的词也劝退。
    result["min_chars"] = MIN_SEARCH_TERM_CHARS
    result["q_too_short"] = (
        result["mode"] == "text"
        and len(q.translate(_SEARCH_STRIP_CHARS)) < MIN_SEARCH_TERM_CHARS
    )
    return result


# ── 用户操作 ──────────────────────────────────────────────

def get_user_by_email(email: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def create_user(email: str, password_hash: str) -> dict:
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (email, password_hash),
        )
        return {"id": cursor.lastrowid, "email": email}


# ── AI 总结次数限制 ───────────────────────────────────────

def is_vip_active(user) -> bool:
    """VIP 权益判定——`vip_expire_at` 的**权威**比较。

    naive datetime 兜底在此收口：新增的 vip_expire_at 比较必须走这里，
    否则同一个「还是不是 VIP」的问题会在多处得到不同答案。
    现状（2026-10-07 实测，不是推断）：`api_auth._build_user_response` 里
    **仍有一份手写比较**。8 种输入形态（aware/naive 各自过期与未过期、
    脏字符串、空串、NULL、is_vip=0）逐一对齐，两边答案完全一致——
    但那是**巧合性的一致，不是构造出来的一致**：本函数 `except` 捕
    (ValueError, TypeError)，那份只捕 ValueError。
    「TypeError 那支当前不可达」是数据形态的性质（TEXT 列在 SQLite 里
    恒返回 typeof=text，实测 int/float 存进去读回都是字符串），不是代码
    给的保证。要合并成一份时，两处的异常范围必须一起改。
    """
    if not user["is_vip"] or not user["vip_expire_at"]:
        return False
    try:
        expire = datetime.fromisoformat(user["vip_expire_at"])
    except (ValueError, TypeError):
        return False
    # 兜底：SQLite 里存的可能是 naive datetime，统一按 UTC 处理
    if expire.tzinfo is None:
        expire = expire.replace(tzinfo=timezone.utc)
    return expire > datetime.now(timezone.utc)


# ── 额度拆分：解析额度 / 对话额度（工单 #4）────────────────
#
# 拆分前那套「单一共用额度」的实现（check_summary_quota /
# consume_summary_quota）已随路由层切换一并删除：全仓库再无调用者，
# 留着只会让人以为还有第二条计费路径。这里是唯一的计费实现。


#: 「无限」的取值。与 ``remaining == -1`` 的既有约定同一个数：
#: 数据层一律用 -1 表示不限量，覆盖列也用同一个数，两边不必再翻译一次。
QUOTA_UNLIMITED = -1


def _resolve_quota_limit(kind: str, override: int | None, vip_active: bool) -> tuple[int, str]:
    """把「覆盖值 + VIP 状态」折成一个 (上限, 来源)。

    **纯函数，不触库。** 它是上限取值的唯一规则实现，
    ``quota_limit`` 与后台用户列表两处都调它——
    两条读出口各自算一遍的话，后台显示的额度就可能和真实判定对不上，
    而管理员恰恰是唯一会去核对那个数字的人。

    优先级：有效 VIP > 覆盖值 > 全局。
    VIP 压过覆盖值不是疏忽，是 ``check_quota_kind`` / ``consume_quota`` /
    ``refund_quota`` 三处都在解析上限**之前**就短路返回 -1 的既有行为
    （工单 #12 把它写成了显式要求：改额度对有效 VIP 必须报「不生效」，
    不能静默成功）。这里如实反映那三处的判定，后台才不会显示一个假数字。
    """
    if vip_active:
        return QUOTA_UNLIMITED, "vip"
    if override is not None:
        return int(override), "override"
    return globals()[quota_kinds.spec(kind).limit_const], "global"


def quota_limit(kind: str, user_id: int | None = None) -> int:
    """取上限。传 user_id 时该用户的覆盖值优先，**只在这一处解析**。

    全局值走 ``globals()[...]`` 而不是 ``from DAILY_PARSE_LIMIT``：
    测试会 monkeypatch 模块属性，导入时冻结会让「改配置后行为随之改变」
    这条 AC 无法验证。

    公开是因为路由层要报同样的数字。import 时冻结的副本会和这里的
    remaining 打架，同一份 payload 报出 `remaining=0, limit=3`（真实上限 1）。

    刻意**不**新写一个 effective_limit()：多一个入口就多一次「A 处用了
    覆盖值、B 处忘了」的可能，而这类分裂的表现恰恰是上面那句自相矛盾的
    payload。覆盖值的读取只在这里发生，别处要数字就带着 user_id 来调。

    传了 user_id 会**多一次查询**（调用方往往已经读过同一个 users 行）。
    这是刻意的：把已取到的行传进来会让函数多一个「行可能不是这个用户的」
    入参，而省下的只是一次本地 SQLite 的点查——用正确性换它不划算。
    """
    if user_id is None:
        return _resolve_quota_limit(kind, None, vip_active=False)[0]
    override_col = quota_kinds.spec(kind).override_col
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {override_col} FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    override = row[override_col] if row else None
    return _resolve_quota_limit(kind, override, vip_active=False)[0]


def check_quota_kind(user_id: int, kind: str) -> tuple[bool, int]:
    """判定单类额度。只读，不写库。返回 (allowed, remaining)，-1 表示无限。"""
    record = quota_kinds.spec(kind)
    count_col, date_col = record.count_col, record.date_col
    limit = quota_limit(kind, user_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return False, 0
        if is_vip_active(user):
            return True, -1
        # 上限为负 = 无限（工单 #12 允许把单人上限设成 -1）。
        # 少了这一支，下面 `current >= limit` 会拿 0 >= -1 判成「已用完」，
        # 于是「设成无限」的用户反而被彻底封死——比不设更糟。
        if limit < 0:
            return True, QUOTA_UNLIMITED
        # 上限为 0 = 一条都不能用（工单 #12 的值域）。
        # 这一支必须排在「今天还没用过 → 额度是满的」那条捷径**之前**：
        # 那条捷径假设 limit >= 1，limit=0 时它会返回 (True, 0)，
        # 于是「一条都不能用」变成了「可以用 0 次」——和封禁没区别，
        # 区别只是用户以为自己被封了。
        if limit == 0:
            return False, 0
        # 日期不是今天，说明今天还没用过，额度是满的
        if user[date_col] != today:
            return True, limit

        current = user[count_col] or 0
        if current >= limit:
            return False, 0
        return True, limit - current


def check_quota(user_id: int) -> dict:
    """一次判定全部额度。键固定为 parse / chat。"""
    return {kind: check_quota_kind(user_id, kind) for kind in quota_kinds.QUOTA_KINDS}


def consume_quota(user_id: int, kind: str) -> int:
    """扣减一次额度，返回扣减后的 remaining。

    **判定与扣减必须在同一条 SQL 里**（工单 #17）。

    原来这里是读-改-写：先 SELECT 拿到 `current`，再无条件 `count + 1`。
    而调用方的 `check_quota_kind` 是**只读**判定，且与本次扣减之间隔着字幕提取
    与整个模型调用（几十秒）。2026-10-06 实测：8 线程同时起跑、上限 3、
    预置已用 2，最终 `daily_parse_count = 5` —— 超限 2 次，
    白送的是平台付费资源（模型调用费）。

    现在改成守卫式：`WHERE ... AND count < limit` 由数据库在写锁内判定，
    靠 rowcount 区分「扣成功」与「额度已满」。这样无论多少并发同时进来，
    成功的次数不会超过 limit —— 判定与扣减之间**没有可被穿插的窗口**。

    **为什么「没扣成」用 `None` 而不是 0**（2026-10-06 实测踩过）：
    remaining 恰好用完时**就是 0**（上限 3，第 3 次扣成功后 `3-2-1=0`）。
    拿 0 表示「没扣成」会把「刚好用完」误判成「额度已满」——
    实测症状是「上限 3 只调了 2 次模型，第 3 次被 quota_exhausted 拒掉，
    而库里计数已经是 3」。两种含义撞车，且**不报错**。
    所以第三态必须与 remaining 的值域不相交。

    返回值语义（**注意 0 不表示「没扣成」**——见下方那段说明）：
      · `QUOTA_UNLIMITED` (-1) = 无限额度用户，本次不计入上限判定
      · `>= 0`               = 扣减后的 remaining（**恰好用完时就是 0**）
      · `None`               = **本次没扣成**（额度已满）。这是新增的第三态。
    """
    record = quota_kinds.spec(kind)
    count_col, date_col = record.count_col, record.date_col
    limit = quota_limit(kind, user_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return None
        if is_vip_active(user):
            return QUOTA_UNLIMITED

        # 上限为负 = 无限。计数照扣（后台要看今日用量），但不参与判定。
        if limit < 0:
            conn.execute(
                f"UPDATE users SET {count_col} = COALESCE({count_col}, 0) + 1, "
                f"{date_col} = ? WHERE id = ?",
                (today, user_id),
            )
            return QUOTA_UNLIMITED

        # 跨天（或今天还没用过）：今天第一次用。
        #
        # **不能用 `SET count = 1`**（2026-10-06 实测）：那是绝对值赋值。
        # 8 个线程同时进来时都判定「日期不是今天」，于是都把计数设成 1——
        # 只有 1 次被记录，其余 7 次的扣减被静默吞掉。症状是
        # 「额度只扣了 1 次，用户却发了 8 个请求」：**丢额度**，
        # 比超限更难发现（用户觉得额度消耗慢，平台白送）。
        #
        # 正确写法：先判「日期不是今天」再原子地置 1，判据与赋值在同一条
        # 语句的 WHERE 里，SQLite 在写锁内求值，只有一个线程能改成 1；
        # 其余线程 WHERE 不成立（date 已经是今天）落到下面的同一天分支。
        if user[date_col] != today:
            if limit == 0:
                return None
            cur = conn.execute(
                f"UPDATE users SET {count_col} = 1, {date_col} = ? "
                f"WHERE id = ? AND {date_col} IS NOT ?",
                (today, user_id, today),
            )
            if cur.rowcount == 1:
                return limit - 1
            # 没改成 → 别人刚把日期拨到今天。**不返回**：继续走同一天分支，
            # 那一支会用守卫式扣减给出正确结果。

        # 同一天：守卫式扣减。`count < limit` 与 `+1` 在同一条语句里，
        # SQLite 在写锁内求值，所以并发线程里只有 limit 次能改到 1。
        cur = conn.execute(
            f"UPDATE users SET {count_col} = COALESCE({count_col}, 0) + 1 "
            f"WHERE id = ? AND COALESCE({count_col}, 0) < ?",
            (user_id, limit),
        )
        if cur.rowcount != 1:
            # 没扣成 = 额度已满。刻意不 raise：调用方已有
            # 「扣减返回 None 就发额度用完事件并中止」的分支，
            # 改成抛异常会让那条路径变成 500。
            return None
        # remaining 从**库里重读**，不用本事务开头那次 SELECT 的 `user`。
        # 走到这里可能是从跨天分支掉下来的（别人刚把日期拨到今天），
        # 那时 `user[count_col]` 还是旧值，拿它算会报出一个错的余额。
        fresh = conn.execute(
            f"SELECT {count_col} FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return limit - (fresh[count_col] or 0)


def refund_quota(user_id: int, kind: str) -> int:
    """把一次扣减还回去，返回回滚后的 remaining。

    用途：模型调用抛异常时不让用户白扣。扣减与调用之间没有事务，
    所以回滚本身是幂等且原子的（单条 UPDATE，不做读-改-写）：
    绝不会把计数压到负数，也不会跨天给今天白送额度，
    更不会把并发的另一次扣减覆盖掉。
    """
    record = quota_kinds.spec(kind)
    count_col, date_col = record.count_col, record.date_col
    limit = quota_limit(kind, user_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return 0
        if is_vip_active(user):
            return -1
        # 跨天了：扣的是昨天的，回滚会让今天的额度凭空多出来，不做。
        if user[date_col] != today:
            return QUOTA_UNLIMITED if limit < 0 else limit

        # 单条 UPDATE 里完成减一。读-改-写两步会和并发的 consume_quota
        # 交叉：中间那次扣减会被本处的绝对值覆盖掉，等于白送额度。
        conn.execute(
            f"UPDATE users SET {count_col} = MAX(COALESCE({count_col}, 0) - 1, 0) "
            f"WHERE id = ?",
            (user_id,),
        )
        row = conn.execute(
            f"SELECT {count_col} FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return QUOTA_UNLIMITED if limit < 0 else limit - (row[count_col] or 0)


# ── 管理后台读出口（工单 #12）─────────────────────────────────
#
# 「读出口按谁在读枚举，不要按数据在哪张表枚举」：下面两个列表各自有一条
# **不依赖任何旧表已有行**的读路径——空库启动时也读得到（只是空数组），
# 管理员账号本身由 users 表给出，不会因为先有列表后有用户就看不见人。
#
# 刻意不塞进 api_community / api_summarize：那两个模块的读出口是**公开**的，
# 后台的读出口是特权读出口，两者混在一处等于给公开端点开一个后门。

#: 后台列表每页条数。**必须有上界**——没有上界的 limit?limit=999999 就是
#: 一次把整张 users 表拉进内存，而这张表随时间单调增长。
ADMIN_PAGE_SIZE_DEFAULT = 20
ADMIN_PAGE_SIZE_MAX = 200

#: offset 的上界。与 _MAX_SAFE_PAGE 同一理由：OFFSET 过大在 SQLite 里
#: 会溢出，且没有任何一个合法的前端会翻到那么后面。
_MAX_SAFE_OFFSET = 1_000_000_000


def _clamp_limit_offset(limit, offset) -> tuple[int, int]:
    """把 limit / offset 收敛到安全范围。**两个方向都要收。**

    收下界：limit <= 0 会让 SQL 变成「取 0 行」，一个手滑的 limit=0
    就会让后台看起来「没有数据」——而它其实有 999 条。归一到默认值，
    宁可多给也不谎报空。
    """
    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError):
        limit, offset = ADMIN_PAGE_SIZE_DEFAULT, 0
    return (
        max(1, min(limit, ADMIN_PAGE_SIZE_MAX)),
        max(0, min(offset, _MAX_SAFE_OFFSET)),
    )


def _quota_used_today(row, kind: str) -> int:
    """该用户**今天**已用掉多少条。跨天的旧计数按 0 报。

    不这么做的话，后台会在第二天早上显示「已用 3 次」而用户实际满额可用——
    管理员会据此去改额度，改的却是一个昨天的事实。
    """
    record = quota_kinds.spec(kind)
    count_col, date_col = record.count_col, record.date_col
    if row[date_col] != datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        return 0
    return row[count_col] or 0


def _admin_user_item(row) -> dict:
    """后台用户行。字段名与前端契约一一对应，不多不少。

    limit 与 source 取自 ``_resolve_quota_limit``——与真实判定同一份规则，
    后台显示的数字和用户实际被卡住的那个数字必然一致。
    """
    item = {
        "id": row["id"],
        "email": row["email"],
        "is_admin": row["is_admin"],
        "is_vip": row["is_vip"],
        "vip_expire_at": row["vip_expire_at"],
        "created_at": row["created_at"],
    }
    vip_active = is_vip_active(row)
    for kind, record in quota_kinds.QUOTA_KINDS.items():
        limit, source = _resolve_quota_limit(kind, row[record.override_col], vip_active)
        item[f"{kind}_used"] = _quota_used_today(row, kind)
        item[f"{kind}_limit"] = limit
        item[f"{kind}_limit_override"] = row[record.override_col]
        item[f"{kind}_limit_source"] = source
    return item


def _like_escape(raw: str) -> str:
    """转义 LIKE 模式里的三个特殊字符。

    不转义的话，管理员搜 `%` 会匹配全表，搜 `_` 会匹配任意单字符——
    这是**功能**上的错，不是注入（参数仍然是绑定变量），但足以让人
    对着「搜什么都全出来」的结果排查半天。
    """
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_admin_users(limit: int = ADMIN_PAGE_SIZE_DEFAULT, offset: int = 0,
                     q: str = "") -> dict:
    """后台用户列表（require_admin 保护）。返回 {items, total, limit, offset}。

    排序用 id 升序而不是 created_at：created_at 有并列（同秒注册），
    并列的行在翻页时会重复出现或凭空消失，而 offset 分页没有「同值保持原序」
    的保证。按主键排则天然稳定。
    """
    limit, offset = _clamp_limit_offset(limit, offset)
    where, params = "", ()
    if q:
        where = "WHERE email LIKE ? ESCAPE '\\'"
        params = (f"%{_like_escape(q.strip())}%",)
    with get_db() as conn:
        total = conn.execute(f"SELECT count(*) FROM users {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM users {where} ORDER BY id ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {
        "items": [_admin_user_item(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def admin_user_detail(user_id: int) -> dict | None:
    """重读一个用户行（用于写操作后回读，确认真的落库）。

    刻意是**重读**而不是回显请求值：回显等于把「我们打算写什么」当成
    「我们写成了什么」返回，写失败时前端会显示一个不存在的额度。
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _admin_user_item(row) if row else None


def set_user_quota_override(user_id: int, overrides: dict) -> int:
    """写入单人额度覆盖，返回受影响的行数（0 = 用户不存在 → 404）。

    ``overrides`` 形如 ``{"parse": 5, "chat": None}``：
    值是 None 表示**清除覆盖**、回落全局。只写传进来的 key，
    没传的 key 一个字都不动。

    列名一律取自 ``quota_kinds.QUOTA_KINDS``，不接受外部传入——拼进 SQL 的必须是
    白名单里的常量，不是请求里的字符串。
    """
    if not overrides:
        return 0
    assignments, params = [], []
    for kind, value in overrides.items():
        # 拼错的 kind 在这里抛，而不是拼出一条 no such column 的 UPDATE。
        override_col = quota_kinds.spec(kind).override_col
        assignments.append(f"{override_col} = ?")
        params.append(value)
    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE users SET {', '.join(assignments)} WHERE id = ?",
            (*params, user_id),
        )
        return cursor.rowcount


class UserConflict(Exception):
    """删除 / 改权限被前置条件挡下。路由层翻成 409。

    刻意**不**继承 ValueError：ValueError 说的是「你传的值不对」，
    而这里说的是「这个人现在动不了」——两者的补救动作完全不同，
    混成一个 400 会让前端把「先处理订单」显示成「参数写错了」。
    """

    def __init__(self, detail: str, blockers: dict | None = None):
        super().__init__(detail)
        self.detail = detail
        #: 表名 → 行数。给前端做「还剩几行要处理」，不给人看。
        self.blockers = blockers or {}


#: 删用户前必须为空的表 → 给管理员看的中文名。
#:
#: orders 与 parse_history 都带 ``REFERENCES users(id)`` 且**没有 ON DELETE**，
#: SQLite 在 foreign_keys=ON 下按 RESTRICT 处理：不先清掉这些行，
#: DELETE 会直接抛 ForeignKeyError —— 500，且错误信息对管理员毫无指导性。
#: 这里先查一次，把数据库约束翻译成一句能照着做的话。
_USER_DELETE_BLOCKERS = {
    "orders": "订单",
    "parse_history": "解析历史",
}


def _user_admin_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM users WHERE is_admin = 1").fetchone()[0]


def set_user_admin(user_id: int, is_admin: bool, *, acting_id: int | None = None) -> int:
    """改管理员标记。返回 1 = 成功，0 = 用户不存在（路由层翻 404）。

    **不**碰 VIP：会员判定留在 ``is_vip_active`` 那一条路径上，
    由订单支付写入。后台改它会同时踩到「新增测试不得锁会员行为」这条纪律。

    两道自锁保护，失效后果都不可逆：
      · 不能撤销**自己**的权限 —— 那这次操作就成了最后一步，
        单管理员部署下再没有人能把它改回来（只能进库改）。
      · 不能把**最后一个**管理员降级 —— 同上，只是隔了一层。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return 0
        if acting_id is not None and user_id == acting_id and not is_admin:
            raise UserConflict("不能撤销自己的管理员权限——那样就没有人能把它改回来了")
        if row["is_admin"] and not is_admin and _user_admin_count(conn) <= 1:
            raise UserConflict("系统里只剩这一个管理员，不能降级")
        conn.execute(
            "UPDATE users SET is_admin = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if is_admin else 0, user_id),
        )
        return 1


def create_admin_user(email: str, password_hash: str, is_admin: bool = False) -> dict:
    """后台建号。

    **不写任何额度覆盖**：新号一律回落全局上限，与公开注册完全一致。
    要单独放宽走额度那条路（``set_user_quota_override``），
    在这里偷偷给一份初值会让「这个号为什么和别人不一样」变成查不到的历史。
    """
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, is_admin) VALUES (?, ?, ?)",
            (email, password_hash, 1 if is_admin else 0),
        )
        return {
            "id": cursor.lastrowid,
            "email": email,
            "is_admin": 1 if is_admin else 0,
        }


def delete_user(user_id: int, *, acting_id: int | None = None) -> None:
    """删掉一个用户。**名下有内容就拒绝**，绝不静默级联（ADR 0012）。

    拒绝而不是级联的理由：订单是支付凭证，解析历史是用户自己的数据。
    级联换来的是「后台一个按钮点下去」，付出的是「删错了没法恢复」——
    两边不对称，所以宁可让人多走一步。冲突时抛 ``UserConflict``。

    ``videos`` **不**在阻断名单里：``parsed_by`` 本来就是弱引用，
    既有设计写明「解析者注销后社区内容必须留下来」（ADR 0010），
    社区列表用 LEFT JOIN 取作者，解析者没了照样读得出。

    ``chat_messages`` 没有外键，但必须跟着删：同一段注释也写了
    「会话记录应随该用户一起消失，而不是变成孤儿行」。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return
        if acting_id is not None and user_id == acting_id:
            raise UserConflict("不能删除自己正在用的账号")
        # 这条在端点路径下**不可达**：actor 与 target 不同时 actor 也是管理员，
        # 于是管理员至少两个，「最后一个」的条件永远不成立；actor == target
        # 时上面已经拦住了。留着是纵深防御——自删守卫哪天放宽，这里兜住。
        if row["is_admin"] and _user_admin_count(conn) <= 1:
            raise UserConflict("系统里只剩这一个管理员，不能删除")

        counts = {
            table: conn.execute(
                f"SELECT count(*) FROM {table} WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            for table in _USER_DELETE_BLOCKERS
        }
        blocking = {t: n for t, n in counts.items() if n}
        if blocking:
            parts = "、".join(
                f"{_USER_DELETE_BLOCKERS[t]} {n} 条" for t, n in blocking.items()
            )
            raise UserConflict(
                f"这个账号名下还有{parts}，先处理掉再删。"
                "后台不替你级联删除——删错了没法恢复。",
                blockers=blocking,
            )

        conn.execute("DELETE FROM chat_messages WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


#: 后台社区列表项的 SELECT 投影。列表与「改完回读」共用同一段 SQL 是刻意的：
#: 两处各写一份的话，字段集早晚会漂移，而前端会用 PATCH 的返回值**直接替换**
#: 列表里那一行——漂移会立刻变成「改完之后这一行比别行少字段」。
_ADMIN_COMMUNITY_ITEM_SQL = (
    "SELECT v.id, v.video_url, v.video_title AS title, v.tags, v.created_at, "
    "       v.status, u.email AS author_email "
    "FROM videos v LEFT JOIN users u ON u.id = v.parsed_by"
)


def _admin_community_item(row) -> dict:
    """后台社区列表项的投影：把 tags 从 JSON 字符串解析成 list。

    单独抽出来是因为改标签的端点要回读**同一形状**（update_video_tags
    末尾那次回读）。解析容错统一交给 _decode_tags_text（工单 #32 收口）：
    而不是把原始字符串透出去——前端拿到字符串会直接渲染成 `["编程"]` 那样
    一串带引号的怪东西。
    """
    item = dict(row)
    item["tags"] = _decode_tags_text(item.get("tags"))
    return item


def list_admin_community(limit: int = ADMIN_PAGE_SIZE_DEFAULT, offset: int = 0) -> dict:
    """后台社区记录列表（require_admin 保护）。

    **不过滤 status**：pending 行是占位，后台要看的是「谁占了位没解析完」，
    这类记录恰恰只在 pending 状态里存在。这条仍然成立。

    这段注释的第三句原本是「项目范围边界也明确不做下架，后台因此没有、也不该有
    「只看得见已就绪」这层过滤」——**这一句已经不成立了**：后台现在有改标签与
    删除两个写出口。保留「不过滤」不是为了给下架让路，而是因为把占位行过滤掉
    会让「谁占了位没解析完」这个问题**永远查不出来**：占位行本身就是答案。
    删除端点的语义（只删 videos 一行）在 delete_video_record 里说全了，
    与这里读不读得到是两件事。

    列表项**带上 status**（ready / pending）：前端要按状态区别渲染——一个是社区
    内容，一个是占位。只给一个「都看得见的列表」却不告知状态，前端就只能把占位
    行当内容画出来，而 pending 行里根本没有总结与字幕。

    LEFT JOIN users 取作者邮箱：parsed_by 没有外键（ADR 0010——解析者注销后
    社区内容必须留下来），所以作者可能已经不在，用 LEFT 而不是 INNER，
    否则那些行会凭空消失，而它们恰恰是最该被看见的。
    """
    limit, offset = _clamp_limit_offset(limit, offset)
    with get_db() as conn:
        total = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
        rows = conn.execute(
            _ADMIN_COMMUNITY_ITEM_SQL + " ORDER BY v.id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return {
        "items": [_admin_community_item(row) for row in rows],
        "total": total, "limit": limit, "offset": offset,
    }


def update_video_tags(video_id: int, tags: list[str]) -> dict | None:
    """改一条社区视频的标签，**回读**改动后的列表项；没有这一行就返回 None。

    返回回读而不是 rowcount：前端拿返回值去替换列表里那一行，形状必须与
    列表项**逐字段一致**，而这个一致性靠「共用 _ADMIN_COMMUNITY_ITEM_SQL 与
    _admin_community_item」保证，不是靠两处抄得一样仔细。回读还在同一个事务里
    做，所以并发删掉这一行时拿到的是 None（404），而不是一条刚被删掉的行的残影。

    **只碰 tags 与 updated_at**：status / summary_md / mindmap_md /
    subtitle_text 一个都不写。改分类不该顺手改内容；把它们塞进同一条 UPDATE
    的代价是「以后加一列就默认能被后台改」，而那正好是在把后台变成第二条
    内容写入路径。updated_at 要跟着动：它就是这一行「最后一次被维护」的时刻，
    不动的话后台分不清「三个月前解析的」与「今天刚被改过标签的」。

    **不带 status 条件**：ready 与 pending 都允许改标签。pending 行的标签此刻
    还没写回（模型还没跑完），但后台正是要能给占位行标上人工指定的分类。
    加 ``AND status = 'ready'`` 会让它对占位行静默 404，而 404 在契约里的
    含义是「这一行不存在」——那是在说谎。

    值域（词表 / 非空 / 不超过 MAX_TAGS）由调用方在进到这里之前判掉：
    那是**请求**的合法性，不是存储的约束，判据的形态（400 + detail）属于
    端点。这里只负责落库，因此不 import tags——省掉一条数据层到词表模块的边。
    """
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(list(tags), ensure_ascii=False)
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE videos SET tags = ?, updated_at = ? WHERE id = ?",
            (payload, now, video_id),
        )
        if cursor.rowcount == 0:
            # 0 只有一种含义：没有这一行。不静默成功——那会让管理员以为
            # 改到了某个其实不存在的地方。
            return None
        row = conn.execute(
            _ADMIN_COMMUNITY_ITEM_SQL + " WHERE v.id = ?", (video_id,)
        ).fetchone()
    return _admin_community_item(row) if row else None


def delete_video_record(video_id: int) -> int:
    """删掉一条社区视频记录，返回删掉的行数（0 = 没有这一行）。

    **只删 videos 一张表。** 这是本设计成立的前提，而不是实现时的克制：
    建表语句里全库只有两处外键，都是 ``REFERENCES users(id)``（orders 与
    parse_history），**没有任何外键指向 videos**；parse_history 与
    chat_messages 都不引用 videos——它们只按 user_id / video_url 记自己的事，
    与「社区里那一行还在不在」无关。schema 里根本没有这条边，所以删这一行在
    数据上就波及不到任何用户记录；**要是哪天给 videos 加上被引用的外键，
    这条论证连同下面那段一起作废**。

    parse_history 保留是用户明确要的语义：视频从社区消失，解析过它的用户在
    自己的历史里仍看得到自己那条记录。级联删掉它等于替用户决定「你解析过
    的东西不许留」——而解析历史是**用户自己的数据**，不是社区内容的附属品。
    （对照 delete_user：那条是「有名下内容就 409，绝不级联」，方向一致。）

    **允许删 pending 占位行，不加 status 守卫。** 代价先说清：一次正在进行的
    解析会因此在 complete_video 处拿到 0，而那个函数的 docstring 明写「由
    调用方报警而不是静默当作成功」——所以后果是**响亮地失败**，不是数据悄悄
    写丢。仍然允许删的理由是**后台没有别的清理出口**：

    - 占位行不带任何「我还活着」的凭据。updated_at 只在被写时才会动，
      解析过程本身不会续租，所以「进程正在跑」与「半小时前崩了」在表里
      是**同一种形状**。
    - 唯一近似的判据是 :data:`VIDEO_PENDING_TTL_SECONDS`（默认 30 分钟），
      而它只活在 reserve_video 的接管分支里：同一个链接被**再次解析**时
      才会顺带回收陈旧占位。后台这个页面拿不到它，也用不上它。
    - 于是禁止删 pending 的实际后果是：崩掉的占位行除非有人恰好重新解析
      那个链接，否则后台永远清不掉它——把一种「偶发但响亮的失败」换成
      一种「静默且永久的死条目」。这是更糟的死路。

    选「响亮地失败」而不是「制造死条目」。complete_video 的调用方已经处理
    0 的分支，不在本次改动范围内。
    """
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        return cursor.rowcount


# ── 订单操作 ──────────────────────────────────────────────

def create_order(user_id: int, order_no: str, amount: int, currency: str = "cny", plan_type: str = "monthly"):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO orders (order_no, user_id, amount, currency, plan_type)
               VALUES (?, ?, ?, ?, ?)""",
            (order_no, user_id, amount, currency, plan_type),
        )


def update_order_stripe_session(order_no: str, session_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE orders SET stripe_session_id = ? WHERE order_no = ?",
            (session_id, order_no),
        )


def complete_order(session_id: str, payment_intent_id: str) -> dict | None:
    """支付完成时更新订单状态、激活 VIP。幂等性：只有 pending 状态的订单才会被处理。"""
    from dateutil.relativedelta import relativedelta

    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE stripe_session_id = ? AND status = 'pending'",
            (session_id,),
        ).fetchone()

        if not order:
            return None

        now = datetime.now(timezone.utc).isoformat()
        user = conn.execute(
            "SELECT * FROM users WHERE id = ?", (order["user_id"],)
        ).fetchone()

        current_expire = None
        if user["vip_expire_at"]:
            try:
                current_expire = datetime.fromisoformat(user["vip_expire_at"])
                # 兜底 naive datetime（与 is_vip_active 同源修复）
                if current_expire.tzinfo is None:
                    current_expire = current_expire.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        base_time = datetime.now(timezone.utc)
        if current_expire and current_expire > base_time:
            base_time = current_expire

        if order["plan_type"] == "monthly":
            new_expire = base_time + relativedelta(months=1)
        elif order["plan_type"] == "yearly":
            new_expire = base_time + relativedelta(years=1)
        else:
            new_expire = base_time + relativedelta(months=1)

        conn.execute(
            "UPDATE orders SET status='paid', stripe_payment_intent_id=?, paid_at=?, updated_at=? WHERE id=?",
            (payment_intent_id, now, now, order["id"]),
        )
        conn.execute(
            "UPDATE users SET is_vip=1, vip_expire_at=?, updated_at=? WHERE id=?",
            (new_expire.isoformat(), now, order["user_id"]),
        )

        return dict(order)


def get_user_orders(user_id: int) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── 解析历史记录 ──────────────────────────────────────────

def _trim_parse_history(conn, user_id: int):
    """每用户只保留最近 MAX_PARSE_HISTORY_PER_USER 条**未收藏**记录。

    **收藏不参与裁剪。** 这不是「多给它一点空间」：上限不管多大，
    越线那一刻被删的一定是最旧的那条，而那恰好可能是用户攒下来、
    特意标了星的那条。界面上没有任何一处提示过「收藏也会被裁掉」，
    所以那是一次静默的数据丢失 —— 收藏这个功能存在的全部意义就是
    挡掉它。

    因此收藏条数**不受上限约束**：1000 条未收藏 + 任意条收藏都成立。
    """
    conn.execute(
        f"""DELETE FROM parse_history
            WHERE user_id = ? AND is_favorite = 0 AND id NOT IN (
                SELECT id FROM parse_history
                WHERE user_id = ? AND is_favorite = 0
                ORDER BY COALESCE(updated_at, created_at) DESC
                LIMIT {int(MAX_PARSE_HISTORY_PER_USER)})""",
        (user_id, user_id),
    )


def upsert_parse_history(
    user_id: int,
    video_url: str,
    video_title: str = "",
    video_data: dict | None = None,
    summary_md: str = "",
    mindmap_md: str = "",
    subtitle_data: dict | None = None,
) -> int:
    """按 (user_id, video_url) 去重保存解析历史，返回记录 ID。

    合并语义：新值为空时保留旧值（视频源保存不覆盖 AI 结果，反之亦然），
    非空新值覆盖旧值。查询走 idx_history_user_url 复合索引。

    去重由**唯一索引**兜底，不靠「先查再写」：原来那是读-改-写，
    两个并发请求各自查到「还没有这行」就各插一行（实测 10 线程 → 2 行）。
    现在一条 INSERT ... ON CONFLICT DO UPDATE 走完两条路，
    冲突的那条直接就地合并，docstring 的承诺才在并发下成立。
    """
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        # RETURNING 而不是 cursor.lastrowid：走 DO UPDATE 那条路时
        # lastrowid 是未定义的（实测会给出别的行），而调用方要的是
        # 「这一条」的 id —— 冲突路径上它必须仍然是**既有**那一行。
        row = conn.execute(
            """INSERT INTO parse_history
               (user_id, video_url, canonical_url, video_title, video_data,
                summary_md, mindmap_md, subtitle_data, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, video_url) DO UPDATE SET
                   video_title   = COALESCE(NULLIF(excluded.video_title, ''),
                                            parse_history.video_title),
                   video_data    = COALESCE(NULLIF(excluded.video_data, ''),
                                            parse_history.video_data),
                   summary_md    = COALESCE(NULLIF(excluded.summary_md, ''),
                                            parse_history.summary_md),
                   mindmap_md    = COALESCE(NULLIF(excluded.mindmap_md, ''),
                                            parse_history.mindmap_md),
                   subtitle_data = COALESCE(NULLIF(excluded.subtitle_data, ''),
                                            parse_history.subtitle_data),
                   updated_at    = excluded.updated_at
               RETURNING id""",
            (user_id, video_url, canonical_video_url(video_url), video_title,
             json.dumps(video_data, ensure_ascii=False) if video_data else "",
             summary_md, mindmap_md,
             json.dumps(subtitle_data, ensure_ascii=False) if subtitle_data else "",
             now, now),
        ).fetchone()
        _trim_parse_history(conn, user_id)
        return row["id"]


# ── 追问会话（工单 #8）────────────────────────────────────
#
# 真相源唯一：只有服务端在答案**完整产出后**才调 append_chat_turn。
# 前端不再回调任何保存接口——记录存不存在取决于服务端有没有真答出这句话，
# 而不是取决于浏览器有没有多发一个请求（改一行 JS、或写库前断流，
# 记录都会不见；反过来伪造一条 {question, answer} 也只是一行请求的事）。
#
# 隔离在查询条件里：每个读函数都以 user_id 打头，别人的记录读不出来。


def append_chat_turn(user_id: int, video_url: str, question: str, answer: str) -> None:
    """把一整轮追问（一问 + 一答）写进会话表。

    两行在**同一个事务**里写：只有问没有答的半轮记录配不出 answer，
    分两次写等于在两次写之间留一个窗口，而那个窗口里的会话读出来是残的。
    """
    with get_db() as conn:
        for role, content in (("user", question), ("assistant", answer)):
            conn.execute(
                """INSERT INTO chat_messages
                   (user_id, video_url, canonical_url, role, content)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, video_url, canonical_video_url(video_url), role, content),
            )


def get_recent_chat_messages(user_id: int, video_url: str, turns: int | None = 3) -> list:
    """某个用户与某个视频最近的追问明细，按 id **升序**返回。

    `turns` 轮 = 2 * turns 条（写入永远成对）；`turns=None` 取全部，
    聚合整段会话时用。升序是拼 prompt 的要求：时间序给模型才读得通。
    取「最近的一段」却在 SQL 里正序查会拿到最早的，所以先倒序取再翻回来。
    """
    sql = ("SELECT role, content FROM chat_messages"
           " WHERE user_id = ? AND canonical_url = ? ORDER BY id DESC")
    params = [user_id, canonical_video_url(video_url)]
    if turns is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(turns)) * 2)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _pair_up_chat_messages(messages: list) -> list:
    """把消息明细配成 [{question, answer}]。

    相邻的 user / assistant 两条配成一轮。末尾若剩一个孤立的 user 行
    （有问无答），**丢弃**：半轮对话展示成一条空回答比不展示更糟。
    """
    turns: list = []
    pending: str | None = None
    for msg in messages:
        if msg["role"] == "user":
            # 新的问题开始。上一轮若没配上答案（脏数据），就此丢弃，
            # 不让它顺延到这一轮上把两问配成一次回答。
            pending = msg["content"]
        elif msg["role"] == "assistant" and pending is not None:
            turns.append({"question": pending, "answer": msg["content"]})
            pending = None
    return turns


def get_chat_session(user_id: int, video_url: str) -> list:
    """某个用户与某个视频的完整会话，形状恒为 [{question, answer}]。

    **所有读点都必须走这里**，别再自己拼一份：新表为空时回退读旧列这条
    规则散在两处就一定会漂移（详情接口给老记录、新端点给空）。
    """
    turns = _pair_up_chat_messages(get_recent_chat_messages(user_id, video_url, turns=None))
    if turns:
        return turns
    # 下面这条查询**同样**按 user_id 过滤：旧列挂在 parse_history 行上，
    # 漏掉它就是别人的追问记录被读出来。
    with get_db() as conn:
        row = conn.execute(
            "SELECT chat_history FROM parse_history "
            "WHERE user_id = ? AND canonical_url = ?",
            (user_id, canonical_video_url(video_url)),
        ).fetchone()
    return _legacy_chat_history(row["chat_history"]) if row else []


def _legacy_chat_history(raw: str | None) -> list:
    """读老库里的 parse_history.chat_history（工单 #8 之前唯一的存放处）。

    **为什么还留着这条路**：老库里已有的记录还躺在那一列，尚未迁移；
    新表为空时回退读它，那些历史才不至于凭空消失。旧列从此只读不写。

    ⚠️ **本函数与 `_decode_tags_text` 的口径刻意不同，不要统一**（工单 #35）。
    它解的是**对象数组** `[{"question": ..., "answer": ...}]`，不是字符串数组：

    - `_decode_tags_text` 过滤非字符串元素（`isinstance(t, str)`），
      因为它解的 tags 列存的就是字符串数组；
    - 本函数**原样返回**数组里的每一个元素 —— 因为老列存的是 dict，
      而 `isinstance(t, str)` 对 dict **恒为 False**。

    所以把这里的口径「统一」成过滤版，**正常的老数据会被整个清空**，
    症状是老用户的追问记录凭空消失。实测（临时库，三种输入逐格对照）：

        输入 [{"question","answer"}]   现状 [{'question','answer'}]  过滤版 []
        输入 ["q","a"]                  现状 ['q','a']              过滤版 []

    「数组里混进非字符串」在**结构上不可达**：这一列唯一的真实写入点是
    `append_chat_history`（工单 #8 之前），它只 `append({"question":...})`，
    没有任何路径能塞进字符串或数字。要来的数据只可能是手改库或迁移脚本 ——
    那属于**数据事故**，该在写入侧报警，不该靠读的时候静默丢弃。
    """
    try:
        chats = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return chats if isinstance(chats, list) else []


def get_parse_histories(user_id: int, limit: int = MAX_PARSE_HISTORY_PER_USER) -> list:
    """历史记录列表（不含大字段，含摘要预览）"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, video_url, video_title, summary_md,
                      COALESCE(updated_at, created_at) AS updated_at,
                      created_at,
                      -- 封面地址供历史列表渲染缩略图。video_data 本身是
                      -- 大字段，列表页不需要，所以只把里面那一项用
                      -- json_extract 挑出来，不整块拉出来再丢。
                      --
                      -- json_valid 是**必需**的守卫，不是保险：实测（.scratch/
                      -- probe_json_extract.py）一行非法 JSON 就会让整条查询
                      -- 抛 "malformed JSON"，也就是**一条坏记录足以让整个
                      -- 历史列表 500**。取不到就返回空串，前端回落到占位图标。
                      CASE WHEN json_valid(video_data)
                           THEN COALESCE(json_extract(video_data, '$.thumbnail'), '')
                           ELSE '' END AS cover_url,
                      -- 追问记录在 chat_messages（工单 #8）。
                      -- 后面 OR 的是老库那一列：新表为空时它仍然是唯一有记录的地方，
                      -- 漏掉它会让「详情里读得到记录、列表里却没有」自相矛盾。
                      (EXISTS (SELECT 1 FROM chat_messages m
                               WHERE m.user_id = parse_history.user_id
                                 AND m.canonical_url = parse_history.canonical_url)
                       OR (chat_history IS NOT NULL AND chat_history != '[]')) AS has_chat
               FROM parse_history WHERE user_id = ?
               ORDER BY COALESCE(updated_at, created_at) DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            has_chat = bool(item.get("has_chat"))
            summary_md = item.pop("summary_md") or ""
            item["summary_preview"] = summary_md.strip()[:120]
            item["has_chat"] = has_chat
            item["has_ai_result"] = has_chat or bool(item["summary_preview"])
            # cover_url 由上面的 json_valid 守卫保证是字符串；
            # 这里只做一次类型归一，前端不必再判 null。
            if not isinstance(item.get("cover_url"), str):
                item["cover_url"] = ""
            items.append(item)
        return items


def get_parse_history_detail(user_id: int, history_id: int) -> dict | None:
    """单条历史记录完整内容（JSON 字段已反序列化）"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM parse_history WHERE user_id = ? AND id = ?",
            (user_id, history_id),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        for field in ("video_data", "subtitle_data"):
            try:
                item[field] = json.loads(item[field]) if item[field] else None
            except (ValueError, TypeError):
                item[field] = None

    # 读在 with 块**外面**做，与那个事务彻底分开。会话读法只有一个入口，
    # 老列回退也归它管——这里不再另写一份。
    item["chat_history"] = get_chat_session(user_id, item["video_url"])
    return item


#: 历史列表的「有 AI 结果」判定。**别名写死为 h** —— 一旦套上别名就
#: 必须用别名指列，裸表名在 SQLite 里会报 no such column。
#:
#: 与 get_parse_histories 的 has_ai_result 同一口径：有问答记录，
#: 或 summary_md 非空。字幕/思维导图单独存在不算「AI 解析过」——
#: 那两个都不花模型调用，界面上那个 AI 徽标指的是总结。
_HISTORY_HAS_AI_ALIASED_H = (
    "(EXISTS (SELECT 1 FROM chat_messages m"
    "  WHERE m.user_id = h.user_id AND m.canonical_url = h.canonical_url)"
    " OR (h.chat_history IS NOT NULL AND h.chat_history != '[]')"
    " OR COALESCE(TRIM(h.summary_md), '') != '')"
)


def list_parse_histories(user_id: int, q: str = "", tag: str = "",
                         favorite: bool = False, ai: str = "",
                         page: int = 1,
                         page_size: int = HISTORY_PAGE_SIZE_DEFAULT) -> dict:
    """历史列表：分页 + 关键词 / 标签 / 仅收藏 / AI 状态，可任意组合。

    q 走 LIKE 而不是 FTS5，与社区刻意不同：社区是全站共享表、量级不封顶，
    值得养一个 FTS 虚拟表；历史是**按 user_id 隔离的个人列表**，
    上限 1000 条（且 _trim_parse_history 兜着），一条 B 树索引上的 LIKE
    扫 1000 行是微秒级。为一个人最多 1000 行的表建 FTS + 触发器，
    换来的是「一条坏记录就能让整张表 500」那类新风险。

    链接定位（q 以 http 开头）走**等值**而不是子串：粘贴链接时用户要的
    是「就是这一条」，而 LIKE 会同时命中被当成子串出现的别的记录。
    """
    q = (q or "").strip()
    where = ["h.user_id = ?"]
    params: list = [user_id]

    if q.startswith(("http://", "https://")):
        where.append("h.canonical_url = ?")
        params.append(canonical_video_url(q))
    elif q:
        where.append("(COALESCE(h.video_title, '') LIKE ? OR h.video_url LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])

    tag_clause, tag_params = _tag_clause(tag)
    if tag_clause:
        # 共享子句自带前导 " AND "（社区那两处是直接拼进 where 串的），
        # 而这里把条件收集起来再统一 join —— 不去掉就会拼出
        # "... AND  AND EXISTS ..."，SQLite 直接 syntax error near "AND"。
        where.append(tag_clause.lstrip().removeprefix("AND "))
        params.extend(tag_params)

    if favorite:
        where.append("h.is_favorite = 1")

    if ai == "ai":
        where.append(_HISTORY_HAS_AI_ALIASED_H)
    elif ai == "parse":
        where.append(f"NOT {_HISTORY_HAS_AI_ALIASED_H}")

    page, page_size = _clamp_page(page, page_size)
    clause = " AND ".join(where)
    sql_from = "parse_history h LEFT JOIN videos v ON v.canonical_url = h.canonical_url"

    with get_db() as conn:
        total = conn.execute(
            f"SELECT count(*) FROM {sql_from} WHERE {clause}", tuple(params)
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT h.id, h.video_url, h.video_title, h.summary_md,
                       COALESCE(h.updated_at, h.created_at) AS updated_at,
                       h.created_at, h.is_favorite,
                       CASE WHEN json_valid(h.video_data)
                            THEN COALESCE(json_extract(h.video_data, '$.thumbnail'), '')
                            ELSE '' END AS cover_url,
                       {_HISTORY_HAS_AI_ALIASED_H} AS has_ai_result,
                       COALESCE(v.tags, '[]') AS tags
                FROM {sql_from} WHERE {clause}
                ORDER BY COALESCE(h.updated_at, h.created_at) DESC, h.id DESC
                LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()

    items = []
    for r in rows:
        item = dict(r)
        summary_md = item.pop("summary_md") or ""
        item["summary_preview"] = summary_md.strip()[:120]
        item["has_ai_result"] = bool(item["has_ai_result"])
        item["is_favorite"] = bool(item["is_favorite"])
        if not isinstance(item.get("cover_url"), str):
            item["cover_url"] = ""
        item["tags"] = _decode_tags_text(item.get("tags"))
        items.append(item)

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size,
        # 三档计数要能对上：仅解析 + AI解析 == 全部（忽略搜索/标签/收藏过滤）。
        # 不给这两个数的话，前端那三个档位的数字只能靠总数减出来，
        # 而减法在有搜索条件时会骗人。
        "has_ai": sum(1 for i in items if i["has_ai_result"]),
        "favorites": sum(1 for i in items if i["is_favorite"]),
    }


def list_parse_history_facets(user_id: int) -> list:
    """历史页标签筛选的选项（带计数），来自该用户的**全部**历史。

    刻意不受当前筛选条件影响，也不按当前页汇总：
    前者会让「选了标签 A 之后标签 B 消失」，后者会让「翻页之后标签增减」。
    两种表现用户读起来都是同一个意思 —— 「这个筛选不生效」。
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT je.value AS tag, count(*) AS n
               FROM parse_history h
               LEFT JOIN videos v ON v.canonical_url = h.canonical_url
               JOIN json_each(
                   CASE WHEN json_valid(COALESCE(v.tags, '[]'))
                        THEN v.tags ELSE '[]' END) je
               WHERE h.user_id = ?
               GROUP BY je.value
               ORDER BY n DESC, je.value""",
            (user_id,),
        ).fetchall()
    return [{"tag": r["tag"], "count": r["n"]} for r in rows]


def set_parse_history_favorite(user_id: int, history_id: int,
                               is_favorite: bool) -> bool:
    """收藏 / 取消收藏。记录不存在时返回 False。

    **不动 updated_at。** 列表按 updated_at 倒序；一旦收藏也顺带刷新
    时间戳，用户点一下星标，这条记录就会从列表中间跳到最顶上 ——
    在他手指底下重排，看起来像页面出 bug。
    """
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE parse_history SET is_favorite = ? WHERE user_id = ? AND id = ?",
            (1 if is_favorite else 0, user_id, history_id),
        )
        return cur.rowcount > 0


def get_parse_history_favorite(user_id: int, history_id: int):
    """收藏状态：1 收藏 / 0 未收藏 / None 记录不存在。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT is_favorite FROM parse_history WHERE user_id = ? AND id = ?",
            (user_id, history_id),
        ).fetchone()
    if row is None:
        return None
    return 1 if row["is_favorite"] else 0


def clear_parse_history(user_id: int, keep_favorites: bool = True) -> int:
    """清空历史，返回删掉的条数。

    默认**跳过收藏**：一键清空是典型的误操作，而收藏是用户唯一一处
    「这条我特意留着的」标记。要连收藏一起删，调用方得显式要求。
    """
    with get_db() as conn:
        if keep_favorites:
            cur = conn.execute(
                "DELETE FROM parse_history WHERE user_id = ? AND is_favorite = 0",
                (user_id,),
            )
        else:
            cur = conn.execute(
                "DELETE FROM parse_history WHERE user_id = ?", (user_id,)
            )
        return cur.rowcount


def delete_parse_history(user_id: int, history_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM parse_history WHERE user_id = ? AND id = ?",
            (user_id, history_id),
        )
        return cursor.rowcount > 0


# ── 社区视频（工单 #6 / ADR 0001）────────────────────────
#
# 这一族函数就是「同一个链接全站只解析一次」的机制本体：
#
#   reserve_video    抢占解析权（唯一索引在这里真正起作用）
#   complete_video   占位者把结果写回自己占的那一行
#   release_video    占位者失败时把位置还回去
#   get_video_by_url 读社区里已有的那一份结果
#
# 为什么必须是「先占位」而不是「先查有没有、没有就插入」：
# 查与插之间有一个窗口，两个用户同时进来会双双查到「还没有」，
# 于是双双去调模型、双双扣额度——而此时两人都还什么结果都没拿到。
# 唯一索引只能把第二次**插入**挡下来，挡不住第二次**调模型**。
# 所以本设计把「调模型」这个动作挂在 reserve 成功这个条件上：
# 抢到占位的人去调模型，没抢到的人转去等待或复用。
# 这三件事（一次模型调用、一次额度扣减、一行数据）因此同时成立。
#
# 唯一的例外是「占位者自己失败」：它不能永远占着那个位置，
# 否则这个链接从此再也没人能解析——所以 finally 里必须 release_video。

#: 占位行的状态。pending = 已有人占位、正在解析（结果还没产出）；
#: ready = 社区里已有结果，谁来读都是同一份，谁都不能改。
VIDEO_STATUS_PENDING = "pending"
VIDEO_STATUS_READY = "ready"

#: 陈旧占位的回收阈值（秒）。占位者只在 `finally` 里还位——进程被杀、
#: 机器断电、模型无限挂起时它跑不到那里，那一行就永远停在 pending，
#: **这个链接从此再也没人能解析**。所以这里给占位加一个上限。
#:
#: ⚠️ 这个值必须**大于一次正常解析的最长耗时**（字幕提取 + 一次模型调用）。
#: 调小它会在占位者还在好好干活时把位置偷走，于是两个人同时调模型、
#: 同时扣额度——正好是本设计要防的那件事。它是「可恢复性」与
#: 「不重复扣费」之间的取舍，取后者优先。
VIDEO_PENDING_TTL_SECONDS = _env_int("VIDDIGEST_VIDEO_PENDING_TTL_SECONDS", 1800)


def _pending_is_stale(row: dict) -> bool:
    """占位行是否已经老到可以安全接管。解析不了时间就当它还活着。"""
    raw = row.get("updated_at")
    if not raw:
        return False
    try:
        updated = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - updated).total_seconds()
    return age > VIDEO_PENDING_TTL_SECONDS


#: 覆盖闸门的过期阈值（秒）。语义与 :data:`VIDEO_PENDING_TTL_SECONDS` 同：
#: 持闸者只在 finally 里还闸——进程被杀、机器断电、模型挂死时它跑不到那里，
#: 那一行就永远锁着，**这个链接从此没人能覆盖**。
#:
#: ⚠️ 必须大于一次正常覆盖的最长耗时（字幕提取 + 一次模型调用）。调小它会在
#: 持闸者还在好好干活时把锁偷走，于是两个人同时调模型、同时扣额度——
#: 正好是这个闸门要防的那件事。取值与 pending 同量级，但它是独立的环境变量：
#: 覆盖与首次解析的耗时分布不同，不该共用一个旋钮。
VIDEO_REGENERATE_TTL_SECONDS = _env_int("VIDDIGEST_VIDEO_REGENERATE_TTL_SECONDS", 1800)


def acquire_regenerate_gate(video_url: str, user_id: int) -> bool:
    """抢一次覆盖权。抢到返回 True，被别人占着或不归他返回 False。

    **跨进程安全的关键在「一条语句」**：抢锁是单条带条件的 UPDATE，而 SQLite
    串行化写事务，于是两个进程同时抢时只有一个拿到 `rowcount == 1`。这与
    :func:`reserve_video` 依赖唯一索引裁决首次解析是同一套机制——不是新发明，
    是把已经在这套代码里成立的那件事用到第二条路径上。

    过期判定刻意写在 SQL 的 WHERE 里而不是「先读后判」：先读后判会在两个
    进程之间留一个窗口，而那个窗口正是工单 #20 要消灭的那类静默失效
    （两个进程都判定「没人占着」，都进去，两次调模型、两次扣额度）。

    同样刻意**不**改 status：见 :func:`_migrate_regenerate_columns`。
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=VIDEO_REGENERATE_TTL_SECONDS)).isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE videos
                  SET regenerating_by = ?, regenerating_at = ?
                WHERE canonical_url = ?
                  AND status = ?
                  AND parsed_by = ?
                  AND (regenerating_by IS NULL
                       OR regenerating_at IS NULL
                       OR regenerating_at <= ?)""",
            (user_id, now.isoformat(), canonical_video_url(video_url),
             VIDEO_STATUS_READY, user_id, cutoff),
        )
        return cursor.rowcount == 1


def release_regenerate_gate(video_url: str, user_id: int) -> None:
    """放掉覆盖闸门。必须与 :func:`acquire_regenerate_gate` 成对，且放在 finally 里。

    WHERE 里带上 `regenerating_by = ?`：一个过期后迟到醒来的持闸者放闸时，
    不能把**别人**的锁清掉——那会让第三个人以为没人占着而同时进来。
    """
    with get_db() as conn:
        conn.execute(
            """UPDATE videos SET regenerating_by = NULL, regenerating_at = NULL
                WHERE canonical_url = ? AND regenerating_by = ?""",
            (canonical_video_url(video_url), user_id),
        )


def probe_video(video_url: str) -> str:
    """**只读**地问一句：这个链接现在归谁。不写库，不开写事务。

    供等待者轮询用（工单 #19 第 1 项）。返回三种之一：

    - ``"ready"``     社区里已有结果，调用方该去复用
    - ``"claimable"`` 没人占位，或占位已老到可以接管——**该调用方去抢**
    - ``"waiting"``   别人正占着且占位还新鲜——继续等

    为什么需要它：:func:`reserve_video` 每次调用都是一个**写事务**——
    它先 INSERT 让唯一索引来裁决，被拒绝了才另开事务重读。而等待者
    每 50ms 重抢一次，实测每个等待者每秒产生 17 次写事务（30 秒内 510 次，
    5 个等待者 2550 次）。这些事务抢的是 WAL 写锁，**争锁的代价由那个
    真正有活要干的占位者承担**——它在几十秒后要 complete_video 写回结果，
    正好撞上等待者最密的轮询。

    所以「每轮重新抢」这个刻意设计（占位者中途失败还位时，等待者要能
    在下一轮成为首次解析者）保留不动，只把**裁决**与**写入**拆开：
    轮询走这条只读路径判断该不该抢，只有真的要抢时才调 reserve_video。

    ⚠️ 读到「没人占位」不等于抢得到：那一刻到 INSERT 之间仍有竞态。
    那个竞态由 :func:`reserve_video` 里的唯一索引兜住，本函数不负责。
    **它只回答「值不值得花一次写事务」**，不回答「你抢到了没有」。
    """
    row = get_video_by_url(video_url)
    if row is None:
        # 没人占位：该抢
        return "claimable"
    if row["status"] == VIDEO_STATUS_READY:
        return "ready"
    # 别人占着：只有老到 TTL 才该接管，否则继续等
    return "claimable" if _pending_is_stale(row) else "waiting"


def reserve_video(
    video_url: str,
    user_id: int | None,
    video_title: str = "",
    cover_url: str = "",
) -> tuple[str, dict | None]:
    """抢占一个链接的解析权。返回 (outcome, row)。

    outcome 只有三种，调用方必须分别处理——尤其是 "reserved"：
    **只有拿到它的人才允许去调模型**，这是「只调一次模型」的唯一入口。

    - "reserved"  占位成功，调用者是首次解析者
    - "ready"     社区里已有结果，row 就是那份结果，直接复用
    - "pending"   别人正在解析，调用方需要等它出结果

    写与读刻意分在两个事务里：插入被唯一索引拒绝之后，必须在一个
    **新的**事务里重读，否则读到的还是那个已被回滚的事务的快照。

    `video_title` / `cover_url` 是**占位时**就带进去的（工单 #17 第 2 项）。

    为什么不是等 ready 之后再回填：那条路（`publish_video_card`）
    要求 `status = 'ready'`，而占位行在解析完成前一直是 pending。
    前端 `publishCard` 紧跟 `/api/parse` 发出，那一刻 `videos` 行
    **还不存在**（它要等用户点「AI 总结」才被本函数创建），
    于是那次 UPDATE 永远匹配 0 行 —— 首次解析者填的标题/封面填不进去，
    卡片实际由**第二个访问者**填。连带后果：`videos_fts` 索引
    `video_title`，而绝大多数行该字段是空串 → 社区按标题搜索基本失效。

    在占位时一并写入仍然遵守「只填空，不覆盖」：这里写的是
    **自己刚建的行**，而 #7 的承诺是「ready 行谁都不能改写」，
    pending 行还没到那个阶段。
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO videos
                   (video_url, canonical_url, status, parsed_by, created_at,
                    updated_at, video_title, cover_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (video_url, canonical_video_url(video_url),
                 VIDEO_STATUS_PENDING, user_id, now, now,
                 video_title or "", cover_url or ""),
            )
        return "reserved", None
    except sqlite3.IntegrityError:
        # 唯一索引挡住了重复行：这个链接已有人在解析或已解析完成。
        row = get_video_by_url(video_url)
        if row is None:
            # 占位者刚失败并把位置还了回去。这里**不能**当成 reserved——
            # 报 pending 让调用方重试一轮，下一轮就能抢到。
            return "pending", None
        if row["status"] == VIDEO_STATUS_READY:
            return "ready", row

        # 别人占着位，但那个占位可能早就死了（进程被杀、模型挂死）——
        # 不回收的话这个链接就永久不可解析。条件写进 WHERE，保证并发下
        # 只有一个人接管成功；抢输的人下一轮再试。
        if _pending_is_stale(row):
            now = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                # 同样只在**为空时**填：接管的是别人的占位行，
                # 它可能已经带了卡片字段，不能覆盖。
                cursor = conn.execute(
                    """UPDATE videos SET parsed_by = ?, created_at = ?, updated_at = ?,
                              video_title = CASE WHEN COALESCE(video_title, '') = '' THEN ? ELSE video_title END,
                              cover_url    = CASE WHEN COALESCE(cover_url, '')    = '' THEN ? ELSE cover_url    END
                       WHERE canonical_url = ? AND status = ? AND updated_at = ?""",
                    (user_id, now, now, video_title or "", cover_url or "",
                     canonical_video_url(video_url), VIDEO_STATUS_PENDING,
                     row["updated_at"]),
                )
                if cursor.rowcount == 1:
                    return "reserved", None
        return "pending", row


def complete_video(
    video_url: str,
    summary_md: str = "",
    mindmap_md: str = "",
    tags: list | None = None,
    subtitle_text: str = "",
    template: str | None = None,
) -> int:
    """占位者把解析结果写回**自己占的那一行**，返回更新的行数。

    ``WHERE status = 'pending'`` 不是多余的防御：它是「社区内容不会被
    任何人改写」这条承诺的落地点——已 ready 的行谁都改不了，包括
    后来的解析者。返回 0 说明这一行已经不是 pending（正常流程下不会
    发生），由调用方报警而不是静默当作成功。

    ``template`` 是这次用的提示词模板 id（工单 #105）。不传 = 默认模板，
    取自 ``prompts/templates.json``——列的默认值不能另写一份。
    """
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(list(tags) if tags else [], ensure_ascii=False)
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE videos
               SET status = ?, summary_md = ?, mindmap_md = ?, tags = ?,
                   subtitle_text = ?, template = ?, updated_at = ?
               WHERE canonical_url = ? AND status = ?""",
            (
                VIDEO_STATUS_READY, summary_md, mindmap_md, payload,
                subtitle_text, template or default_template_id(), now,
                canonical_video_url(video_url),
                VIDEO_STATUS_PENDING,
            ),
        )
        return cursor.rowcount


def regenerate_video(
    video_url: str,
    user_id: int | None,
    *,
    subtitle_text: str,
    summary_md: str = "",
    mindmap_md: str = "",
    tags: list | None = None,
    template: str | None = None,
) -> int:
    """作者本人改写**自己那份**已完成的总结，返回更新的行数（ADR 0007）。

    与 complete_video 是两条独立的语句，不是同一句的两个开关：

    - ``WHERE status = 'ready'``：只改已完成的内容，不碰别人正在解析的占位。
      覆盖不经过占位——重新解析的那几十秒里旧内容仍然有效，改回 pending
      会让所有复用者突然看到空白。
    - ``AND parsed_by IS ?``：写权限的判定落在这里，而不是调用方的 if 里。
      用 ``IS`` 而不是 ``=``：parsed_by 可为 NULL（建表早期未登录的记录），
      ``=`` 在 NULL 上永不成立，而 ``IS`` 才能正确表达「相等或两边都是 NULL」。

    **``subtitle_text`` 是必填的，且排在 ``*`` 之后强制关键字**，因为「忘传」
    在这个函数里不是无害的（工单 #21）：

    - 给了默认值 ``""`` 时，忘传就写入空串——而字幕是**社区共用内容**
      （ADR 0002），清空它不可逆；本函数**仍返回 1**（行确实被更新了），
      调用方看不到任何异常。
    - 仅仅把它插到 ``summary_md`` 之前也不够：那样它仍可省略，未来的
      **位置调用**会把「总结」错传进 ``subtitle_text``，于是 summary 变空而
      **不报错**。加 ``*`` 之后位置调用是 ``TypeError``。

    刻意**不加** ``COALESCE`` 守卫：覆盖时字幕跟着更新是对的——字幕与总结同源，
    分开维护只会让用户看到「旧字幕配新总结」。要防的是「忘传」，不是「更新」。

    （顺带纠正一个曾被记录的判断：「覆盖失败会连带清空字幕」在生产路径上
    **不可达**——``api_summarize.py:540`` 的 ``if not has_subtitle: return``
    早就把空字幕挡在外面，:642 是唯一调用点且必传刚判定过非空的值。）

    ``template`` 是这次用的提示词模板 id（工单 #105）。不传 = 默认模板，
    取自 ``prompts/templates.json``——与 complete_video 同一口径。

    返回 0 有两种含义，调用方必须区分：这一行不存在 / 不是 ready（被并发释放或
    从未完成），或者它属于别人。两种都不该被静默当成成功。
    """
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(list(tags) if tags else [], ensure_ascii=False)
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE videos
               SET summary_md = ?, mindmap_md = ?, tags = ?,
                   subtitle_text = ?, template = ?, updated_at = ?
               WHERE canonical_url = ? AND status = ? AND parsed_by IS ?""",
            (
                summary_md, mindmap_md, payload,
                subtitle_text, template or default_template_id(), now,
                canonical_video_url(video_url),
                VIDEO_STATUS_READY, user_id,
            ),
        )
        return cursor.rowcount


def release_video(video_url: str, user_id: int | None) -> int:
    """占位者失败时把位置还回去，返回删掉的行数。

    只删 pending 行：已经 ready 的社区内容与占位无关，绝不能被回滚删掉。

    不还回去的代价是永久性的——那一行会永远停在 pending，
    后来的每个用户都只能干等到超时，再也没人能解析这个链接。

    ``AND parsed_by IS ?`` 不是多余的防御，它是**写权限**的判定落点：
    只删「本来就是我占的那一行」。

    少了它有一条现实的失效链：占位被 TTL 接管之后（见 reserve_video 的
    陈旧占位分支），位置已经归了另一个人，而**前一个**占位者的 finally
    还在跑——它一删，接管者的整次解析就没了，而接管者的 finally 不会发 done，
    社区里也永远不会有他那份总结。

    用 ``IS`` 而不是 ``=``：parsed_by 可为 NULL（建表早期未登录的记录），
    ``=`` 在 NULL 上永不成立，而 ``IS`` 才能正确表达「相等或两边都是 NULL」。
    与 regenerate_video 的 ``AND parsed_by IS ?`` 同一口径。
    """
    with get_db() as conn:
        cursor = conn.execute(
            """DELETE FROM videos
               WHERE canonical_url = ? AND status = ? AND parsed_by IS ?""",
            (canonical_video_url(video_url), VIDEO_STATUS_PENDING, user_id),
        )
        return cursor.rowcount


def get_video_by_url(video_url: str) -> dict | None:
    """读社区里那一份结果（tags 已反序列化成列表）。没有则 None。

    这是「视频是否已存在」「取某视频的字幕」唯一的查询入口——
    不再查 parse_history（ADR 0001）：那张表是按 (user_id, video_url)
    去重的个人记录，跨用户看不见别人的解析结果。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE canonical_url = ?",
            (canonical_video_url(video_url),),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["tags"] = _decode_tags_text(item.get("tags"))
    return item


# ── 邀请码（工单 #99 / #103）──────────────────────────────
#
# 注册必须带邀请码，码由管理员发放。四类失效原因要分开说（用户据此
# 才知道该做什么）：不存在 / 已作废 / 已过期 / 已用完。
#
# **只存哈希**：库里存的是 sha256(明文)，不是明文——库被读走不等于
# 码被用掉（工单 #103 要解决的就是这件事）。代价是明文取不回来，所以
# **明文只在创建响应里给那一次**；之后的列表出口给的是**脱敏提示**
# （前 4 后 4，中间 ****）——它不是明文，不能用来注册。
#
# 码本身也必须够强：猜中一枚码就能注册，所以组合空间至少 128 bit
# （下界是**可执行断言**，见下面 INVITE_CODE_ENTROPY_BITS）。

#: invite_codes 的建表语句。**唯一一份**——executescript 与老库升级路径
#: （_migrate_invite_codes_table）引用同一个常量，两处漂移的后果是
#: 老库缺列而新库不缺。
#:
#: 列的含义：
#:
#: - ``code_hash``：**唯一**，sha256(明文)。库里没有明文。
#: - ``code_hint``：给管理员看的脱敏提示（前 4 后 4，中间 ****）。它是
#:   建码那一刻从明文算出来存下的——**从哈希算不出来**，所以只能存。
#:   它不是明文，不能用来注册。
#: - ``created_by``：发放者的 user id，**不建外键**：与 videos.parsed_by
#:   同一理由——发放者被删之后，码本身还得留着（已注册的账号不该因为
#:   管理员换人就失效）。
#: - ``created_at``：发放时刻。
#: - ``expires_at``：为空 = 永不过期。
#: - ``max_uses``：总名额（≥ 1）；``used_count``：已用数。
#: - ``revoked``：作废位。作废与用尽是两件事——用尽是「名额用完了」，
#:   作废是「这个码提前收回」，都拒绝注册，但管理员要能分开看。
#: - ``note``：给管理员看的备注（这枚码给了谁）。**可空**。
_INVITE_CODES_DDL = """
    CREATE TABLE IF NOT EXISTS invite_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code_hash TEXT NOT NULL UNIQUE,
        code_hint TEXT NOT NULL DEFAULT '',
        created_by INTEGER,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        max_uses INTEGER NOT NULL DEFAULT 1,
        used_count INTEGER NOT NULL DEFAULT 0,
        revoked INTEGER NOT NULL DEFAULT 0,
        note TEXT
    )
"""

#: 邀请码字符集。**去掉易混字符**：0/O、1/I/L、2/Z、5/S、8/B。
#:
#: 码是要被人抄过去的——管理员口头告诉用户、截图发过去——字形相近的
#: 字符抄错一次的代价是「用户填了却注册不了」，而用户看到的提示是
#: 「邀请码无效」，不会怀疑是抄错了。
#:
#: **25 个互异字符**（20 个字母 + 5 个数字）。
INVITE_CODE_ALPHABET = "ABCDEFGHJKMNPRTUVWXY34679"

#: 码长。28 位 × 每个位置 log2(25) ≈ 4.644 bit ≈ **130.0 bit**
#: ≈ 1.07e39 种可能。
INVITE_CODE_LENGTH = 28

#: 组合空间的比特数 = 码长 × log2(字符集大小)。**用 math.log2 算**，
#: 不写死数字——字符集或码长一改，这个值跟着变。
#:
#: **下界（≥128 bit）的可执行断言在测试里**，不在本模块：
#: ``backend/tests/test_invite_codes.py`` 里那条 ``test_the_code_space_has_at_least_128_bits``
#: 独立重算一遍并 assert ≥ 128。为什么不写成模块级 assert：模块级
#: assert 在 import 时触发，字符集被改弱的后果是**整个后端起不来**——
#: 而这是一件 CI 就能拦住的事，不该拿线上可用性去换。测试里的断言是
#: 同一件事的正确位置：它拦得住提交，且不会砸掉服务。
INVITE_CODE_ENTROPY_BITS = INVITE_CODE_LENGTH * math.log2(len(INVITE_CODE_ALPHABET))


class InviteRegistrationError(Exception):
    """注册被拒的原因。``detail`` 是给用户看的中文。

    为什么不让数据层直接抛 HTTPException：数据层抛 HTTP 错误会把它
    钉死在「只能在 HTTP 路径上用」，而它真正的职责是
    「校验 + 建号 + 计数，一笔交易」。翻译成 HTTP 状态码是路由层的活。
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def hash_invite_code(code: str) -> str:
    """明文邀请码 → 存库用的 sha256 十六进制摘要（工单 #103）。

    ## 为什么是 sha256，不是 bcrypt / argon2

    慢哈希的价值在于「人选的弱口令要慢慢试」。邀请码不是弱口令——它是
    130 bit 的全随机串，没有字典可打，KDF 的「慢」在这里只换来注册延迟，
    换不来安全性。sha256 是标准库自带，**不引新依赖**（依赖是攻击面）。

    ## 为什么存摘要就够

    摘要不可逆 ⇒ 库里没有明文 ⇒ **库被读走不等于码被用掉**：攻击者拿到
    的是 32 位十六进制摘要，想反推明文只能猜，而码的组合空间是 2^130。

    ## 一处定义

    建码时算一次、验码时算一次，两边必须算的是同一个东西——所以这个
    函数是**唯一**的哈希入口，调用方不许自己拼 hashlib。
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def generate_invite_code(length: int = INVITE_CODE_LENGTH) -> str:
    """生成一枚邀请码（随机、大写、无易混字符）。

    用 ``secrets`` 而不是 ``random``：码是注册闸门，猜中就能注册，
    必须用密码学安全的随机源——``random`` 的可预测性在这里是漏洞。

    组合空间由 ``INVITE_CODE_ENTROPY_BITS`` 给出；「≥ 128 bit」这条下界
    是一条**可执行断言**，在 ``backend/tests/test_invite_codes.py`` 里
    （``test_the_code_space_has_at_least_128_bits``）——它独立重算一遍，
    不抄本模块的常量。
    """
    return "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(length))


def mask_invite_code(code: str) -> str:
    """脱敏：只回前 4 后 4。

    列表接口不得回显完整码（工单 #99）：明文只在创建响应里给一次。

    这个形态**是对外契约**（字段 ``code_masked``，值形如 ``abcd****wxyz``），
    前端管理页直接显示它——改了它前端就显示错了。
    """
    if not code:
        return ""
    if len(code) <= 8:
        return code[:4] + "****"
    return code[:4] + "****" + code[-4:]


def _iso_utc(moment: datetime | None = None) -> str:
    """UTC ISO-8601（秒级、固定格式）。

    固定格式是刻意的：``expires_at`` 的比较用字符串字典序就成立
    （同一时区、同一精度），不必每次都 parse 一遍。
    """
    stamp = moment if moment is not None else datetime.now(timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_iso_utc(raw: str | None) -> datetime | None:
    """把 expires_at 解析成带时区的 datetime；空值或坏值返回 None。"""
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _invite_code_expired(expires_at: str | None, now: datetime | None = None) -> bool:
    """这枚码是否已过期。

    空值 = 永不过期（设计如此）。

    **坏值按过期处理**（fail closed）：expires_at 只有本模块写，出现
    坏值说明数据被改坏了；此时放行等于把闸门敞开。
    """
    if not expires_at or not expires_at.strip():
        return False
    deadline = _parse_iso_utc(expires_at)
    if deadline is None:
        return True
    moment = now if now is not None else datetime.now(timezone.utc)
    return deadline <= moment.astimezone(timezone.utc)


# 邀请码的四种状态，互斥且穷尽。顺序即优先级：
# 作废 > 过期 > 用尽 > 可用。
INVITE_CODE_STATUSES = ("active", "exhausted", "expired", "revoked")

# 一次最多发多少枚邀请码（工单 #104：批量发码）。
# 上限存在的理由不是「数据库扛不住」，而是**明文只给一次**这件事：
# 一次发 200 枚意味着管理员要手动复制 200 行，而绝大多数码根本没人要。
INVITE_CODE_BATCH_MAX = 20


def invite_code_status(expires_at: str | None, used_count: int, max_uses: int,
                       revoked: int) -> str:
    """这枚码现在的状态——**唯一一份算法**（工单 #104）。

    两种坏值都按「保守」处理：坏日期算过期（放行等于把闸门敞开），
    坏计数算用尽（多给一次名额就是多开一道门）。

    这个函数被两处调用：列表投影（每行的 ``status``）与状态筛选
    （注册成 SQL 的 ``invite_status``）。**两处必须是同一份算法**——
    分成两份的话，筛选出来的行和它显示的状态会不一致，而那种不一致
    不报错，只是「筛了却看不到」「看到却筛不出来」。
    """
    if revoked:
        return "revoked"
    if _invite_code_expired(expires_at):
        return "expired"
    try:
        used = int(used_count)
        limit = int(max_uses)
    except (TypeError, ValueError):
        return "exhausted"
    if used >= limit:
        return "exhausted"
    return "active"


def _invite_code_item(row, *, include_code: bool = False, code: str | None = None) -> dict:
    """邀请码的对外投影。

    ``include_code=False``（列表）**不回显完整码**，只给 code_masked；
    ``include_code=True``（创建响应）才给明文，且只给那一次。

    库里**没有明文**（工单 #103），所以列表出口的掩码直接读 ``code_hint``
    这一列——它是建码那一刻从明文算出来存下的（从哈希算不出来）。
    创建出口的明文由调用方（create_invite_code）**当场**传进来。

    状态字段给的是**算出来的**，不是存出来的：过期与否取决于「现在」，
    而库里不存「已过期」这个状态——存了就与时间脱钩，第二天还得改一遍。
    """
    expired = _invite_code_expired(row["expires_at"])
    exhausted = row["used_count"] >= row["max_uses"]
    # 状态只由 invite_code_status 算（工单 #104）：筛选与显示共用一份算法。
    status = invite_code_status(
        row["expires_at"], row["used_count"], row["max_uses"], row["revoked"]
    )
    item = {
        "id": row["id"],
        "created_by": row["created_by"],
        # 发放人邮箱：管理员要能看出「这枚码是谁发的」。
        # 没 JOIN 到 users 时（老的 SELECT * 出口）就是 None。
        "created_by_email": (row["created_by_email"]
                             if "created_by_email" in row.keys() else None),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "max_uses": row["max_uses"],
        "used_count": row["used_count"],
        "revoked": row["revoked"],
        "note": row["note"],
        "expired": expired,
        "exhausted": exhausted,
        "status": status,
    }
    if include_code:
        item["code"] = code
    else:
        item["code_masked"] = row["code_hint"]
    return item


def invite_code_expiry(hours: int | None) -> str | None:
    """「几小时后过期」→ expires_at 时间戳（UTC ISO-8601）。None = 永不过期。

    放在数据层是因为**时间戳格式只有这一处定义**：管理端算「24 小时」
    要是自己拼一次字符串，就多了一个真相来源，而两处格式一旦漂移，
    症状是「码没过期」或「码刚发就过期」——都很难查。
    """
    if hours is None:
        return None
    return _iso_utc(datetime.now(timezone.utc) + timedelta(hours=hours))


def create_invite_codes(count: int = 1, created_by: int | None = None, max_uses: int = 1,
                         expires_at: str | None = None, note: str | None = None) -> list[dict]:
    """建 **count** 枚邀请码，返回**含明文码**的行列表。

    明文只在这里给一次：之后所有列表出口都只给脱敏码。

    **库里只存哈希**（工单 #103）：落库的是 ``code_hash``（sha256）与
    ``code_hint``（脱敏提示），明文不出这个函数。

    批量（工单 #104）为什么在这一层而不是管理端拼循环：一次事务里发多枚，
    撞码重试也在同一个事务里，**不会出现「发到第 3 枚时前两枚已落库、
    第 3 枚失败」这种半截状态**。
    """
    if count < 1:
        raise ValueError("至少要发 1 枚邀请码")
    if count > INVITE_CODE_BATCH_MAX:
        raise ValueError(f"一次最多发 {INVITE_CODE_BATCH_MAX} 枚")
    if max_uses < 1:
        raise ValueError("max_uses 至少是 1")
    items: list[dict] = []
    with get_db() as conn:
        for _ in range(count):
            for _attempt in range(8):
                code = generate_invite_code()
                try:
                    cursor = conn.execute(
                        "INSERT INTO invite_codes (code_hash, code_hint, created_by, created_at,"
                        " expires_at, max_uses, used_count, revoked, note)"
                        " VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)",
                        (
                            hash_invite_code(code),
                            mask_invite_code(code),
                            created_by,
                            _iso_utc(),
                            expires_at,
                            max_uses,
                            note,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # 撞上重复的码（概率极低）：换一枚重试。
                    continue
                row = conn.execute(
                    "SELECT i.*, u.email AS created_by_email FROM invite_codes i"
                    " LEFT JOIN users u ON u.id = i.created_by WHERE i.id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                items.append(_invite_code_item(row, include_code=True, code=code))
                break
            else:
                raise RuntimeError("生成邀请码失败：连续多枚都撞上了重复的码")
    return items


def create_invite_code(created_by: int | None = None, max_uses: int = 1,
                        expires_at: str | None = None, note: str | None = None) -> dict:
    """建一枚邀请码，返回**含明文码**的行。

    等价于批量发 1 枚——单枚出口是批量出口的薄封装，不是第二份实现。
    """
    return create_invite_codes(
        1, created_by=created_by, max_uses=max_uses, expires_at=expires_at, note=note
    )[0]


def get_invite_code_by_id(invite_id: int) -> dict | None:
    """按 id 读一枚码（脱敏投影）。没有则 None。

    与列表用**同一个 JOIN 投影**：发放人邮箱是列表和单枚读都要给的字段，
    两个出口的形状必须一致——作废接口回的单枚若少这个字段，调用方会
    以为「这枚码没人发过」。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT i.*, u.email AS created_by_email FROM invite_codes i"
            " LEFT JOIN users u ON u.id = i.created_by"
            " WHERE i.id = ?",
            (invite_id,),
        ).fetchone()
    return _invite_code_item(row) if row else None


def list_invite_codes(limit: int = ADMIN_PAGE_SIZE_DEFAULT, offset: int = 0,
                      status: str | None = None) -> dict:
    """邀请码列表（require_admin 保护）。返回 {items, total, limit, offset}。

    **不回显完整码**：这一层只给 code_masked（工单 #99）。

    按 id 降序：刚发的码排在最前，管理员不用翻页去找自己刚生成的那枚。

    ## status 筛选（工单 #104）

    筛选与每行显示的 ``status`` 用**同一份算法**：筛选走 SQL 里的
    ``invite_status(...)``（注册的就是 ``invite_code_status`` 那个函数），
    显示走同一个函数的直接调用。两处一旦漂移，症状是「筛了却看不到」
    或「看到却筛不出来」——而且都不报错。

    ``total`` 也按筛选后的集合算：分页器的总页数要跟列表对得上，
    否则翻到最后一页会出现空页。
    """
    limit, offset = _clamp_limit_offset(limit, offset)
    where = ""
    params: list = []
    if status:
        if status not in INVITE_CODE_STATUSES:
            raise ValueError(f"未知的邀请码状态：{status}")
        where = (" WHERE invite_status(i.expires_at, i.used_count, i.max_uses, i.revoked) = ?")
        params.append(status)
    with get_db() as conn:
        total = conn.execute(
            f"SELECT count(*) FROM invite_codes i{where}", params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT i.*, u.email AS created_by_email FROM invite_codes i"
            " LEFT JOIN users u ON u.id = i.created_by"
            f"{where} ORDER BY i.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {
        "items": [_invite_code_item(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def revoke_invite_code(invite_id: int) -> int:
    """作废一枚码。返回受影响的行数（0 = 没有这枚码 → 404）。

    **幂等**：已作废的码再作废一次仍然成功（返回 1）——管理员点两下
    不该收到报错，那是误报。
    """
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE invite_codes SET revoked = 1 WHERE id = ?", (invite_id,)
        )
        return cursor.rowcount


def register_with_invite_code(email: str, password_hash: str, invite_code: str) -> dict:
    """校验邀请码 + 建用户 + 给码计数，**同一事务**（工单 #99）。

    返回 ``{"id": ..., "email": ...}``；被拒时抛 ``InviteRegistrationError``。

    ## 查码用哈希，不是明文

    工单 #103 之后库里没有明文，所以查找条件是 ``code_hash = sha256(输入)``。
    查不到就等于「这枚码不存在」——哈希碰撞在这里不构成问题（要撞出一枚
    能用的码得先知道明文，而明文只在创建那一刻存在过）。

    ## 为什么必须是一个事务

    「校验 → 建号 → 计数」跨两张表。不是一个事务就会出两种歪账：
    码被消耗了而账号没建成（用户白丢一个码），或账号建成了而码没计数
    （同一个名额被用两次）。所以三步放进**一个**事务，任一步失败整笔
    回滚——两种歪账都出不来。

    ## 为什么是 BEGIN IMMEDIATE

    默认的 DEFERRED 到**第一个写语句**才拿锁，那时两个并发请求都已经
    读到了 used_count=0，然后各自写回 1：谁都没被拒绝，两个账号共用
    一个名额，而且**没有任何报错**。IMMEDIATE 在事务开头就拿写锁，
    第二个请求只能等；等完醒来看到的已是 used_count=1，于是被拒。
    """
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM invite_codes WHERE code_hash = ?", (hash_invite_code(invite_code),)
        ).fetchone()
        if row is None:
            raise InviteRegistrationError("邀请码无效")
        if row["revoked"]:
            raise InviteRegistrationError("邀请码已作废")
        if _invite_code_expired(row["expires_at"]):
            raise InviteRegistrationError("邀请码已过期")
        if row["used_count"] >= row["max_uses"]:
            raise InviteRegistrationError("邀请码已被用完")
        if conn.execute(
            "SELECT 1 FROM users WHERE email = ?", (email,)
        ).fetchone() is not None:
            raise InviteRegistrationError("该邮箱已注册")
        try:
            cursor = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                (email, password_hash),
            )
        except sqlite3.IntegrityError:
            # 邮箱唯一约束：并发下另一个请求刚好把同一个邮箱建了。
            # 抛出去 → 整笔回滚 → 码不被消耗。
            raise InviteRegistrationError("该邮箱已注册") from None
        consumed = conn.execute(
            "UPDATE invite_codes SET used_count = used_count + 1"
            " WHERE id = ? AND used_count < max_uses AND revoked = 0",
            (row["id"],),
        ).rowcount
        if not consumed:
            # 走到这里说明计数没生效：宁可整笔回滚，也不留
            # 「账号建成了但码没计数」的歪账。
            raise InviteRegistrationError("邀请码已被用完")
        return {"id": cursor.lastrowid, "email": email}
