import os

# 数据库连接串，由 docker-compose 注入；默认指向 compose 中的 db 服务
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://matboard:matboard@db:5432/matboard",
)
