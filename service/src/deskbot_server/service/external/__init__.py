"""外部进程服务管理：manifest 契约、状态机、看门狗。

新增外部服务 = 在 service/externals/<name>/ 下放一个 service.yaml +
可执行 install 脚本，管理框架无需改动。
"""
