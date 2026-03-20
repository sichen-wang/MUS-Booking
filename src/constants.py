# -*- coding: utf-8 -*-
from typing import List, Tuple

# ---- UI 常量：地点列表（用于下拉与补全；与核心 FID_MAP 的 key 对应） ----
PLACES: List[str] = [
    "MPC319 管弦乐学部",
    "MPC320 管弦乐学部",
    "MPC321 室内乐琴房（GP）",
    "MPC322 室内乐琴房（UP）",
    "MPC323 管弦乐学部琴房",
    "MPC324 管弦乐学部琴房",
    "MPC325 管弦乐学部琴房（UP）",
    "MPC326 管弦乐学部琴房（UP）",
    "MPC327 管弦乐学部琴房（UP）",
    "MPC328 管弦乐学部琴房（UP）",
    "MPC329 管弦乐学部琴房（UP）",
    "MPC334 室内乐琴房（GP）",
    "MPC335 管弦乐学部琴房",
    "MPC336 管弦乐学部琴房",
    "MPC337 管弦乐学部琴房",
    "MPC401 管弦乐学部琴房",
    "MPC402 管弦乐学部琴房",
    "MPC403 管弦乐学部琴房",
    "MPC404 管弦乐学部琴房",
    "MPC405 管弦乐学部琴房",
    "MPC406 管弦乐学部琴房（UP）",
    "MPC407 管弦乐学部琴房",
    "MPC408 管弦乐学部琴房（UP）",
    "MPC409 管弦乐学部琴房（UP）",
    "MPC410 管弦乐学部琴房",
    "MPC411 管弦乐学部琴房",
    "MPC412 室内乐琴房（GP）",
    "MPC413 室内乐琴房（UP）",
    "MPC414 管弦乐学部琴房",
    "MPC415 管弦乐学部琴房（UP）",
    "MPC416 管弦乐学部琴房",
    "MPC417 管弦乐学部琴房（UP）",
    "MPC418 管弦乐学部琴房（GP）",
    "MPC419 管弦乐学部琴房（GP）",
    "MPC420 管弦乐学部琴房（GP）",
    "MPC421 管弦乐学部琴房",
    "MPC422 管弦乐学部琴房（GP）",
    "MPC423 管弦乐学部琴房",
    "MPC424 管弦乐学部琴房（GP）",
    "MPC425 室内乐琴房（GP）",
    "MPC426 管弦乐学部琴房",
    "MPC427 管弦乐学部琴房",
    "MPC428 管弦乐学部琴房",
    "MPC429 管弦乐学部琴房",
    "MPC430 管弦乐学部琴房",
    "MPC518 室内乐琴房（Double GP）",
    "MPC519 室内乐琴房（Double GP）",
    "MPC524室内乐琴房（Double GP）",
]

# ---- 默认值 ----
DEFAULT_THEME: str = "练琴"

# 预定结果关键字：命中任一则停止重试
MUST_STOP_KEYWORDS: Tuple[str, ...] = (
    "Cookie 过期",
    "保存成功",
    "手速太慢，该时间段已经被预订啦",
    "请求失败, 检查网络、代理服务器或 VPN",
)

# proxies 不再是必填项，因为直接连接（校园网内或 AnyConnect VPN）时不需要代理
REQUIRED_FIELDS: Tuple[str, ...] = ("user_id", "user_password", "user_name", "user_email")
