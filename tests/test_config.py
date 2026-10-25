import unittest

from config import ConfigError, load_config

TOKEN = "123456789:AAH" + "x" * 32


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = load_config({"BOT_TOKEN": TOKEN})
        self.assertEqual(cfg.bot_token, TOKEN)
        self.assertEqual(cfg.progress_every, 25)
        self.assertEqual(cfg.message_delay, 1.0)

    def test_missing_token(self):
        with self.assertRaises(ConfigError):
            load_config({})

    def test_placeholder_token_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({"BOT_TOKEN": "YOUR_TELEGRAM_BOT_TOKEN"})

    def test_quoted_token_is_cleaned(self):
        self.assertEqual(load_config({"BOT_TOKEN": f'"{TOKEN}"'}).bot_token, TOKEN)

    def test_overrides(self):
        cfg = load_config({"BOT_TOKEN": TOKEN, "PROGRESS_EVERY": "10", "MESSAGE_DELAY_SECONDS": "0.5", "LOG_LEVEL": "debug"})
        self.assertEqual((cfg.progress_every, cfg.message_delay, cfg.log_level), (10, 0.5, "DEBUG"))

    def test_bad_numbers(self):
        for key, value in [("PROGRESS_EVERY", "abc"), ("MESSAGE_DELAY_SECONDS", "-1"), ("EXTRACT_RETRIES", "0"), ("LOG_LEVEL", "LOUD")]:
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    load_config({"BOT_TOKEN": TOKEN, key: value})


if __name__ == "__main__":
    unittest.main()
