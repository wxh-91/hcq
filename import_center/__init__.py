"""统一导入单元（包声明）。对外仅暴露取服务入口。

包被导入时即自动完成服务登记（幂等），任何入口（dz.py、admin.py、
测试脚本等）都可直接使用 get_service，不依赖外部先调用 bootstrap()。
"""
from import_center.manager import container, get_service, register_service
from import_center.bootstrap import bootstrap

bootstrap()

__all__ = ["container", "get_service", "register_service"]
