import os

# App imports initialize the shared engine during test collection.
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
