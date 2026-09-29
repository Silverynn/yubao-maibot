"""新交付包必须完整可核验，并排除真实运行数据。"""

import hashlib
import importlib.util
import json
import shutil
import unittest
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_heart_delivery", ROOT / "scripts/build_heart_delivery.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class HeartDeliveryTests(unittest.TestCase):
    def test_source_manifest_has_all_required_host_interfaces(self):
        paths = {relative for relative, _ in builder.source_files()}
        expected = set(builder.CORE_FILES) | set(builder.OPTIONAL_LIVE2D_FILES)
        self.assertTrue(expected <= paths)
        self.assertEqual(len([p for p in paths if p.startswith("patches/heart-")]), 7)
        self.assertTrue(any(p.endswith("webui_labels.py") for p in paths))
        self.assertFalse(any(p.startswith((".runtime/", "logs/", ".secrets/")) for p in paths))
        self.assertFalse(any("live2d-models" in p or "__pycache__" in p for p in paths))

    def test_package_hashes_and_zip_match_and_existing_output_is_protected(self):
        # 当前 Windows 的 TemporaryDirectory 会带拒绝访问 ACL；普通目录可写。
        parent = ROOT / ".runtime"
        parent.mkdir(parents=True, exist_ok=True)
        output = parent / f"heart-delivery-test-{uuid.uuid4().hex}"
        archive = output.with_name(output.name + ".zip")
        try:
            folder, archive, count = builder.build(output)
            hashes = json.loads((folder / "SHA256.json").read_text(encoding="utf-8"))
            self.assertEqual(len(hashes), count)
            self.assertIn("README.md", hashes)
            self.assertIn("extensions/heart_memory_scope.py", hashes)
            self.assertIn("patches/heart-memory-scope.patch", hashes)
            with zipfile.ZipFile(archive) as zipped:
                self.assertIsNone(zipped.testzip())
                self.assertEqual(set(zipped.namelist()), set(hashes) | {"SHA256.json"})
                for relative, expected in hashes.items():
                    self.assertEqual(hashlib.sha256(zipped.read(relative)).hexdigest(), expected)
            with self.assertRaises(FileExistsError):
                builder.build(output)
        finally:
            # 只清理这次创建、且已确认在隔离测试目录内的目标。
            if output.resolve().parent != parent.resolve() or not output.name.startswith("heart-delivery-test-"):
                raise RuntimeError("拒绝清理未核对的测试目录")
            if output.exists():
                shutil.rmtree(output)
            if archive.exists():
                archive.unlink()


if __name__ == "__main__":
    unittest.main()
