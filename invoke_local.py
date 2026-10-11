"""Run the real ingest against the real Open-Meteo API, with fake AWS stores.
No AWS account or credentials needed. Run:  python invoke_local.py
Writes local_snapshot.json (what would go to S3) and prints a summary."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from handler import SNAPSHOT_KEY, run_ingest          # noqa: E402
from openmeteo_client import OpenMeteoClient          # noqa: E402


class FakeTable:
    def __init__(self): self.items = []
    def batch_writer(self):
        outer = self
        class BW:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def put_item(s, Item): outer.items.append(Item)
        return BW()


class FakeS3:
    def __init__(self): self.objects = {}
    def put_object(self, Bucket, Key, Body, **kw): self.objects[Key] = Body


table, s3 = FakeTable(), FakeS3()
summary = run_ingest(OpenMeteoClient(max_calls=20), table, s3, "local")
print(json.dumps(summary, indent=2))
Path("local_snapshot.json").write_bytes(s3.objects[SNAPSHOT_KEY])
print(f"\n{len(table.items)} items 'written'. Sample item:")
d = next((i for i in table.items if i["name"] == "Delhi"), table.items[0])
print(json.dumps(d, indent=2, default=str))
print("\nSaved local_snapshot.json")
