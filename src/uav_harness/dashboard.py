"""Serve the packaged web console over the same model-bench transport as the CLI."""
import argparse
from pathlib import Path
import tempfile
import uvicorn
from .api import create_app
from .bench_console import settings_for
from .config import Settings
from .errors import HarnessError


def main():
    parser = argparse.ArgumentParser(description='UAV Harness 网页控制台')
    parser.add_argument('--config', type=Path, help='已有机群配置；不与串口/模拟/身份选项合用')
    parser.add_argument('--port', help='USB 串口；省略时自动查找唯一设备')
    parser.add_argument('--baud', type=int, default=57600)
    parser.add_argument('--system-id', type=int, default=1)
    parser.add_argument('--component-id', type=int, default=1)
    parser.add_argument('--board-id', type=int, default=1010)
    parser.add_argument('--mock', action='store_true', help='协议模拟，无实机连接')
    parser.add_argument('--observe', action='store_true', help='只读观察，禁止控制')
    parser.add_argument('--http-port', type=int, default=None)
    parser.add_argument('--state-dir', type=Path, default=Path('state/bench-console'))
    args = parser.parse_args()
    if args.http_port is not None and not 1024 <= args.http_port <= 65535:
        parser.error('--http-port 必须在 1024..65535 之间')
    if args.config and (args.mock or args.port or args.baud != 57600 or args.system_id != 1 or args.component_id != 1 or args.board_id != 1010 or args.state_dir != Path('state/bench-console')):
        parser.error('--config 不能与模拟、串口、身份或状态目录选项合用')

    def serve(state_dir):
        settings = Settings.load(args.config) if args.config else settings_for(args, state_dir)
        if args.observe:
            settings.allow_control = False
        if args.http_port is not None:
            settings.api_port = args.http_port
        kinds = {link.kind for link in settings.links}
        print('协议模拟：所有响应均不代表实机效果。' if kinds == {'simulator'} else '实机链路：网页点击执行后才派发控制动作。', flush=True)
        print(f'网页控制台：http://{settings.api_host}:{settings.api_port}', flush=True)
        print('关闭服务请按 Ctrl+C；等待已有任务收尾。', flush=True)
        uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port, workers=1)

    try:
        if args.mock:
            with tempfile.TemporaryDirectory(prefix='uav-dashboard-mock-') as folder:
                serve(folder)
        else:
            serve(args.state_dir)
    except (HarnessError, OSError, ValueError) as exc:
        parser.exit(1, f'[ERROR] {exc}\n')


if __name__ == '__main__':
    main()
