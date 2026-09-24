"""本地运行入口:``python3 -m industrial_capacity`` 启动 HTTP 服务。

环境变量:
- ``IC_PORT``:监听端口,默认 8080
- ``IC_HOST``:监听地址,默认 127.0.0.1
"""

from __future__ import annotations

import os

from .app import create_app
from .interfaces.http_api import make_server


def main() -> None:
    host = os.environ.get("IC_HOST", "127.0.0.1")
    port = int(os.environ.get("IC_PORT", "8080"))
    app = create_app()
    server = make_server(app, host, port)
    print(f"工业专网能力预约与违约归因平台已启动: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
