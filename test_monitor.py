import datetime as dt
import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import patch

import monitor as m

OFFICE = dict(name='India', slug='india', region='Asia-Pacific', lang='en')
NOW = dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc)


class CollectorTests(unittest.TestCase):
    def item(self, title='A release with enough detail', url='https://www.unicef.org/india/press-releases/example'):
        return m.make_item(OFFICE, title, url, dt.date(2026, 9, 10), 'Press release', 'scrape')

    def test_archive_retains_items_and_first_seen_on_refresh(self):
        first = m.merge_archive([], [self.item()], NOW)
        self.assertEqual(m.merge_archive(first, [], NOW), first)
        later = m.merge_archive(first, [self.item('Updated title')], NOW + dt.timedelta(days=1))
        self.assertEqual(later[0]['first_seen'], first[0]['first_seen'])
        self.assertEqual(later[0]['title'], 'Updated title')

    def test_cross_office_copies_are_preserved(self):
        one = self.item()
        two = {**one, 'office': 'Nepal', 'slug': 'nepal'}
        self.assertEqual(len(m.merge_archive([], [one, two], NOW)), 2)

    def test_retention_uses_publication_date(self):
        old = {**self.item(), 'date': '2020-01-01'}
        self.assertEqual(m.merge_archive([], [old], NOW), [])

    def test_dates_are_multilingual_and_validated(self):
        for text in ['14 September 2026', '14 septembre 2026', '14 de septiembre de 2026', '14 de setembro de 2026', '2026-09-14', 'Mon, 14 Sep 2026 12:00:00 GMT']:
            self.assertEqual(m.parse_date(text), NOW.date())
        self.assertIsNone(m.parse_date('31 February 2026'))

    def test_separate_cards_keep_their_own_date_type_and_image(self):
        html = '''<div class="card_large"><img src="/image.jpg"><div class="card-content">
        <div class="tile--date">10 September 2026</div><div class="tile--content-category">Statement</div>
        <h3><a href="/india/press-releases/first">The first announcement headline</a></h3></div></div>
        <div class="card-content"><h3><a href="/india/press-releases/second">The second announcement headline</a></h3></div>'''
        items = m.items_from_listing(SimpleNamespace(text=html), 'https://www.unicef.org/india/press-centre', OFFICE)
        self.assertEqual(items[0]['date'], '2026-09-10')
        self.assertEqual(items[0]['type'], 'Statement')
        self.assertEqual(items[0]['image'], 'https://www.unicef.org/image.jpg')
        self.assertIsNone(items[1]['date'])

    def test_non_latin_titles_do_not_collapse(self):
        self.assertNotEqual(m.norm_title('\u4eca\u5929\u7684\u513f\u7ae5'), m.norm_title('\u660e\u5929\u7684\u513f\u7ae5'))

    def test_atom_uses_alternate_link_and_date(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Example release</title>
        <link rel="self" href="https://www.unicef.org/api/1"/>
        <link rel="alternate" href="https://www.unicef.org/india/press-releases/example"/>
        <published>2026-09-10T12:00:00Z</published></entry></feed>'''
        with patch.object(m, 'get', return_value=SimpleNamespace(content=xml)):
            items = m.items_from_feed('https://www.unicef.org/india/feed.xml', OFFICE)
        self.assertEqual(items[0]['date'], '2026-09-10')
        self.assertIn('/press-releases/', items[0]['url'])

    def test_script_embedding_and_rss_escaping(self):
        item = self.item('</script><script>alert(1)</script> & quote')
        rss = ET.fromstring(m.build_rss([item, {**item, 'office': 'Nepal'}, {**item, 'type': 'Statement'}], NOW))
        self.assertEqual(len(rss.findall('.//item')), 1)
        output = m.build_page({'items': [item]}, m.ROOT / 'template.html')
        self.assertNotIn('</script><script>alert(1)</script>', output)
        self.assertIn('\\u003c', output)

    def test_pagination_follows_multiple_pages(self):
        def response(n):
            return SimpleNamespace(text='<a rel="next" href="?page='+str(n+1)+'">Next</a>', url='https://www.unicef.org/india/press-releases?page='+str(n))
        with patch.object(m, 'get', side_effect=[response(0), response(1), response(2)]), patch.object(m, 'items_from_listing', side_effect=[[self.item(url='https://www.unicef.org/india/press-releases/'+str(n))] for n in range(3)]), patch.object(m.time, 'sleep'):
            items, _, method = m.collect(OFFICE, pages=3)
        self.assertEqual(len(items), 3)
        self.assertEqual(method, 'scrape')

    def test_url_validation(self):
        self.assertFalse(m.safe_url('javascript:alert(1)'))
        self.assertFalse(m.safe_url('https://www.unicef.org.example.com/'))
        self.assertTrue(m.safe_url('https://www.unicef.cn/en/press-releases/test'))


if __name__ == '__main__':
    unittest.main()
