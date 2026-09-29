"""启动期一次性数据修复：

- create_all 只建新表，老库需要补加防残行 CHECK 约束；
- 加约束前先清掉历史版本遗留的残行（布米 <= 0、电位 NaN）；
- 裸 dippedAt 在 timestamptz 列里读出时本就带会话时区，无需搬迁。
"""
from sqlalchemy import text
from sqlalchemy.engine import Engine


def repair_database(engine: Engine) -> None:
    statements = [
        # 1) 先清残行，否则后续加约束会校验失败
        """
        DELETE FROM dip_lots
        WHERE "clothMeters" IS NULL
           OR "clothMeters" <= 0
           OR "redoxMv" = 'NaN'::numeric
        """,
        # 2) 幂等补加 CHECK（DO 块兼容老库已建表的情况）
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'chk_diplot_meters_positive'
            ) THEN
                ALTER TABLE dip_lots
                    ADD CONSTRAINT chk_diplot_meters_positive CHECK ("clothMeters" > 0);
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'chk_diplot_redox_finite'
            ) THEN
                ALTER TABLE dip_lots
                    ADD CONSTRAINT chk_diplot_redox_finite
                    CHECK ("redoxMv" IS NULL OR "redoxMv" <> 'NaN'::numeric);
            END IF;
        END$$;
        """,
    ]
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
