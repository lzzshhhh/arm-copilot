"""产品档案注册表。"""

from . import so101  # noqa: F401  导入即注册
from .base import PRODUCTS, ProductProfile, Step, register

__all__ = ["PRODUCTS", "ProductProfile", "Step", "register"]
