# -*- coding: utf-8 -*-
"""版本同步测试：metadata、页面构建号、CHANGELOG 必须一致。"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestVersionSync(unittest.TestCase):
    def test_metadata_page_changelog_match(self):
        meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        version = re.search(r"^version:\s*(\S+)", meta, re.M).group(1)

        page = (ROOT / "pages" / "yh" / "index.html").read_text(encoding="utf-8")
        build = re.search(r'PAGE_BUILD = "v([0-9.]+)"', page)
        self.assertIsNotNone(build, "页面缺少 PAGE_BUILD")
        self.assertEqual(version, build.group(1),
                         "metadata.yaml 与页面 PAGE_BUILD 不一致")

        log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## v{version}", log, "CHANGELOG 缺少当前版本条目")


if __name__ == "__main__":
    unittest.main()
