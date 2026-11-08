"""抖音续火花系统 —— 后端应用包。

模块划分
--------
* ``config``      环境变量与运行配置
* ``db``          SQLite 访问、迁移、分页
* ``router``      路由表、请求/响应对象
* ``http_server`` HTTP 服务器、鉴权、限流、静态资源
* ``bootstrap``   启动装配（迁移 + 种子 + 路由加载 + 管理员）
* ``common``      纯逻辑工具（安全、时间、配额、文案、邮件、日志、鉴权）
* ``api``         接口定义（只做参数校验与响应拼装）
* ``automation``  抖音自动化（CDP 浏览器驱动、登录、续火花任务）
* ``worker``      调度器与任务执行
"""

from .version import BUILD, PRODUCT, VERSION

__version__ = VERSION

__all__ = ["VERSION", "BUILD", "PRODUCT", "__version__"]
