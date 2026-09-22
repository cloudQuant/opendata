"""Retry utility for data fetch operations."""

import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from loguru import logger as _default_logger

P = ParamSpec("P")
R = TypeVar("R")


def _resolve_logger(logger: logging.Logger | None, args: tuple[Any, ...]) -> logging.Logger:
    """Resolve logger from decorator arg or instance attribute."""
    if logger is not None:
        return logger
    if args and hasattr(args[0], "logger"):
        return args[0].logger  # type: ignore[no-any-return]
    return _default_logger  # type: ignore[return-value]


def retry_on_exception(
    max_retries: int = 3,
    retry_delay: int = 5,
    logger: logging.Logger | None = None,
    allowed_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    重试装饰器

    Args:
        max_retries: 最大重试次数
        retry_delay: 重试延迟（秒）
        logger: 日志记录器
        allowed_exceptions: 允许重试的异常类型
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            local_logger = _resolve_logger(logger, args)

            last_exception: BaseException | None = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except allowed_exceptions as e:
                    last_exception = e
                    local_logger.warning(
                        f"{func.__name__} 第 {attempt + 1}/{max_retries} 次执行失败: {e}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay * (2**attempt))  # 指数退避

            local_logger.error(f"{func.__name__} 执行失败，达到最大重试次数")
            if last_exception is None:
                raise RuntimeError("Unexpected: no exception to raise") from None
            raise last_exception

        return wrapper

    return decorator


def async_retry_on_exception(
    max_retries: int = 3,
    retry_delay: int = 5,
    logger: logging.Logger | None = None,
    allowed_exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    异步重试装饰器

    Args:
        max_retries: 最大重试次数
        retry_delay: 重试延迟（秒）
        logger: 日志记录器
        allowed_exceptions: 允许重试的异常类型
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            local_logger = _resolve_logger(logger, args)

            last_exception: BaseException | None = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except allowed_exceptions as e:
                    last_exception = e
                    local_logger.warning(
                        f"{func.__name__} 第 {attempt + 1}/{max_retries} 次执行失败: {e}"
                    )
                    if attempt < max_retries - 1:
                        import asyncio

                        await asyncio.sleep(retry_delay * (2**attempt))

            local_logger.error(f"{func.__name__} 执行失败，达到最大重试次数")
            if last_exception is None:
                raise RuntimeError("Unexpected: no exception to raise") from None
            raise last_exception

        return wrapper

    return decorator
