from collections.abc import Iterable
from pathlib import Path
from typing import Any
from tortoise import fields
from pydantic import BaseModel, Field
from zhenxun.services.db_context import Model
from zhenxun.ui.models import RenderableComponent
from zhenxun.ui.models.core.base import ContainerComponent

QUOTE_ASSETS_PATH = Path(__file__).parent / "templates"

_base_theme_cache: dict[str, str] = {}


def _find_base_theme_for_variant(variant_name: str) -> str:
    if not _base_theme_cache:
        components_root = QUOTE_ASSETS_PATH / "components"
        if components_root.exists() and components_root.is_dir():
            for component_dir in components_root.iterdir():
                if component_dir.is_dir():
                    base_theme_name = component_dir.name
                    _base_theme_cache[base_theme_name] = base_theme_name
                    skins_dir = component_dir / "skins"
                    if skins_dir.exists() and skins_dir.is_dir():
                        for skin_dir in skins_dir.iterdir():
                            if skin_dir.is_dir():
                                _base_theme_cache[skin_dir.name] = base_theme_name
    return _base_theme_cache.get(variant_name, "qq-native")


class Quote(Model):
    """语录模型"""

    id = fields.IntField(pk=True, generated=True)
    """主键ID"""

    group_id = fields.CharField(max_length=64, index=True)
    """群组ID"""

    image_path = fields.CharField(max_length=255, unique=True)
    """图片路径"""

    image_hash = fields.CharField(max_length=64, null=True, index=True)
    """图片哈希值，用于检测重复图片"""

    ocr_text = fields.TextField(null=True)
    """OCR识别文本"""

    recorded_text = fields.TextField(null=True)
    """记录的文本"""

    tags = fields.JSONField(default=list)
    """标签列表"""

    manual_tags = fields.JSONField(default=list)
    """统一标签的旧格式兼容镜像"""

    tag_schema_version = fields.IntField(default=0)
    """标签存储版本，0 为旧 JSON，1 为关联表"""

    quoted_user_id = fields.CharField(max_length=64, null=True, index=True)
    """被记录用户的QQ号"""

    uploader_user_id = fields.CharField(max_length=64, null=True, index=True)
    """上传者的QQ号"""

    created_at = fields.DatetimeField(auto_now_add=True)
    """创建时间"""

    deleted_at = fields.DatetimeField(null=True, index=True)
    """移入回收站的时间，空值表示正常语录"""

    deleted_by_user_id = fields.CharField(max_length=64, null=True)
    """执行删除操作的用户QQ号"""

    view_count = fields.IntField(default=0)
    """查看次数"""

    class Meta:
        table = "quote"
        table_description = "语录表"

    @classmethod
    def _run_script(cls):
        return [
            "ALTER TABLE quote ADD COLUMN manual_tags TEXT;",
            "UPDATE quote SET manual_tags = '[]' WHERE manual_tags IS NULL;",
            "ALTER TABLE quote ADD COLUMN tag_schema_version INT NOT NULL DEFAULT 0;",
            "ALTER TABLE quote ADD COLUMN deleted_at TIMESTAMP NULL;",
            "ALTER TABLE quote ADD COLUMN deleted_by_user_id VARCHAR(64) NULL;",
            "CREATE INDEX quote_deleted_at_idx ON quote (deleted_at);",
        ]


class QuoteTag(Model):
    """群内唯一的标签词条。"""

    id = fields.IntField(pk=True, generated=True)
    group_id = fields.CharField(max_length=64)
    value = fields.CharField(max_length=255)
    normalized_value = fields.CharField(max_length=255)
    alias_group_key = fields.CharField(max_length=32, null=True)

    class Meta:
        table = "quote_tag"
        table_description = "语录标签词条表"
        unique_together = (("group_id", "normalized_value"),)
        indexes = (("group_id", "alias_group_key"),)


class QuoteTagLink(Model):
    """语录与标签的多对多关系。"""

    id = fields.IntField(pk=True, generated=True)
    quote = fields.ForeignKeyField(
        "models.Quote",
        related_name="tag_links",
        on_delete=fields.CASCADE,
    )
    tag = fields.ForeignKeyField(
        "models.QuoteTag",
        related_name="quote_links",
        on_delete=fields.CASCADE,
    )

    class Meta:
        table = "quote_tag_link"
        table_description = "语录标签关联表"
        unique_together = (("quote", "tag"),)
        indexes = (("quote_id",), ("tag_id",))


class QuotedReplyData(BaseModel):
    """被引用消息的数据模型"""

    avatar_data_url: str
    """被引用者的头像Base64数据URI"""
    author: str
    """被引用者的名称"""
    text: Any
    """被引用消息的内容(可以是字符串或内容列表)"""


class QuoteCardData(RenderableComponent):
    """语录卡片的数据模型"""

    avatar_data_url: str
    """头像的Base64数据URI"""
    text: Any
    """语录文本或内容列表"""
    author: str
    """作者名"""
    author_role: str | None = Field(default=None, description="作者角色 (owner, admin)")
    author_level: str | None = Field(default=None, description="作者等级 (如 'LV81')")
    author_title: str | None = Field(default=None, description="作者群头衔")
    quoted_reply: QuotedReplyData | None = Field(
        default=None, description="被引用的消息内容"
    )

    @property
    def template_name(self) -> str:
        base_theme = _find_base_theme_for_variant(self.variant) if self.variant else "qq-native"
        return f"@quote/components/{base_theme}"

    variant: str | None = Field(default=None, description="要使用的皮肤/主题名称")


class QuoteSequenceData(ContainerComponent):
    """多条语录序列的数据模型"""

    messages: list[QuoteCardData] = Field(description="连续的消息卡片列表")
    """连续的消息卡片列表"""
    variant: str | None = Field(default=None, description="要使用的皮肤/主题名称")
    """要使用的皮肤/主题名称"""

    def get_children(self) -> Iterable[RenderableComponent]:
        """将消息卡片列表作为子组件暴露给渲染器"""
        yield from self.messages

    @property
    def template_name(self) -> str:
        return "@quote/pages/quote-sequence-page"


class HotQuoteItemData(BaseModel):
    """热门语录排行榜中的单个项目"""

    rank: int
    quote_id: int
    user_name: str
    avatar_data_url: str
    preview_text: str
    is_image_quote: bool
    image_path: str
    view_count: int


class HotQuotesPageData(RenderableComponent):
    """热门语录页面的数据模型"""

    group_id: str
    quotes: list[HotQuoteItemData]

    @property
    def template_name(self) -> str:
        return "@quote/pages/hot_quotes"
