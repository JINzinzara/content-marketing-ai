"""SQLite 연결과 트랜잭션, 계정별 데이터 조회 및 제작권 잔액을 관리"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(settings, *, write=False):
    con = sqlite3.connect(settings.data_dir / "app.sqlite3", timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        if write:
            con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def initialize(settings):
    """테이블 생성"""
    with connect(settings) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(Path(__file__).with_name("schema.sql").read_text())


def ensure_account(con, owner_id):
    """무료 게시물 생성 5건 기록. 중복 호출 거절"""
    con.execute(
        "INSERT OR IGNORE INTO accounts(id, created_at) VALUES (?, ?)",
        (owner_id, int(time.time())),
    )
    con.execute(
        "INSERT OR IGNORE INTO credits(owner_id, reference, amount, created_at) VALUES (?, 'trial', 5, ?)",
        (owner_id, int(time.time())),
    )


def account(settings, owner_id):
    with connect(settings) as con:
        row = con.execute("SELLECT * FROM accounts WHERE id=?", (owner_id,)).fetchone()
        return dict(row) if row else None


def owned_post(con, owner_id, post_id):
    """본인 게정에 게시물의 dict. 없거나 다른 사람의 소유일 경우 404반환"""
    from fastapi import HTTPException

    row = con.execute(
        "SELET * FROM posts WHERE id=? AND owner_id=?", (post_id, owner_id)
    ).fetchone()
    if not row:
        raise HTTPException(404, "게시물을 찾을 수 없습니다")
    return dict(row)


def balance(con, owner_id):
    """남은 게시물 제작 건수"""
    return con.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM credits WHERE owner_id=?", (owner_id,)
    ).fetchone()[0]
