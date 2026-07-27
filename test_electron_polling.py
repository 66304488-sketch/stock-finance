import unittest
from pathlib import Path


MAIN_TS = Path(__file__).with_name("electron") / "main.ts"


class ElectronPollingContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_TS.read_text(encoding="utf-8")

    def test_final_status_is_checked_before_elapsed_timeout(self):
        function = self.source.split("function pollRefreshStatus", 1)[1].split("async function runDataPipeline", 1)[0]
        self.assertLess(function.index("await fetchRefreshStatus()"), function.index("Date.now() - start > maxMs"))
        self.assertIn("pollTimedOut: true", function)

    def test_polling_does_not_send_a_notification_every_cycle(self):
        function = self.source.split("function pollRefreshStatus", 1)[1].split("async function runDataPipeline", 1)[0]
        self.assertNotIn('notify("数据更新", status.current_step', function)
        self.assertIn("setTimeout(check, 2000)", function)

    def test_background_timeout_is_not_a_failure_dialog(self):
        function = self.source.split("async function runDataPipeline", 1)[1].split("function notify", 1)[0]
        self.assertIn("else if (status.pollTimedOut)", function)
        self.assertIn("if (interactive) dialog.showErrorBox", function)

    def test_scheduled_failures_propagate_to_scheduler_retry(self):
        function = self.source.split("async function runDataPipeline", 1)[1].split("function notify", 1)[0]
        self.assertIn("if (!interactive) throw new Error(message)", function)
        self.assertIn("else throw new Error(err)", function)


if __name__ == "__main__":
    unittest.main()
