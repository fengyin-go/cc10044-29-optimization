"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import quality, resource

# quality 的固定路径必须先于 resource 的 /api/{module} 动态路由注册。
ROUTERS = [quality, resource]
