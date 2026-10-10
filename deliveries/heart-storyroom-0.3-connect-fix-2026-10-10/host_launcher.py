"""本机跑团主机启动器：复用平台，管理自己的临时通道，不启用模型或QQ。"""
from datetime import datetime
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
import ctypes
import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from start_local import check_existing


ROOT = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
URL_PATTERN = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com\b')


def read_health(address, timeout=2):
    # 本机/外部探测均直连，避免环境代理把localhost送往外部。
    with build_opener(ProxyHandler({})).open(address + '/health', timeout=timeout) as response:
        return json.loads(response.read(16384))


def extract_url(text):
    for match in URL_PATTERN.finditer(text):
        if match.group() != 'https://api.trycloudflare.com':
            return match.group()
    return None


class Connection:
    def __init__(self, emit, root=ROOT):
        self.root, self.emit = root, emit
        self.stop = threading.Event()
        self.platform = self.tunnel = None
        self.log_handle = None
        self.reader = None
        self.url = None
        self.directory = root / '.runtime/主机连接'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state = self.directory / '当前连接.json'

    def log(self, name, **fields):
        with (self.directory / '启动记录.jsonl').open('a', encoding='utf-8') as file:
            file.write(json.dumps({'time': datetime.now().isoformat(timespec='seconds'),
                                  'feature': name, **fields}, ensure_ascii=False) + '\n')

    def status(self, message):
        self.emit('status', message)
        self.log('主机启动状态', message=message)

    def write_state(self, status):
        content = {'status': status, 'url': self.url if status == 'ready' else None,
                   'launcher_pid': os.getpid(), 'time': datetime.now().isoformat(timespec='seconds')}
        temporary = self.state.with_suffix('.tmp')
        temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.state)

    def validate(self):
        config = json.loads((self.root / 'local-config.json').read_text(encoding='utf-8-sig'))
        port = int(config.get('port', 8790))
        if not 1024 <= port <= 65535:
            raise ValueError('本机平台端口不正确')
        if not Path(config['python']).is_file():
            raise FileNotFoundError('local-config.json中的Python路径不存在')
        executable = self.root / 'tools/cloudflared.exe'
        release = json.loads((self.root / 'tools/cloudflared-install.json').read_text(encoding='utf-8'))
        if hashlib.sha256(executable.read_bytes()).hexdigest() != release['sha256']:
            raise RuntimeError('连接工具指纹不符，请重新核对官方安装记录')
        for item in json.loads((self.root / 'vendor-manifest.json').read_text(encoding='utf-8')):
            file = self.root / 'web/vendor' / item['name']
            if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != item['sha256']:
                raise RuntimeError('缺少或改动了Live2D舞台库：' + item['name'])
        vt = Path(config['vtuber_root'])
        if not (vt / 'model_dict.json').is_file() or not (vt / 'frontend/libs/live2dcubismcore.min.js').is_file():
            raise FileNotFoundError('VT模型配置或Live2D Core缺失')
        return config, executable, 'http://127.0.0.1:' + str(port)

    def start_platform(self, config, address):
        try:
            existing = read_health(address)
        except OSError:
            existing = None
        if existing is not None:
            check_existing(existing)
            self.status('复用已经运行的 StoryRoom；退出时不会停止它')
            return
        self.status('正在启动本机 StoryRoom')
        output = (self.directory / 'StoryRoom.log').open('a', encoding='utf-8')
        try:
            command = [config['python'], '-u', '-X', 'utf8', str(self.root / 'launch.py'),
                       '--port', str(config.get('port', 8790)), '--vtuber-root', config['vtuber_root']]
            if config.get('asr_url'):
                command += ['--asr-url', config['asr_url']]
            self.platform = subprocess.Popen(command, cwd=self.root, stdout=output, stderr=subprocess.STDOUT,
                                             creationflags=NO_WINDOW)
        finally:
            output.close()
        deadline = time.monotonic() + 25
        while not self.stop.is_set() and time.monotonic() < deadline:
            if self.platform.poll() is not None:
                raise RuntimeError('平台启动失败，请查看StoryRoom.log')
            try:
                check_existing(read_health(address))
                return
            except OSError:
                self.stop.wait(.3)
        raise RuntimeError('平台启动被停止或等待超过25秒')

    def start_tunnel(self, executable, address):
        self.status('本机平台就绪，正在建立 HTTPS 连接；首次可能需要稍等')
        # 显式使用自己的空配置，不覆盖或读入用户其他cloudflared隧道设置。
        tunnel_config = self.directory / '临时通道.yml'
        tunnel_config.write_text('{}\n', encoding='utf-8')
        self.log_handle = (self.directory / 'cloudflared.log').open('a', encoding='utf-8')
        self.tunnel = subprocess.Popen([str(executable), 'tunnel', '--no-autoupdate',
                                       '--config', str(tunnel_config), '--protocol', 'http2', '--url', address],
                                      cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)

        def collect():
            for line in self.tunnel.stdout:
                self.log_handle.write(line)
                self.log_handle.flush()
                candidate = extract_url(line)
                if candidate:
                    self.url = candidate

        self.reader = threading.Thread(target=collect, daemon=True)
        self.reader.start()
        deadline = time.monotonic() + 90
        while not self.stop.is_set() and time.monotonic() < deadline:
            if self.tunnel.poll() is not None:
                raise RuntimeError('网络通道退出，请查看cloudflared.log；本机无需重装')
            if self.url:
                try:
                    check_existing(read_health(self.url, timeout=3))
                    return
                except (OSError, ValueError):
                    pass  # URL公布不等于链路已建立，继续等待实际健康响应。
            self.stop.wait(.6)
        raise RuntimeError('异地连接被停止或90秒内未能验证；请查看网络日志')

    def run(self):
        try:
            self.write_state('starting')
            config, executable, address = self.validate()
            self.start_platform(config, address)
            if self.stop.is_set():
                return
            self.start_tunnel(executable, address)
            if self.stop.is_set():
                return
            self.write_state('ready')
            self.status('连接已验证。把下方地址交给伙伴；两个 PC 插件使用各自密钥')
            self.emit('ready', self.url)
            while not self.stop.wait(2):
                if self.tunnel.poll() is not None or (self.platform and self.platform.poll() is not None):
                    raise RuntimeError('本次启动的服务已退出，原地址不可继续使用')
        except Exception as error:
            if not self.stop.is_set():
                self.log('连接失败', error=str(error))
                self.emit('error', str(error))
        finally:
            self.cleanup()
            self.emit('stopped', None)

    def cleanup(self):
        # 只拥有Popen返回的两个子进程；不扫描、不按名称结束用户程序。
        for process in (self.tunnel, self.platform):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        if self.reader:
            self.reader.join(timeout=2)
        if self.log_handle:
            self.log_handle.close()
        if self.tunnel and self.tunnel.stdout:
            self.tunnel.stdout.close()
        self.write_state('stopped')
        self.log('主机连接停止', reused_platform_stopped=False, maibot_started=False, model_called=False)


class Window:
    def __init__(self, root):
        self.root = root
        root.title('一键开启跑团连接 · StoryRoom')
        root.geometry('760x410')
        root.minsize(660, 380)
        self.events = queue.Queue()
        self.connection = self.worker = None
        self.closing = False
        self.address = tk.StringVar(value='正在准备连接…')
        self.status = tk.StringVar(value='检查环境中')
        frame = ttk.Frame(root, padding=22)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='这台电脑是跑团主机', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='共享房间与两个形象；双方 MaiBot 仍在各自电脑运行。').pack(anchor='w', pady=(8, 18))
        ttk.Label(frame, textvariable=self.status, wraplength=690).pack(anchor='w', pady=8)
        self.entry = ttk.Entry(frame, textvariable=self.address, state='readonly', font=('Consolas', 12))
        self.entry.pack(fill='x', pady=12)
        buttons = ttk.Frame(frame)
        buttons.pack(anchor='w', pady=8)
        self.copy = ttk.Button(buttons, text='复制给伙伴的地址', command=self.copy_url, state='disabled')
        self.copy.pack(side='left', padx=(0, 8))
        self.open = ttk.Button(buttons, text='打开共享房间', command=self.open_url, state='disabled')
        self.open.pack(side='left', padx=8)
        self.restart = ttk.Button(buttons, text='重新连接', command=self.start, state='disabled')
        self.restart.pack(side='left', padx=8)
        ttk.Button(buttons, text='查看连接日志', command=lambda: os.startfile(str(ROOT / '.runtime/主机连接'))).pack(side='left', padx=8)
        ttk.Label(frame, text='地址每次重连可能变化，双方插件需更新地址。\n关闭本窗口会停止它启动的通道和平台；已在运行的平台保留。\n本入口不启动 MaiBot、不填密钥、不启用付费模型。', wraplength=690).pack(anchor='w', pady=18)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.pump)
        self.start()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.restart.configure(state='disabled')
        self.address.set('正在准备连接…')
        self.connection = Connection(lambda *event: self.events.put(event))
        self.worker = threading.Thread(target=self.connection.run, daemon=True)
        self.worker.start()

    def copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.address.get())

    def open_url(self):
        webbrowser.open(self.address.get())

    def close(self):
        self.closing = True
        self.status.set('正在停止本次启动的连接…')
        self.copy.configure(state='disabled')
        self.open.configure(state='disabled')
        self.connection.stop.set()

    def pump(self):
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == 'status' and not self.closing:
                self.status.set(value)
            elif kind == 'ready' and not self.closing:
                self.address.set(value)
                self.copy.configure(state='normal')
                self.open.configure(state='normal')
                webbrowser.open(value)
            elif kind == 'error':
                self.status.set('连接未成功：' + value)
            elif kind == 'stopped':
                self.address.set('连接已停止')
                self.copy.configure(state='disabled')
                self.open.configure(state='disabled')
        if self.worker and not self.worker.is_alive():
            if self.closing:
                self.root.destroy()
                return
            self.restart.configure(state='normal')
        self.root.after(100, self.pump)


def main():
    root = tk.Tk()
    mutex = None
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        name = 'Local\\StoryRoomHost-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:20]
        mutex = kernel.CreateMutexW(None, False, name)
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel.CloseHandle(mutex)
            root.withdraw()
            messagebox.showinfo('已在运行', '跑团连接窗口已经打开，请使用已有窗口。不会重复启动平台或通道。')
            root.destroy()
            return
    try:
        Window(root)
        root.mainloop()
    finally:
        if mutex:
            kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
