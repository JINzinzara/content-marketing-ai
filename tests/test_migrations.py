"""이전 스키마의 계정·게시물·제작권이 재시작과 변경 후에도 남는지 검증"""

import sqlite3
from pathlib import Path

from app.config import Settings
from app.db import balance, connect, initialize, owned_post


def test_existing_database_is_upgraded_without_losing_rows_or_trial_history(tmp_path):
    cfg = Settings(data_dir=tmp_path / "legacy")
    cfg.prepare()
    old_schema = (
        (Path(__file__).parents[1] / "app/schema.sql")
        .read_text()
        .split("CREATE TABLE IF NOT EXISTS style_references")[0]
    )
    with sqlite3.connect(cfg.data_dir / "app.sqlite3") as con:
        con.executescript(old_schema)
        con.execute(
            "INSERT INTO accounts(id, name, created_at) VALUES ('existing', '기존 카페', 1)"
        )
        con.execute(
            "INSERT INTO credits(owner_id, reference, amount, created_at) VALUES ('existing', 'trial', 5, 1)"
        )
        con.execute(
            "INSERT INTO credits(owner_id, reference, amount, created_at) VALUES ('existing', 'post:old', -1, 1)"
        )
        con.execute(
            """INSERT INTO posts(id, owner_id, product_name, facts, purpose, original_file, title, caption, layout, aspect, color, created_at)
                VALUES ('old', 'existing', '기존 상품', '확인한 사실', '신메뉴 안내', 'old.jpg', '제목', '본문', 'frame', 'portrait', '#314e3e', 1)"""
        )
        con.execute(
            "UPDATE posts SET price='6,500원', finished_file='old-final.jpg', rendered_revision=1, finalized_at=1 WHERE id='old'"
        )
        con.execute(
            """INSERT INTO posts(id, owner_id, product_name, facts, price, purpose, original_file, title, caption, layout, aspect, color, created_at, publish_state)
                VALUES ('pending', 'existing', '발행 중 상품', '', '6,500원', '신메뉴 안내', 'pending.jpg', '제목', '본문', 'frame', 'portrait', '#314e3e', 1, 'publishing')"""
        )
    initialize(cfg)
    initialize(cfg)
    with connect(cfg) as con:
        post = owned_post(con, "existing", "old")
        assert post["product_name"] == "기존 상품" and post["style_snapshot"] == "{}"
        assert (
            post["selected_image_id"] is None
            and post["show_price"] == 0
            and balance(con, "existing") == 4
        )
        assert post["revision"] == 2 and post["rendered_revision"] == 1
        assert post["finished_file"] == "old-final.jpg" and post["caption"] == "본문"
        pending = owned_post(con, "existing", "pending")
        assert (
            pending["revision"] == 1
            and pending["show_price"] == 1
            and pending["publish_state"] == "publishing"
        )
        assert con.execute("SELECT COUNT(*) FROM credits").fetchone()[0] == 2
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3
