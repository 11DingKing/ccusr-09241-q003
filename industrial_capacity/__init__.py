"""工业专网能力预约与违约归因平台的服务端包入口。"""

from .app import App, create_app

PROJECT_CODE = "industrial_capacity"

__all__ = ["App", "create_app", "PROJECT_CODE", "project_info"]


def project_info() -> dict[str, str]:
    """返回稳定的项目标识,供运行检查和诊断使用。"""
    return {"code": PROJECT_CODE, "title": "工业专网能力预约与违约归因平台"}
