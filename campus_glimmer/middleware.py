# campus_glimmer/middleware.py
"""请求关联 ID：让分散在多实例、多行里的日志能对回同一次请求。

生产是多副本部署，一条用户投诉落到日志里只是某个副本的某一行。没有关联 ID
时只能靠时间戳和 URL 猜，日志收集得再集中也对不回去。这里的做法是：

* 客户端带 ``X-Request-ID`` 就沿用，否则自己生成一个；
* 同一个值同时写进响应头和日志上下文，gunicorn 的 access log 也打同一个字段；
* 对客户端传入的值只接受 ``[A-Za-z0-9._-]`` 且不超过 64 字符，否则丢弃重新
  生成——否则一个带换行符的请求头就能往日志文件里伪造一整行记录。
"""

import logging
import re
import uuid
from contextvars import ContextVar

REQUEST_ID_HEADER = 'X-Request-ID'
REQUEST_ID_META_KEY = 'HTTP_X_REQUEST_ID'

# 日志里没有请求上下文时的占位符，保持格式稳定，方便 awk/grep 切字段。
NO_REQUEST_ID = '-'

# 只放行安全字符：日志文件是按行读的，放行控制字符等于允许日志注入。
_REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}$')

_current_request_id = ContextVar('current_request_id', default=NO_REQUEST_ID)


def current_request_id():
    """当前请求的关联 ID，日志过滤器用它填 ``%(request_id)s``。"""

    return _current_request_id.get()


def new_request_id():
    return uuid.uuid4().hex[:16]


def sanitize_request_id(raw):
    """校验客户端传入的关联 ID，不合格就返回空串让调用方重新生成。"""

    if not raw:
        return ''
    candidate = raw.strip()
    return candidate if _REQUEST_ID_RE.match(candidate) else ''


class RequestIdFilter(logging.Filter):
    """把当前请求 ID 注入每条日志记录。

    优先取 ``record.request``：Django 自己记 4xx/5xx 的地方
    （``BaseHandler.get_response``）跑在中间件链**返回之后**，那时上下文变量
    已经被 reset 掉了。偏偏这些正是最需要关联的日志行。Django 的
    ``django.utils.log.log_response`` 会把 request 塞进 ``extra``，所以从记录
    本身取比从上下文取更可靠。

    没有 request 的记录（启动阶段、后台线程、定时任务、业务代码里的裸日志）
    退回上下文变量，再没有就落到占位符——否则格式化时 ``%(request_id)s``
    会直接把日志打挂。
    """

    def filter(self, record):
        request = getattr(record, 'request', None)
        record.request_id = (
            getattr(request, 'request_id', '') or current_request_id()
            or NO_REQUEST_ID
        )
        return True


class RequestIdMiddleware:
    """透传或生成请求关联 ID，并挂到响应头与日志上下文上。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = sanitize_request_id(
            request.META.get(REQUEST_ID_META_KEY, '')) or new_request_id()
        request.request_id = request_id

        # ContextVar 必须成对 reset：CAS 登录之类的流程会在一个请求里反复
        # 进出中间件，漏掉 reset 会把上一个请求的 ID 泄漏给下一个。
        token = _current_request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _current_request_id.reset(token)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response
