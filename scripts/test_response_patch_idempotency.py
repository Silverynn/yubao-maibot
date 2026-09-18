"""在临时目录检查回复补丁，绝不执行生产启动流程。"""

import ast
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
REPLY_PATH = Path("src/maisaka/builtin_tool/reply.py")
MARKER = "    target_additional_config = target_message.message_info.additional_config\n"
ANCHOR = "    try:\n        replyer = replyer_manager.get_replyer(\n"


def remove_repeated_reply_setup(text: str) -> tuple[str, int]:
    """仅收拢连续且逐字相同的已知初始化块；不猜测或删除不同逻辑。"""
    count = text.count(MARKER)
    if count <= 1:
        return text, 0
    start = text.index(MARKER)
    end = text.index(ANCHOR, start)
    region = text[start:end]
    blocks = [MARKER + part for part in region.split(MARKER)[1:]]
    if len(blocks) != count or any(block != blocks[0] for block in blocks):
        raise ValueError("初始化块存在不同内容，停止自动清理")
    return text[:start] + blocks[0] + text[end:], count - 1


class ReplyPatchTest(unittest.TestCase):
    def test_reply_patch_sequence_is_idempotent(self):
        spec = importlib.util.spec_from_file_location("response_patches", ROOT / "scripts/apply_response_guarantees.py")
        patches = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(patches)
        upstream = subprocess.check_output(
            ["git", "-C", str(ROOT / ".runtime/MaiBot"), "show", "HEAD:" + REPLY_PATH.as_posix()]
        ).decode("utf-8-sig")
        current = (ROOT / ".runtime/MaiBot" / REPLY_PATH).read_text(encoding="utf-8-sig")
        cleaned, _ = remove_repeated_reply_setup(current)
        for source in (upstream, cleaned):
            with self.subTest(source="upstream" if source == upstream else "local"), tempfile.TemporaryDirectory() as tmp:
                patches.MAIBOT = Path(tmp)
                target = Path(tmp) / REPLY_PATH
                target.parent.mkdir(parents=True)
                target.write_text(source, encoding="utf-8")
                for iteration in range(3):
                    patches.patch_reply_tool()
                    patches.patch_contextual_auto_emoji()
                    patches.patch_named_friend_example_at_request()
                    result = target.read_text(encoding="utf-8")
                    ast.parse(result)
                    self.assertEqual(result.count(MARKER), 1)
                    self.assertEqual(result.count("    force_at_friend_example_only = rich_reply_enabled"), 1)
                    if iteration:
                        self.assertEqual(result, previous)
                    previous = result
                if source == cleaned:
                    self.assertEqual(result, cleaned)

    def test_cleanup_preserves_other_changes_and_rejects_nonidentical_blocks(self):
        block = MARKER + "    value = 1\n\n"
        source = "# teammate change\n" + block * 3 + ANCHOR + "rest"
        cleaned, removed = remove_repeated_reply_setup(source)
        self.assertEqual(removed, 2)
        self.assertEqual(cleaned, "# teammate change\n" + block + ANCHOR + "rest")
        with self.assertRaises(ValueError):
            remove_repeated_reply_setup(MARKER + "x\n" + MARKER + "y\n" + ANCHOR)


if __name__ == "__main__":
    unittest.main()
