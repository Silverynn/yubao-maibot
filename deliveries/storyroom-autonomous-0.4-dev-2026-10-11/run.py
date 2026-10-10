"""Start an isolated, single-process room host; model workers begin paused."""
import argparse
import json
import secrets
from pathlib import Path
import uvicorn
from storyroom.app import create_app


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8791)
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).parent/'.runtime')
    parser.add_argument('--vtuber-root',type=Path)
    parser.add_argument('--model-config',type=Path)
    parser.add_argument('--asr-url')
    args=parser.parse_args()
    if not 1024<=args.port<=65535:raise ValueError('端口必须在1024～65535之间')
    args.data_dir.mkdir(parents=True,exist_ok=True)
    credential=args.data_dir/'主持人登录密钥.txt'
    if not credential.exists():credential.write_text(secrets.token_urlsafe(32),encoding='utf-8')
    config=json.loads(args.model_config.read_text(encoding='utf-8')) if args.model_config else {}
    for section in ('gm','images'):
        if config.get(section):
            from urllib.parse import urlsplit
            value=urlsplit(config[section]['base_url'])
            if value.scheme!='https' or not value.hostname or value.username or value.password or value.query or value.fragment:
                raise ValueError('模型服务地址必须为纯HTTPS地址')
    app=create_app(args.data_dir,credential.read_text(encoding='utf-8').strip(),args.vtuber_root,args.asr_url,config.get('gm'),config.get('images'))
    print('新版独立房间：http://127.0.0.1:'+str(args.port))
    print('主持人登录文件：'+str(credential.resolve()))
    print('游戏默认暂停。配置模型不等于开始调用；先接管，再恢复可人工主持。')
    uvicorn.run(app,host='127.0.0.1',port=args.port,access_log=False)


if __name__=='__main__':main()
