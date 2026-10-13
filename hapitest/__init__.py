"""HapiTest - 接口自动化测试平台（Core）。

Core 提供：项目管理、接口目录、用例与断言、套件执行、运行历史、报告导出、
导入器（OpenAPI / Postman / Apifox / curl / Spring 源码扫描）与 Web 控制台。

设计原则：
- 仅依赖 Python 3.9+ 标准库，零安装即可运行；
- 项目之间数据隔离（每个项目独立 SQLite 与目录）；
- 平台只做测试编排，不感知业务实现。
"""

__version__ = "0.1.0"
PLATFORM_NAME = "HapiTest"
PROTOCOL_VERSION = "1.0"
