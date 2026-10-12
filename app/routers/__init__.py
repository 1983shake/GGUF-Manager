"""
路由包：按职责聚合，避免单文件过长。

  · models     本地模型管理（扫描 / 检测更新 / 更新 / 删除 / 清理缓存）
  · downloads  来源检索 + 下载 + 任务状态
  · system     配置、日志、llama.cpp 集成、SSE 事件流

本文件不重复 import 各子模块 —— main.py 已显式导入，两边各写一份清单
只会出现“加了新路由但漏改一处”的问题。
`from app.routers import models` 依然可用（Python 会自动加载子模块）。
"""
