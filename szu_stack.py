"""Local SZU crawler/RAGFlow deployment, using the D-drive Docker install."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import httpx

ROOT = Path(__file__).resolve().parent
DOCKER = r'D:\Apps\Docker\DockerDesktop\resources\bin\docker.exe'
COMPOSE = r'D:\Apps\Docker\DockerDesktop\resources\cli-plugins\docker-compose.exe'


def docker_json(*args):
    result = subprocess.run([DOCKER, *args], capture_output=True, text=True, encoding='utf-8')
    return json.loads(result.stdout) if result.returncode == 0 else None


def container_env(name):
    inspected = docker_json('inspect', name)
    return dict(item.split('=', 1) for item in inspected[0]['Config']['Env'] if '=' in item) if inspected else {}


def stack_env():
    env = os.environ.copy()
    env['COMPOSE_PARALLEL_LIMIT'] = '1'
    env['CRAWL4AI_API_TOKEN'] = container_env('szu-crawl4ai').get('CRAWL4AI_API_TOKEN') or secrets.token_hex(32)
    env['SZU_STACK_PASSWORD'] = container_env('szu-knowledge-mysql-1').get('MYSQL_ROOT_PASSWORD') or secrets.token_hex(32)
    inspected = docker_json('inspect', 'szu-crawl4ai')
    entries = inspected[0]['HostConfig'].get('ExtraHosts') or [] if inspected else []
    pinned = next((value.split(':', 1)[1] for value in entries if value.startswith('jwb.szu.edu.cn:')), None)
    if not pinned:
        response = httpx.get('https://dns.alidns.com/resolve', params={'name': 'jwb.szu.edu.cn', 'type': 'A'}, timeout=20)
        response.raise_for_status()
        answers = [item['data'] for item in response.json().get('Answer', []) if item['type'] == 1]
        if not answers or any(not ipaddress.ip_address(ip).is_global for ip in answers):
            raise RuntimeError('Official site DNS did not return public addresses')
        pinned = answers[0]
    if not ipaddress.ip_address(pinned).is_global:
        raise RuntimeError('Refusing a nonpublic pinned IP')
    env['SZU_JWB_IP'] = pinned
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['crawler', 'ragflow', 'status', 'stop', 'bootstrap-account'])
    parser.add_argument('--new-account-password', action='store_true', help='Regenerate local credentials only BEFORE first registration; does not reset a registered account')
    args = parser.parse_args()
    if args.action == 'bootstrap-account':
        target = ROOT / 'data' / 'szu-ragflow-account.json'
        target.parent.mkdir(exist_ok=True)
        if not target.exists() or args.new_account_password:
            target.write_text(json.dumps({'email': 'szu-pilot@example.invalid', 'nickname': 'SZU Pilot',
                                          'password': 'Aa1!' + secrets.token_urlsafe(24)}, indent=2), encoding='utf-8')
        print(f'Local account settings: {target}')
        return
    env = stack_env()
    command = [COMPOSE, '-f', str(ROOT / 'szu-stack.compose.yml')]
    if args.action in ('ragflow', 'status', 'stop'):
        command += ['--profile', 'ragflow']
    if args.action == 'crawler':
        command += ['up', '-d', 'crawl4ai']
    elif args.action == 'ragflow':
        # Set only the Docker VM kernel parameter required by Elasticsearch.
        subprocess.run(['wsl.exe', '-d', 'docker-desktop', '-u', 'root', '--', 'sysctl', '-w', 'vm.max_map_count=262144'], check=True)
        command += ['up', '-d', '--pull', 'missing', 'ragflow']
    elif args.action == 'status':
        command += ['ps']
    else:
        command += ['stop']
    subprocess.run(command, env=env, cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
