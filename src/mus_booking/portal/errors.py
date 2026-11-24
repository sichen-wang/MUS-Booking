"""访问学校系统时可能遇到的问题。按“该怎么处理”分类，而不是按出错的位置分类。"""


class PortalError(Exception):
    """学校系统相关的错误。消息是可以直接给人看的中文。"""


class Unreachable(PortalError):
    """网络不通、超时或学校服务器出错。请求没有生效，稍后可以重试。"""


class Uncertain(PortalError):
    """提交已经发出但没有收到答复，不知道是否订上了。不能重发，只能去“我的预定”里核对。"""


class LoginRejected(PortalError):
    """学校明确拒绝了学号或密码。在用户重新登录之前不要再用这个密码尝试。"""


class NeedsUser(PortalError):
    """学校要求额外的验证，或者需要密码而没有保存。必须由用户本人处理。"""


class SessionLost(PortalError):
    """场地系统的登录在途中失效了。重新登录一次之后可以接着做。"""


class Unexpected(PortalError):
    """学校页面的结构与已知的不一致，可能是学校改了系统。"""
