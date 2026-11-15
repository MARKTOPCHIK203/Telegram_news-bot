"""Offline regression tests for the once-a-day safe public-news digest."""
import time
import unittest
from datetime import datetime

import daily_digest
from daily_digest import TZ, due_for_digest, digest_date, select_digest_stories, render_digest


class DailyDigestTests(unittest.TestCase):
    @staticmethod
    def ts(hour, minute=0):
        return datetime(2026, 11, 15, hour, minute, tzinfo=TZ).timestamp()

    @staticmethod
    def entry(title, source, when):
        return {
            "title": title,
            "source": {"title": source},
            "link": "https://news.google.com/rss/articles/CBMiEXAMPLE123",
            "published_parsed": time.gmtime(when),
        }

    def test_due_at_noon_local_not_utc(self):
        before_noon = self.ts(11, 59)
        noon = self.ts(12)
        self.assertFalse(due_for_digest({"posts": []}, before_noon))
        self.assertTrue(due_for_digest({"posts": []}, noon))
        self.assertFalse(due_for_digest({
            "posts": [], "daily_digest_date": digest_date(noon)
        }, noon))
        self.assertTrue(due_for_digest({"posts": []}, self.ts(16)))

    def test_select_only_allowed_high_level_public_news(self):
        now = self.ts(12, 5)
        entries = [
            self.entry("МИД России заявил о переговорах по Украине", "ТАСС", now - 60),
            self.entry("Зеленский обсудил мирные переговоры по Украине", "Reuters", now - 90),
            self.entry("США обсудили санкции в отношении России и Украины", "Интерфакс", now - 100),
            self.entry("ВС РФ продвинулись и уничтожили колонну ВСУ", "ТАСС", now - 110),
            self.entry("Россия и Украина обсудили условия переговоров", "Медуза", now - 80),
            self.entry("Стороны обсудили перемирие на Украине", "Украинская правда", now - 90),
            self.entry("Ukraine Russia peace talks latest", "Reuters", now - 100),
            self.entry("Переговоры по Украине перенесены", "ТАСС", now - 3 * 86400),
        ]
        items = select_digest_stories(entries, ["ТАСС", "Reuters", "Интерфакс"], now)
        self.assertEqual(len(items), 3)
        self.assertEqual({i["source"] for i in items}, {"ТАСС", "Reuters", "Интерфакс"})
        text = render_digest(items, now)
        self.assertIn("Российская сторона", text)
        self.assertIn("Украинская сторона", text)
        self.assertIn("нельзя надёжно определить", text)
        self.assertNotIn("Медуза", text)
        self.assertNotIn("продвинулись", text)

    def test_broad_official_briefings_both_sides_without_operational_details(self):
        now = self.ts(12, 5)
        entries = [
            self.entry("Минобороны РФ представило общую оценку обстановки на фронте", "ТАСС", now - 20),
            self.entry("Генштаб ВСУ сообщил об оценке военной обстановки", "Reuters", now - 30),
            self.entry("Минобороны РФ заявило о продвижении войск", "РИА Новости", now - 40),
            self.entry("Генштаб ВСУ сообщил об уничтожении танков", "Reuters", now - 50),
        ]
        items = select_digest_stories(entries, ["ТАСС", "Reuters", "РИА Новости"], now)
        self.assertEqual(len(items), 2)
        self.assertEqual({item["side"] for item in items},
                         {"Российская сторона", "Украинская сторона"})
        self.assertTrue(all(item["category"] == "Публичные заявления о военной обстановке"
                            for item in items))
        rendered = render_digest(items, now)
        self.assertIn("по сообщению ТАСС", rendered)
        self.assertIn("по сообщению Reuters", rendered)
        self.assertIn("не гарантирует достоверности", rendered)
        self.assertNotIn("продвижении", rendered)
        self.assertNotIn("уничтожении", rendered)

    def test_no_sources_no_invented_report(self):
        self.assertIsNone(render_digest([]))

    def test_html_is_escaped(self):
        now = self.ts(12)
        title = "Переговоры России и Украины <состоятся> & обсуждаются"
        item = {
            "title": title, "source": "РБК", "link": "https://www.rbc.ru/news?q=x&z=2",
            "published": now, "category": "Дипломатия", "side": "",
        }
        result = render_digest([item], now)
        self.assertIn("&lt;состоятся&gt;", result)
        self.assertIn("&amp;z=2", result)


if __name__ == "__main__":
    unittest.main()
