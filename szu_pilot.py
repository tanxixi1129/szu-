"""Bounded official-site Crawl4AI pilot and optional RAGFlow import.

Run: python szu_pilot.py crawl --max-pages 10
Then set RAGFLOW_API_KEY and run: python szu_pilot.py import
"""
import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import io
import json
import os
import ipaddress
import re
from pathlib import Path
import time
import subprocess
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from crawler import normalize_url, USER_AGENT
from szu_stack import container_env, DOCKER

ROOT = Path(__file__).resolve().parent
RESULT = ROOT / 'data' / 'szu-pilot.json'
DOMAIN = 'jwb.szu.edu.cn'
ATTACHMENTS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx'}


class AttachmentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.active = None
        self.title = ''
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == 'title':
            self.in_title = True
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if '/download.jsp' in href or Path(urlsplit(href).path).suffix.lower() in ATTACHMENTS:
                self.active = {'href': href, 'text': ''}

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.active is not None:
            self.active['text'] += data

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if tag == 'a' and self.active is not None:
            self.active['text'] = self.active['text'].strip()
            self.links.append(self.active)
            self.active = None


def container_robots(seed):
    # Use the same pinned DNS and network path as the browser, not host Fake-IP DNS.
    code = '''import ipaddress,json,socket,sys,urllib.request,urllib.error
from urllib.parse import urlsplit
u=urlsplit(sys.argv[1]); addrs=sorted({r[4][0] for r in socket.getaddrinfo(u.hostname,None,type=socket.SOCK_STREAM)})
if not addrs or any(not ipaddress.ip_address(a).is_global for a in addrs): raise RuntimeError("nonpublic DNS")
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k): return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
try:
 r=opener.open(urllib.request.Request(u.scheme+"://"+u.netloc+"/robots.txt",headers={"User-Agent":sys.argv[2]}),timeout=20)
 body=r.read(524289)
 if len(body)>524288: raise RuntimeError("robots exceeds limit")
 print(json.dumps({"addresses":addrs,"status":r.status,"text":body.decode("utf-8","replace")}))
except urllib.error.HTTPError as e:
 print(json.dumps({"addresses":addrs,"status":e.code,"text":""}))
'''
    proc = subprocess.run([DOCKER, 'exec', 'szu-crawl4ai', 'python', '-c', code, seed, USER_AGENT],
                          capture_output=True, text=True, encoding='utf-8', timeout=45)
    if proc.returncode:
        raise RuntimeError(f'Cannot reliably get robots rules: {proc.stderr.strip()}')
    policy = json.loads(proc.stdout)
    if not policy['addresses'] or any(not ipaddress.ip_address(ip).is_global for ip in policy['addresses']):
        raise RuntimeError('Container DNS resolved to nonpublic addresses')
    status = policy['status']
    if status != 404 and not 200 <= status < 300:
        raise RuntimeError(f'Cannot reliably get robots rules: HTTP {status}')
    robots = RobotFileParser()
    robots.parse(policy['text'].splitlines() if status != 404 else [])
    return robots


def official_url(raw, base):
    try:
        url = normalize_url(urljoin(base, raw))
        if urlsplit(url).hostname != DOMAIN:
            return None
        return url
    except ValueError:
        return None


def markdown_text(result):
    value = result.get('markdown') or ''
    if isinstance(value, dict):
        value = value.get('fit_markdown') or value.get('raw_markdown') or ''
    return '\n'.join(line for line in value.splitlines() if not re.match(r'^\s*(上一条|下一条)\s*[:：]', line))


def crawl(args):
    token = container_env('szu-crawl4ai').get('CRAWL4AI_API_TOKEN')
    if not token:
        raise RuntimeError('Start the service first: python szu_stack.py crawler')
    seed = official_url(args.url, args.url)
    if not seed:
        raise ValueError('This pilot only accepts jwb.szu.edu.cn URLs')
    queue, visited, pages, failures, attachments = deque([seed]), set(), [], [], {}
    robots = container_robots(seed)
    counts = {'attempted': 0, 'successful': 0, 'discovery_only': 0, 'low_information': 0, 'robots_skipped': 0}
    started = datetime.now(timezone.utc).isoformat()
    try:
        with httpx.Client(base_url='http://127.0.0.1:11235', trust_env=False,
                          timeout=120, headers={'Authorization': f'Bearer {token}'}) as client:
            while queue and counts['attempted'] < args.max_pages:
                url = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)
                counts['attempted'] += 1
                try:
                    # Fail closed when robots rules cannot be fetched reliably.
                    if urlsplit(url).scheme != urlsplit(seed).scheme or not robots.can_fetch(USER_AGENT, url):
                        counts['robots_skipped'] += 1
                        failures.append({'url': url, 'error': 'robots denied or unavailable'})
                        continue
                    run_params = {
                        'check_robots_txt': True,
                        'exclude_external_links': True,
                        'excluded_tags': ['nav', 'footer', 'script', 'style'],
                    }
                    is_article = '/info/' in urlsplit(url).path
                    if is_article:
                        run_params['css_selector'] = '.art-main, head > title'
                    response = client.post('/crawl', json={
                        'urls': [url],
                        'crawler_config': {'type': 'CrawlerRunConfig', 'params': run_params},
                    })
                    response.raise_for_status()
                    payload = response.json()
                    results = payload.get('results') or []
                    if not results:
                        raise RuntimeError('Crawl API returned no results')
                    for result in results:
                        if not result.get('success'):
                            raise RuntimeError(str(result.get('error_message') or 'crawl failed'))
                        final_url = official_url(result.get('redirected_url') or result.get('url') or url, url)
                        if not final_url:
                            raise RuntimeError('Final URL is outside the pilot domain')
                        text = markdown_text(result).strip()
                        links = result.get('links') or {}
                        # Attachments may be outside the article body selected for Markdown.
                        attachment_parser = AttachmentParser()
                        attachment_parser.feed(result.get('html') or '')
                        for link in links.get('internal', []) + links.get('external', []) + attachment_parser.links:
                            raw = link.get('href') if isinstance(link, dict) else link
                            candidate = official_url(raw or '', final_url)
                            if not candidate:
                                continue
                            suffix = Path(urlsplit(candidate).path).suffix.lower()
                            if suffix in ATTACHMENTS or '/download.jsp' in candidate:
                                label = link.get('text', '') if isinstance(link, dict) else ''
                                label_suffix = Path(label).suffix.lower()
                                attachments[candidate] = {'url': candidate, 'source_url': final_url,
                                                         'type': label_suffix if label_suffix in ATTACHMENTS else suffix or 'download',
                                                         'title': label}
                            elif suffix in {'', '.htm', '.html', '.jsp', '.php'} and candidate not in visited:
                                if len(queue) < 2000:
                                    if '/info/' in urlsplit(candidate).path:
                                        queue.appendleft(candidate)
                                    else:
                                        queue.append(candidate)
                        if not is_article:
                            counts['discovery_only'] += 1
                            continue
                        if len(text) < 150:
                            counts['low_information'] += 1
                            continue
                        counts['successful'] += 1
                        date_match = re.search(r'(?:发布日期|发布时间)\s*[:：]\s*(\d{4}-\d{2}-\d{2})', text[:500])
                        published_at = date_match.group(1) if date_match else None
                        if published_at:
                            try:
                                datetime.strptime(published_at, '%Y-%m-%d')
                            except ValueError:
                                published_at = None
                        old_year = bool(published_at and int(published_at[:4]) < datetime.now(timezone.utc).year)
                        pages.append({'title': attachment_parser.title.strip() or (result.get('metadata') or {}).get('title') or final_url,
                                      'core_content': text, 'published_at': published_at,
                                      'department': '深圳大学教务部', 'source_url': final_url,
                                      'students': '待确认', 'campus': '待确认', 'category': '待分类',
                                      'official': True, 'verified_at': datetime.now(timezone.utc).isoformat(),
                                      'possibly_outdated': True if old_year else None,
                                      'content_hash': hashlib.sha256(text.encode()).hexdigest()})
                        print(f"OK {counts['successful']}: {final_url} ({len(text)} chars)", flush=True)
                except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                    failures.append({'url': url, 'error': str(exc)})
                    print(f'FAILED {url}: {exc}', flush=True)
                finally:
                    time.sleep(3)
    finally:
        pass
    report = {'started_at': started, 'completed_at': datetime.now(timezone.utc).isoformat(),
              'seed': seed, 'max_pages': args.max_pages, 'counts': counts,
              'remaining_discovered': len(set(queue) - visited), 'pages': pages,
              'attachments': list(attachments.values()), 'failures': failures,
              'quality_note': 'Low-information is a length heuristic, not manual quality review.'}
    # One report file per run; content from the site remains untrusted source data.
    RESULT.parent.mkdir(exist_ok=True)
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(RESULT), 'counts': counts, 'attachments': len(attachments)}, ensure_ascii=False))


def ragflow_client(browser_session=False):
    headers, cookies = {}, {}
    if browser_session:
        state = json.loads((ROOT / 'data' / 'szu-ragflow-browser.json').read_text(encoding='utf-8'))
        storage = {item['name']: item['value'] for origin in state.get('origins', [])
                   if origin['origin'] == 'http://127.0.0.1:8088' for item in origin.get('localStorage', [])}
        auth = storage.get('Authorization') or storage.get('Token')
        try:
            auth = json.loads(auth)
        except (ValueError, TypeError):
            pass
        if isinstance(auth, str):
            headers['Authorization'] = auth
        cookies = {item['name']: item['value'] for item in state.get('cookies', []) if item['domain'] == '127.0.0.1'}
    else:
        token = os.environ.get('RAGFLOW_API_KEY')
        if not token:
            raise RuntimeError('Set RAGFLOW_API_KEY from the local RAGFlow account before importing')
        headers['Authorization'] = f'Bearer {token}'
    return httpx.Client(base_url='http://127.0.0.1:9380', trust_env=False, timeout=120, headers=headers, cookies=cookies)


def ragflow_list(client, path, keywords=''):
    entries, page = [], 1
    while True:
        response = client.get(path, params={'keywords': keywords, 'page_size': 100, 'page': page})
        response.raise_for_status()
        payload = response.json()
        if payload.get('code') != 0:
            raise RuntimeError(str(payload.get('message')))
        data = payload.get('data') or []
        items = data if isinstance(data, list) else data.get('docs', data.get('documents', data.get('datasets', [])))
        entries.extend(items)
        if len(items) < 100:
            return entries
        page += 1


def import_ragflow(args):
    report = json.loads(RESULT.read_text(encoding='utf-8'))
    with ragflow_client(args.browser_session) as client:
        entries = ragflow_list(client, '/api/v1/datasets', '深圳大学知识库')
        dataset = next((item for item in entries if item.get('name') == '深圳大学知识库'), None)
        if not dataset:
            created = client.post('/api/v1/datasets', json={'name': '深圳大学知识库', 'chunk_method': 'naive'})
            created.raise_for_status()
            if created.json().get('code') != 0:
                raise RuntimeError(str(created.json().get('message')))
            dataset = created.json()['data']
        dataset_id = dataset['id']
        existing_docs = {item['name']: item for item in ragflow_list(client, f'/api/v1/datasets/{dataset_id}/documents')}
        doc_ids = []
        uploaded_count = 0
        for page in report['pages']:
            title_part = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', page['title']).strip(' .')[:60] or 'szu'
            name = f"{title_part}-{hashlib.sha256(page['source_url'].encode()).hexdigest()[:10]}-{page['content_hash'][:12]}.md"
            if name in existing_docs:
                print(f'Already uploaded: {name}')
                if args.parse:
                    doc_ids.append(existing_docs[name]['id'])
                continue
            body = (f"# {page['title']}\n\n原始 URL: {page['source_url']}\n"
                    f"发布部门: {page['department']}\n最后验证时间: {page['verified_at']}\n"
                    f"发布时间: {page.get('published_at') or '尚未确认'}\n\n{page['core_content']}\n")
            uploaded = client.post(f'/api/v1/datasets/{dataset_id}/documents',
                                   files={'file': (name, io.BytesIO(body.encode('utf-8')), 'text/markdown')})
            uploaded.raise_for_status()
            payload = uploaded.json()
            if payload.get('code') != 0:
                raise RuntimeError(str(payload.get('message')))
            doc_ids.extend(item['id'] for item in payload['data'])
            uploaded_count += len(payload['data'])
            print(f'Uploaded: {name}')
        if args.parse and doc_ids:
            parsed = client.post(f'/api/v1/datasets/{dataset_id}/chunks', json={'document_ids': doc_ids})
            parsed.raise_for_status()
            if parsed.json().get('code') != 0:
                raise RuntimeError(str(parsed.json().get('message')))
        print(f'Dataset: {dataset_id}; uploaded {uploaded_count}; parse requested: {args.parse}; documents selected: {len(doc_ids)}')


def export_markdown():
    report = json.loads(RESULT.read_text(encoding='utf-8'))
    sections = ['# 深圳大学教务部采集样本', f"采集完成时间：{report['completed_at']}",
                '来源网页为不可信参考资料；适用学生、校区和当前有效性尚待审核。']
    for page in report['pages']:
        sections.append(f"## {page['title']}\n\n原始 URL：{page['source_url']}\n\n"
                        f"发布时间：{page.get('published_at') or '未确认'}\n\n"
                        f"最后验证时间：{page['verified_at']}\n\n{page['core_content']}")
    target = RESULT.with_suffix('.md')
    target.write_text('\n\n---\n\n'.join(sections) + '\n', encoding='utf-8')
    print(f'Exported: {target}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    crawl_parser = sub.add_parser('crawl')
    crawl_parser.add_argument('--url', default='https://jwb.szu.edu.cn/')
    crawl_parser.add_argument('--max-pages', type=int, choices=range(1, 201), default=10, metavar='1-200')
    import_parser = sub.add_parser('import')
    import_parser.add_argument('--parse', action='store_true', help='Requires a configured embedding model')
    import_parser.add_argument('--browser-session', action='store_true', help='Use the saved local pilot browser session instead of an API key')
    sub.add_parser('export', help='Export the saved pilot to one readable Markdown file')
    args = parser.parse_args()
    if args.action == 'crawl':
        crawl(args)
    elif args.action == 'import':
        import_ragflow(args)
    else:
        export_markdown()


if __name__ == '__main__':
    main()
