import unittest

from news_quality import (
    classify, is_safe_title, is_similar_story, normalized_title,
    select_diverse, source_allowed, topic, valid_news_url,
)


class QualityTests(unittest.TestCase):
    def test_similar_headlines(self):
        self.assertTrue(is_similar_story(
            "Украина и Россия обсудили новые условия переговоров",
            "Россия и Украина обсудили новые условия переговоров",
        ))
        self.assertFalse(is_similar_story(
            "В городе А сообщили о 2 поврежденных домах",
            "В городе А сообщили о 5 поврежденных домах",
        ))

    def test_urls(self):
        self.assertTrue(valid_news_url("https://news.google.com/rss/articles/example"))
        self.assertFalse(valid_news_url("http://example.com"))
        self.assertFalse(valid_news_url("https://user:pass@example.com"))
        self.assertFalse(valid_news_url("file:///etc/passwd"))

    def test_sources(self):
        self.assertTrue(source_allowed("Reuters", ["Reuters", "AP"]))
        self.assertTrue(source_allowed("BBC News", ["BBC"]))
        self.assertFalse(source_allowed("Snapchat", ["AP"]))
        self.assertFalse(source_allowed("Fake Reuters Clone", ["Reuters"]))

    def test_sensitive_headlines(self):
        self.assertFalse(is_safe_title("Координаты цели 50.12345, 30.12345"))
        self.assertFalse(is_safe_title("Troop positions reported near city"))
        self.assertTrue(is_safe_title("Сообщили о переговорах по гуманитарным вопросам"))

    def test_labels(self):
        self.assertIn("Неподтверждённое", classify("Неподтверждённые сообщения"))
        self.assertIn("Заявление", classify("Министр заявил о встрече"))
        self.assertEqual(topic("Новые мирные переговоры"), "Дипломатия")

    def test_diversity(self):
        data = [
            {"source": "Reuters", "topic": "События", "published": 3},
            {"source": "Reuters", "topic": "События", "published": 2},
            {"source": "BBC", "topic": "Дипломатия", "published": 1},
        ]
        chosen = select_diverse(data, 2)
        self.assertEqual({x["source"] for x in chosen}, {"Reuters", "BBC"})


if __name__ == "__main__":
    unittest.main()
