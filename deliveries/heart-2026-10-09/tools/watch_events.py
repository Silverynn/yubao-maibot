"""跨目录只读观察实时事件；不启动机器人，不修改数据库。"""
from pathlib import Path
import argparse
import json
import sqlite3
import time

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--maibot-root',required=True,type=Path)
    p.add_argument('--follow',action='store_true');a=p.parse_args()
    db=(a.maibot_root/'data/heart_observation/events.sqlite3').resolve();last=None
    while True:
        if not db.is_file():print('尚无审计数据库，先完成安装并进行测试。',flush=True)
        else:
            connection=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)
            try:
                where="kind IN ('实时对话插件','语音识别','Live2D交互')"
                if last is None:
                    rows=connection.execute('SELECT seq,time,kind,session,payload FROM events WHERE '+where+' ORDER BY seq DESC LIMIT 50').fetchall()[::-1]
                else:
                    rows=connection.execute('SELECT seq,time,kind,session,payload FROM events WHERE '+where+' AND seq>? ORDER BY seq LIMIT 500',(last,)).fetchall()
                for seq,stamp,kind,session,payload in rows:
                    print(json.dumps({'time':stamp,'kind':kind,'session':session,**json.loads(payload)},ensure_ascii=False),flush=True);last=seq
            finally:connection.close()
        if not a.follow:return
        time.sleep(2)

if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:print('已停止观察；机器人不受影响。')
