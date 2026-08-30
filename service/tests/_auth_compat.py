"""v1 遗留 ``deskbot_server.auth.service`` 兼容层。

该模块在 v1→v2 迁移时已消失（用户业务统一走 ``service.user_service.UserService``），
旧测试仍按 ``from deskbot_server.auth.service import ...`` 引用。为不污染生产代码，
这里提供等价函数供测试替换 import。
"""

from __future__ import annotations

from deskbot_server.service.user_service import UserService


def create_user(email: str, password: str):
    """等价于旧 auth.service.create_user（返回带 .id 的 User 对象）。

    v2 的 UserService().register 返回受影响行数（int），需按邮箱回查对象。
    """
    UserService().register(email, password)
    user = UserService().get_user_by_email(email)
    if user is None:
        raise ValueError("注册失败")
    return user


def get_user_by_email(email: str):
    return UserService().get_user_by_email(email)


def set_user_developer(user_id: str, *, is_developer: bool = True):
    return UserService().set_developer(user_id, is_developer=is_developer)
