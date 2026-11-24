"""学校场地系统：登录、读取可预定的内容、提交预定。"""

from .errors import LoginRejected, NeedsUser, PortalError, SessionLost, Uncertain, Unexpected, Unreachable
from .school import DEFAULT_PHONE, Form, Profile, Record, Reply, School, Verdict, classify

__all__ = [
    "DEFAULT_PHONE",
    "Form",
    "LoginRejected",
    "NeedsUser",
    "PortalError",
    "Profile",
    "Record",
    "Reply",
    "School",
    "SessionLost",
    "Uncertain",
    "Unexpected",
    "Unreachable",
    "Verdict",
    "classify",
]
