import argparse
import json
import unittest
from unittest.mock import MagicMock, patch

import szu_pilot as pilot


class PilotTests(unittest.TestCase):
    def test_ragflow_paginates_with_supported_page_size(self):
        client = MagicMock()
        first, second = MagicMock(), MagicMock()
        first.json.return_value = {'code': 0, 'data': [{'id': str(i)} for i in range(100)]}
        second.json.return_value = {'code': 0, 'data': {'docs': [{'id': 'last'}]}}
        client.get.side_effect = [first, second]
        entries = pilot.ragflow_list(client, '/api/v1/datasets')
        self.assertEqual(len(entries), 101)
        self.assertEqual(client.get.call_args_list[0].kwargs['params']['page_size'], 100)
        self.assertEqual(client.get.call_args_list[1].kwargs['params']['page'], 2)

    def test_title_attachment_and_previous_next_cleanup(self):
        parser = pilot.AttachmentParser()
        parser.feed('<title>官方通知</title><a href="/download.jsp?id=1"><span>报名表.xlsx</span></a>')
        self.assertEqual(parser.title, '官方通知')
        self.assertEqual(parser.links, [{'href': '/download.jsp?id=1', 'text': '报名表.xlsx'}])
        result = pilot.markdown_text({'markdown': {'raw_markdown': '正文\n上一条：[其他通知](x)\n下一条：[另外通知](y)'}})
        self.assertEqual(result, '正文')

    def test_official_url_rejects_credentials_other_hosts_and_ports(self):
        base = 'https://jwb.szu.edu.cn/'
        for value in ('http://127.0.0.1/', 'https://evil.example/',
                      'https://jwb.szu.edu.cn.evil.example/',
                      'https://user:secret@jwb.szu.edu.cn/',
                      'https://jwb.szu.edu.cn:8080/', 'javascript:alert(1)'):
            self.assertIsNone(pilot.official_url(value, base), value)
        self.assertEqual(pilot.official_url('/info/1/2.htm#top', base), base + 'info/1/2.htm')

    def test_page_budget_body_filter_and_attachment_classification(self):
        seed = 'https://jwb.szu.edu.cn/'
        article = seed + 'info/1/2.htm'
        homepage = {'success': True, 'url': seed, 'markdown': 'navigation ' * 100,
                    'links': {'internal': [{'href': article}, {'href': seed + 'info/1/3.htm'},
                                          {'href': 'https://untrusted.example/'}]}}
        notice = {'success': True, 'url': seed + 'info/1/3.htm',
                  'markdown': {'raw_markdown': '正文资料。' * 50}, 'metadata': {'title': '通知'},
                  'links': {'internal': [{'href': '/system/_content/download.jsp?wbfileid=1',
                                          'text': '报名表.xlsx'}]}}
        client = MagicMock()
        client.__enter__.return_value = client
        responses = []
        for result in (homepage, notice):
            response = MagicMock()
            response.json.return_value = {'results': [result]}
            responses.append(response)
        client.post.side_effect = responses
        report_path = MagicMock()
        report_path.__str__.return_value = 'report.json'
        robots = MagicMock()
        robots.can_fetch.return_value = True
        with patch.object(pilot, 'container_env', return_value={'CRAWL4AI_API_TOKEN': 'test'}), \
             patch.object(pilot, 'container_robots', return_value=robots), \
             patch.object(pilot.httpx, 'Client', return_value=client), \
             patch.object(pilot, 'RESULT', report_path), \
             patch.object(pilot.time, 'sleep'), patch('builtins.print'):
            pilot.crawl(argparse.Namespace(url=seed, max_pages=2))
        report = json.loads(report_path.write_text.call_args.args[0])
        self.assertEqual(client.post.call_count, 2)
        self.assertEqual(report['counts']['attempted'], 2)
        self.assertEqual(report['counts']['discovery_only'], 1)
        self.assertEqual(len(report['pages']), 1)
        self.assertNotIn('navigation', report['pages'][0]['core_content'])
        self.assertEqual(report['attachments'][0]['type'], '.xlsx')
        second = client.post.call_args_list[1].kwargs['json']['crawler_config']['params']
        self.assertEqual(second['css_selector'], '.art-main, head > title')
        self.assertTrue(second['check_robots_txt'])


if __name__ == '__main__':
    unittest.main()
