import runpy
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CallbackTests(unittest.TestCase):
    def test_language_fix_preserves_other_query_bytes(self):
        url = "https://vpn-b.example.org/passport/v1/public/casLogin?lang=&ticket=a%2Fb%20c&return=%2Ftest"
        with patch("sys.argv", ["xdg-open", url]), \
             patch.dict("os.environ", {"AI_LOGIN_URL": "https://vpn-b.example.org"}), \
             patch("os.execvp") as execute:
            runpy.run_path(str(ROOT / "container/xdg-open"))
        self.assertEqual(execute.call_args.args[1][-1], url.replace("lang=&", "lang=zh-CN&"))

    def test_unrelated_gateway_is_not_rewritten(self):
        url = "https://another.example.org/passport/v1/public/casLogin?lang="
        with patch("sys.argv", ["xdg-open", url]), \
             patch.dict("os.environ", {"AI_LOGIN_URL": "https://vpn-b.example.org"}), \
             patch("os.execvp") as execute:
            runpy.run_path(str(ROOT / "container/xdg-open"))
        self.assertEqual(execute.call_args.args[1][-1], url)

    def test_client_protocol_uses_browserstart_argument(self):
        with patch("sys.argv", ["xdg-open", "atrust://browserstart"]), patch("os.execv") as execute:
            runpy.run_path(str(ROOT / "container/xdg-open"))
        self.assertEqual(execute.call_args.args[1][-1], "browserstart")


if __name__ == "__main__":
    unittest.main()
