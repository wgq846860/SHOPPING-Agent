"""服务层错误（S1.3）。由 API 层映射为 api.v0.1 的统一错误信封。"""

from __future__ import annotations


class ProductNotFound(Exception):
    """商品不存在（404 PRODUCT_NOT_FOUND）。"""


class MerchantNotFound(Exception):
    """商家不存在（404 MERCHANT_NOT_FOUND）。"""


class InvalidMode(Exception):
    """mode_id 不属于该 Product（422 VALIDATION_ERROR）。"""


class DataError(Exception):
    """数据不完整/不可读（503 DATA_UNAVAILABLE），例如缺少必需的指标记录。"""
