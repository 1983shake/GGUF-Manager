"""GGUF-Manager —— GGUF 模型管理器。

版本唯一来源：本文件的 4 个字段。main.py 的 OpenAPI title、Web 页脚、
User-Agent、Docker 镜像 tag、Compose 镜像名全部从这里派生
（一致性由 scripts/verify_release.py 在构建/交付前校验）。

发版规则：测试版 __version__ 保持最后一个正式版号不变，仅递增 __test_sequence__；
只有明确发布正式版时才更新 __version__，并把 __release_status__ 置为 formal、
__test_sequence__ 归 0，清除测试标识与测试记录。
"""

__version__ = "1.4.6"
__release_status__ = "formal"
__test_sequence__ = 0
__docker_tag__ = "1.4.6"
