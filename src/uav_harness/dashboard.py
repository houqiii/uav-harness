"""Serve hardware, protocol simulation, or hardware-free LLM planning in the same UI."""
import argparse
import errno
import socket
from pathlib import Path
import tempfile
import uvicorn
from .api import create_app
from .bench_console import NoSerialPort, settings_for
from .config import Settings
from .errors import HarnessError


def offline_settings(state_dir):
    # Capability profiles only: create_app(offline=True) never opens these links.
    profiles = [('ap-1', 'ardupilot', 'flight', 1, [4, 5]),
                ('px4-1', 'px4', 'flight', 2, [1, 16]),
                ('model-1', 'ardupilot', 'model_bench', 3, [4, 5])]
    return Settings.model_validate({
        'allow_control': False, 'state_dir': str(state_dir),
        'links': [{'name': vid, 'kind': 'simulator'} for vid, *_ in profiles],
        'vehicles': [{'vehicle_id': vid, 'link': vid, 'backend': backend,
                      'profile': profile, 'system_id': system, 'firmware': firmware,
                      'max_height_m': 2 if profile == 'model_bench' else 20}
                     for vid, backend, profile, system, firmware in profiles],
    })


def run_server(app, *, host, port):
    # Reserve the port before application startup opens links or restores sessions.
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    try:
        listener = socket.create_server((host, port), family=family)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise HarnessError(
                f'{host}:{port} 已被占用。请在原服务终端按 Ctrl+C 并等待退出，'
                f'或用 --http-port {port + 1 if port < 65535 else 8080} 启动另一端口。'
                f'查看占用进程：lsof -nP -iTCP:{port} -sTCP:LISTEN'
            ) from None
        raise
    with listener:
        config = uvicorn.Config(app, host=host, port=port, workers=1)
        uvicorn.Server(config).run(sockets=[listener])


def main():
    parser = argparse.ArgumentParser(description='UAV Harness 网页控制台')
    parser.add_argument('--config', type=Path, help='已有机群配置；可与 --offline 合用做能力推演')
    parser.add_argument('--port', help='USB 串口；未检测到 USB 时默认进入离线推演')
    parser.add_argument('--baud', type=int, default=57600)
    parser.add_argument('--system-id', type=int, default=1)
    parser.add_argument('--component-id', type=int, default=1)
    parser.add_argument('--board-id', type=int, default=1010)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--mock', action='store_true', help='启动协议模拟对端，无实机连接')
    mode.add_argument('--offline', action='store_true', help='不打开实机链路；与 --no-sandbox 合用时使用几何测试环境')
    parser.add_argument('--sandbox', action=argparse.BooleanOptionalAction, default=True,
                        help='默认启用 Drone4D 沙盒与实机双页面；--no-sandbox 关闭在线沙盒')
    parser.add_argument('--observe', action='store_true', help='只读观察，禁止控制')
    parser.add_argument('--http-port', type=int, default=None)
    parser.add_argument('--state-dir', type=Path, default=Path('state/bench-console'))
    args = parser.parse_args()
    if args.http_port is not None and not 1024 <= args.http_port <= 65535:
        parser.error('--http-port 必须在 1024..65535 之间')
    if args.config and (args.mock or args.port or args.baud != 57600 or args.system_id != 1 or args.component_id != 1 or args.board_id != 1010 or args.state_dir != Path('state/bench-console')):
        parser.error('--config 不能与模拟、串口、身份或状态目录选项合用')
    if args.offline and args.port:
        parser.error('--offline 不与 --port 合用；已有硬件配置用 --offline --config')

    def serve(state_dir):
        offline = args.offline or (args.sandbox and not args.config and not args.port and not args.mock)
        if args.config:
            settings = Settings.load(args.config)
        elif offline:
            settings = offline_settings(state_dir)
        else:
            try:
                settings = settings_for(args, state_dir)
            except NoSerialPort:
                print('未检测到 USB 串口，自动进入离线推演。', flush=True)
                settings, offline = offline_settings(state_dir), True
        settings.drone4d.enabled = args.sandbox
        if args.observe or offline:
            settings.allow_control = False
        if args.http_port is not None:
            settings.api_port = args.http_port
        kinds = {link.kind for link in settings.links}
        print('Drone4D 在线沙盒；实机控制接口待连接。' if args.sandbox and offline else '无硬件模式：Qwen VLM 在线规划＋几何相机／运动学仿真；不打开飞控链路，不发送 MAVLink 指令。' if offline else
              '协议模拟：所有响应均不代表实机效果。' if kinds == {'simulator'} else
              '实机链路：网页点击执行后才派发控制动作。', flush=True)
        print(f'网页控制台：http://{settings.api_host}:{settings.api_port}' + ('/#sandbox' if args.sandbox else '/#hardware'), flush=True)
        print('关闭服务请按 Ctrl+C；等待已有任务收尾。', flush=True)
        run_server(create_app(settings, offline=offline), host=settings.api_host, port=settings.api_port)

    try:
        if args.mock:
            with tempfile.TemporaryDirectory(prefix='uav-dashboard-mock-') as folder:
                serve(folder)
        else:
            serve(args.state_dir)
    except KeyboardInterrupt:
        print("服务已退出，HTTP 端口已释放。", flush=True)
    except (HarnessError, OSError, ValueError) as exc:
        parser.exit(1, f'[ERROR] {exc}\n')


if __name__ == '__main__':
    main()
