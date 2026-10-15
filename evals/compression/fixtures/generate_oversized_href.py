"""Generate synthetic fixtures for R05; no production history or live website needed."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prefix = 'data:application/octet-stream;base64,'
    href = prefix + 'A' * (3_204_845 - len(prefix))
    text = '杭州3日游：10月20日西湖，10月21日灵隐，10月22日博物馆。仅制作本地文件。'
    page = '<!doctype html><meta charset="utf-8"><title>杭州行程测试</title><p>' + text + '</p><a download="fixture.bin" href="' + href + '">下载图片</a>'
    (args.output_dir / 'oversized-href.html').write_text(page, encoding='utf-8')
    output = json.dumps({'url': 'http://127.0.0.1:8765/oversized-href.html', 'title': '杭州行程测试', 'text': text, 'elements': [{'ref': 0, 'tag': 'a', 'text': '下载图片', 'context': '下载图片', 'href': href}]}, ensure_ascii=False)
    history = [
        {'role': 'user', 'content': '帮我创建杭州3日游路书，10月20日开始，游玩西湖、灵隐、博物馆；不要发布。'},
        {'type': 'function_call', 'name': 'browser_action', 'call_id': 'fixture_browser_open', 'arguments': '{"action":"open","url":"http://127.0.0.1:8765/oversized-href.html"}'},
        {'type': 'function_call_output', 'call_id': 'fixture_browser_open', 'output': output},
        {'role': 'assistant', 'content': '模型调用累计失败达到3次，本轮任务已结束。已保存的修改保留。'},
        {'role': 'user', 'content': '先做个TXT文件'},
    ]
    (args.output_dir / 'legacy-history.json').write_text(json.dumps(history, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'href_characters': len(href), 'history_items': len(history), 'output_dir': str(args.output_dir)}))


if __name__ == '__main__':
    main()
