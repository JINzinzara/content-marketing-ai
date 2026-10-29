"""계정별 SQLite 저장과 트랜잭션, 데이터 보존형 스키마 변경 담당"""

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
    """기존 행을 지우지 않고 테이블과 누락된 게시물 row를 준비"""
    with connect(settings) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(Path(__file__).with_name("schema.sql").read_text())
        columns = {row["name"] for row in con.execute("PRAGMA table_info(posts)")}
        for name, definition in {
            "style_snapshot": "TEXT NOT NULL DEFAULT '{}'",
            "selected_image_id": "TEXT",
            "show_price": "INTEGER NOT NULL DEFAULT 0 CHECK(show_price IN (0,1))",
            "deleted_at": "INTEGER",
            "creation_measurement_id": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                con.execute(f"ALTER TABLE posts ADD COLUMN {name} {definition}")
                if name == "show_price":
                    # 이전 가격 합성 파일을 새 비표시 미리보기의 완성본으로 다운로드 불가
                    # 이미 발행 처리 중인 버전은 그때 확인한 표시 상태를 유지
                    con.execute(
                        "UPDATE posts SET show_price=1 WHERE price!='' AND publish_state IN ('creating','container_ready','publishing','unknown')"
                    )
                    con.execute(
                        "UPDATE posts SET revision=revision+1 WHERE price!='' AND publish_state NOT IN ('creating','container_ready','publishing','unknown')"
                    )
        if "parameters" not in {
            row["name"] for row in con.execute("PRAGMA table_info(image_generations)")
        }:
            con.execute(
                "ALTER TABLE image_generations ADD COLUMN parameters TEXT NOT NULL DEFAULT '{}'"
            )
        for table in ("style_references", "history"):
            if "deleted_at" not in {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}:
                con.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at INTEGER")
        if con.execute("PRAGMA user_version").fetchone()[0] < 4:
            # 제목을 빼기 전 완성본을 새 미리보기와 같은 파일로 취급하지 않는다.
            con.execute("UPDATE posts SET revision=revision+1 WHERE finished_file IS NOT NULL AND publish_state NOT IN ('creating','container_ready','publishing','unknown')")
        if con.execute("PRAGMA user_version").fetchone()[0] < 5:
            # 예전 프레임 완성본을 새 꽉 찬 미리보기와 동일한 버전으로 취급하지 않는다.
            con.execute("UPDATE posts SET revision=revision+1, layout='split' WHERE deleted_at IS NULL AND publish_state NOT IN ('creating','container_ready','publishing','unknown')")
        con.execute("PRAGMA user_version=5")


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
        row = con.execute("SELECT * FROM accounts WHERE id=?", (owner_id,)).fetchone()
        return dict(row) if row else None


def owned_post(con, owner_id, post_id):
    """본인 게정에 게시물의 dict. 없거나 다른 사람의 소유일 경우 404반환"""
    from fastapi import HTTPException

    row = con.execute(
        "SELECT * FROM posts WHERE id=? AND owner_id=? AND deleted_at IS NULL", (post_id, owner_id)
    ).fetchone()
    if not row:
        raise HTTPException(404, "게시물을 찾을 수 없습니다")
    return dict(row)


def balance(con, owner_id):
    """남은 게시물 제작 건수"""
    return con.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM credits WHERE owner_id=?", (owner_id,)
    ).fetchone()[0]
