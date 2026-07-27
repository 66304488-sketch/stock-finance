import unittest
from pathlib import Path


ROOT = Path(__file__).parent


class InstallationContractTest(unittest.TestCase):
    def test_desktop_selects_an_interpreter_with_dependencies(self):
        source = (ROOT / "electron" / "python-manager.ts").read_text(encoding="utf-8")
        finder = source.split("private findPython()", 1)[1].split("/** 启动 FastAPI", 1)[0]
        self.assertIn('dependencyCheck = "import fastapi, uvicorn, akshare', finder)
        self.assertIn(
            "stock_share_changes_cninfo import py_mini_racer", finder)
        self.assertIn('execFileSync(cmd, ["-c", dependencyCheck]', finder)
        self.assertIn("firstExecutable ||", finder)
        self.assertIn("安装运行依赖.command", source)

    def test_installer_propagates_core_dependency_failures(self):
        source = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("set -o pipefail", source)
        self.assertIn("/opt/anaconda3/bin/python3", source)
        self.assertIn("akshare mini-racer pandas", source)
        self.assertIn(
            "stock_share_changes_cninfo import py_mini_racer", source)
        self.assertIn('if ! "$PYTHON" -m pip install', source)
        self.assertIn("安装完成后依赖校验仍未通过", source)
        self.assertNotIn("2>&1 | tail", source)

    def test_crowding_runtime_modules_are_packaged(self):
        source = (ROOT / "electron-builder.yml").read_text(encoding="utf-8")
        self.assertIn('from: "crowding.py"', source)
        self.assertIn('from: "turnover_momentum.py"', source)
        self.assertIn('from: "crowding_external.py"', source)
        self.assertIn('from: "market_cap_structure.py"', source)
        self.assertIn('from: "share_history_cninfo.py"', source)
        self.assertIn('from: "index_constituents.py"', source)
        self.assertIn('from: "index_futures.py"', source)

    def test_arm64_electron_signing_preserves_v8_jit_entitlements(self):
        config = (ROOT / "electron-builder.yml").read_text(encoding="utf-8")
        entitlements = (
            ROOT / "build" / "entitlements.mac.plist"
        ).read_text(encoding="utf-8")
        self.assertIn("hardenedRuntime: true", config)
        self.assertIn(
            "entitlements: build/entitlements.mac.plist", config)
        self.assertIn(
            "entitlementsInherit: build/entitlements.mac.plist", config)
        for key in (
            "com.apple.security.cs.allow-jit",
            "com.apple.security.cs.allow-unsigned-executable-memory",
            "com.apple.security.cs.disable-library-validation",
        ):
            self.assertIn(key, entitlements)


if __name__ == "__main__":
    unittest.main()
